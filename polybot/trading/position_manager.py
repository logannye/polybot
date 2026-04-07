import json
import structlog
from datetime import datetime, timezone
from polybot.markets.websocket import should_early_exit

log = structlog.get_logger()


def compute_unrealized_return(side: str, entry_price: float,
                               current_yes_price: float) -> float:
    """
    Compute unrealized return as a fraction of cost basis.

    For YES: bought at entry_price, current value is current_yes_price.
    For NO:  bought at entry_price (the NO price paid), current NO value
             is (1 - current_yes_price).
    """
    if entry_price <= 0:
        return 0.0
    if side == "YES":
        return (current_yes_price - entry_price) / entry_price
    else:
        current_no_price = 1.0 - current_yes_price
        return (current_no_price - entry_price) / entry_price


def should_take_profit(side: str, entry_price: float,
                        current_yes_price: float,
                        threshold: float = 0.20) -> bool:
    return compute_unrealized_return(side, entry_price, current_yes_price) >= threshold


def should_cut_loss(side: str, entry_price: float,
                     current_yes_price: float,
                     threshold: float = 0.25) -> bool:
    return compute_unrealized_return(side, entry_price, current_yes_price) <= -threshold


class ActivePositionManager:
    def __init__(self, db, executor, scanner, email_notifier, settings,
                 portfolio_lock=None):
        self._db = db
        self._executor = executor
        self._scanner = scanner
        self._email = email_notifier
        self._take_profit = settings.take_profit_threshold
        self._stop_loss = settings.stop_loss_threshold
        _fsl = getattr(settings, 'forecast_stop_loss_threshold', None)
        self._forecast_stop_loss = float(_fsl) if isinstance(_fsl, (int, float)) else self._stop_loss
        self._early_exit_edge = settings.early_exit_edge
        self._forecast_time_stop_floor = getattr(settings, 'forecast_time_stop_minutes', 20.0)
        self._forecast_time_stop_fraction = getattr(settings, 'forecast_time_stop_fraction', 0.10)
        self._forecast_time_stop_max = getattr(settings, 'forecast_time_stop_max_minutes', 480.0)
        self._forecast_time_stop_min_resolution_hours = getattr(
            settings, 'forecast_time_stop_min_resolution_hours', 48.0)
        self._portfolio_lock = portfolio_lock
        self._snipe_max_hold_hours = getattr(settings, 'snipe_max_hold_hours', 48.0)

    async def check_positions(self):
        # Load per-strategy learned thresholds (adaptive TP/SL)
        learned_thresholds = {}
        for strat in ("snipe", "forecast"):
            row = await self._db.fetchval(
                "SELECT learned_params FROM strategy_performance WHERE strategy = $1", strat)
            if row:
                try:
                    params = json.loads(row) if isinstance(row, str) else row
                    if isinstance(params, dict) and params.get("threshold_sample_size", 0) >= 10:
                        learned_thresholds[strat] = params
                except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
                    pass

        positions = await self._db.fetch(
            """SELECT t.id, t.side, t.entry_price, t.shares,
                      t.position_size_usd, t.strategy, t.status,
                      t.opened_at, t.kelly_inputs,
                      m.polymarket_id, m.question, m.resolution_time,
                      a.ensemble_probability
               FROM trades t
               JOIN markets m ON t.market_id = m.id
               LEFT JOIN analyses a ON t.analysis_id = a.id
               WHERE t.status IN ('filled', 'dry_run')
                 AND t.strategy != 'arbitrage'""")

        if not positions:
            return

        price_cache = self._scanner.get_all_cached_prices()
        if not price_cache:
            log.debug("position_manager_no_prices")
            return

        exits_triggered = 0
        for pos in positions:
            market_data = price_cache.get(pos["polymarket_id"])
            if not market_data:
                continue

            current_yes_price = market_data["yes_price"]
            entry_price = float(pos["entry_price"])
            side = pos["side"]
            trade_id = pos["id"]

            # Time-stop: auto-exit forecast trades exceeding hold limit.
            # Dynamic: scales with time-to-resolution so long-dated markets
            # get room to breathe while short-dated ones exit fast.
            # Only fires on flat or losing positions — profitable trades fall
            # through to TP/SL/early-exit checks so winners aren't cut early.
            # Skip entirely for near-resolution markets (≤48h) — let stop-loss
            # handle catastrophic moves; hold to resolution otherwise.
            if pos["strategy"] == "forecast" and pos.get("opened_at") is not None:
                hold_minutes = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 60
                hours_to_resolution = max(
                    0.0,
                    (pos["resolution_time"] - datetime.now(timezone.utc)).total_seconds() / 3600
                ) if pos.get("resolution_time") else 0.0
                if hours_to_resolution > self._forecast_time_stop_min_resolution_hours:
                    effective_stop = min(
                        self._forecast_time_stop_max,
                        max(self._forecast_time_stop_floor,
                            self._forecast_time_stop_fraction * hours_to_resolution * 60),
                    )
                    if hold_minutes > effective_stop:
                        unrealized = compute_unrealized_return(side, entry_price, current_yes_price)
                        if unrealized > 0:
                            log.debug("time_stop_skipped_profitable",
                                      trade_id=trade_id,
                                      hold_minutes=round(hold_minutes, 1),
                                      unrealized=round(unrealized, 4))
                        else:
                            exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                            if self._portfolio_lock:
                                async with self._portfolio_lock:
                                    pnl = await self._executor.exit_position(
                                        trade_id=trade_id, exit_price=exit_price,
                                        exit_reason="time_stop")
                            else:
                                pnl = await self._executor.exit_position(
                                    trade_id=trade_id, exit_price=exit_price,
                                    exit_reason="time_stop")
                            if pnl is not None:
                                exits_triggered += 1
                                log.info("position_time_stop",
                                         trade_id=trade_id,
                                         hold_minutes=round(hold_minutes, 1),
                                         effective_stop=round(effective_stop, 1),
                                         hours_to_resolution=round(hours_to_resolution, 1),
                                         pnl=round(pnl, 4),
                                         market=pos["question"][:60])
                                await self._email.send(
                                    f"[POLYBOT] Position time-stopped",
                                    f"<p><b>Market:</b> {pos['question']}</p>"
                                    f"<p><b>Held:</b> {hold_minutes:.0f}min "
                                    f"(limit: {effective_stop:.0f}min) | "
                                    f"P&L: ${pnl:+.2f}</p>")
                            continue

            # Snipe time-stop: free capital from stale positions
            if pos["strategy"] == "snipe" and pos.get("opened_at") is not None:
                hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                if hold_hours > self._snipe_max_hold_hours:
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    if self._portfolio_lock:
                        async with self._portfolio_lock:
                            pnl = await self._executor.exit_position(
                                trade_id=trade_id, exit_price=exit_price,
                                exit_reason="time_stop")
                    else:
                        pnl = await self._executor.exit_position(
                            trade_id=trade_id, exit_price=exit_price,
                            exit_reason="time_stop")
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("snipe_time_stop", trade_id=trade_id,
                                 hold_hours=round(hold_hours, 1),
                                 pnl=round(pnl, 4),
                                 market=pos["question"][:60])
                        await self._email.send(
                            f"[POLYBOT] Snipe time-stopped ({hold_hours:.0f}h)",
                            f"<p><b>Market:</b> {pos['question']}</p>"
                            f"<p><b>Held:</b> {hold_hours:.0f}h (limit: "
                            f"{self._snipe_max_hold_hours:.0f}h) | "
                            f"P&L: ${pnl:+.2f}</p>")
                    continue

            exit_reason = None

            # Mean-reversion custom exit: use stored price targets from kelly_inputs
            if pos["strategy"] == "mean_reversion" and pos.get("kelly_inputs"):
                try:
                    ki = json.loads(pos["kelly_inputs"]) if isinstance(pos["kelly_inputs"], str) else pos["kelly_inputs"]
                    tp_yes = ki.get("tp_yes_price")
                    sl_yes = ki.get("sl_yes_price")
                    max_hold = ki.get("max_hold_hours", 24.0)

                    # Take-profit: price reverted toward target
                    if tp_yes is not None:
                        if (pos["side"] == "NO" and current_yes_price <= tp_yes) or \
                           (pos["side"] == "YES" and current_yes_price >= tp_yes):
                            exit_reason = "take_profit"

                    # Stop-loss: price moved further against us
                    if not exit_reason and sl_yes is not None:
                        if (pos["side"] == "NO" and current_yes_price >= sl_yes) or \
                           (pos["side"] == "YES" and current_yes_price <= sl_yes):
                            exit_reason = "stop_loss"

                    # Time-stop: held too long
                    if not exit_reason and pos.get("opened_at"):
                        hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                        if hold_hours > max_hold:
                            exit_reason = "time_stop"
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

                if exit_reason:
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    unrealized = compute_unrealized_return(side, entry_price, current_yes_price)
                    if self._portfolio_lock:
                        async with self._portfolio_lock:
                            pnl = await self._executor.exit_position(
                                trade_id=trade_id, exit_price=exit_price,
                                exit_reason=exit_reason)
                    else:
                        pnl = await self._executor.exit_position(
                            trade_id=trade_id, exit_price=exit_price,
                            exit_reason=exit_reason)
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("mr_position_exit", trade_id=trade_id,
                                 reason=exit_reason, pnl=round(pnl, 4),
                                 market=pos["question"][:60])
                # MR trades use custom TP/SL/time-stop above — skip generic
                # early_exit which misinterprets tp_yes_price as ensemble prob
                continue

            # Political strategy: hold to resolution.
            # The edge is structural calibration bias, not timing-dependent.
            # Only take-profit and stop-loss apply — skip early-exit (edge erosion).
            if pos["strategy"] == "political":
                if should_take_profit(side, entry_price, current_yes_price,
                                      self._take_profit):
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    if self._portfolio_lock:
                        async with self._portfolio_lock:
                            pnl = await self._executor.exit_position(
                                trade_id=trade_id, exit_price=exit_price,
                                exit_reason="take_profit")
                    else:
                        pnl = await self._executor.exit_position(
                            trade_id=trade_id, exit_price=exit_price,
                            exit_reason="take_profit")
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("pol_take_profit", trade_id=trade_id, pnl=round(pnl, 4))
                        await self._email.send(
                            f"[POLYBOT] Political position take-profit",
                            f"<p><b>Market:</b> {pos['question']}</p>"
                            f"<p><b>Side:</b> {side} | Entry: ${entry_price:.4f} | "
                            f"Exit: ${exit_price:.4f}</p>"
                            f"<p><b>P&L:</b> ${pnl:+.2f}</p>")
                elif should_cut_loss(side, entry_price, current_yes_price,
                                     self._stop_loss):
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    if self._portfolio_lock:
                        async with self._portfolio_lock:
                            pnl = await self._executor.exit_position(
                                trade_id=trade_id, exit_price=exit_price,
                                exit_reason="stop_loss")
                    else:
                        pnl = await self._executor.exit_position(
                            trade_id=trade_id, exit_price=exit_price,
                            exit_reason="stop_loss")
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("pol_stop_loss", trade_id=trade_id, pnl=round(pnl, 4))
                        await self._email.send(
                            f"[POLYBOT] Political position stop-loss",
                            f"<p><b>Market:</b> {pos['question']}</p>"
                            f"<p><b>Side:</b> {side} | Entry: ${entry_price:.4f} | "
                            f"Exit: ${exit_price:.4f}</p>"
                            f"<p><b>P&L:</b> ${pnl:+.2f}</p>")
                continue  # Skip early-exit — hold to resolution

            strategy = pos["strategy"]
            tp_threshold = learned_thresholds.get(strategy, {}).get(
                "take_profit_threshold", self._take_profit)
            base_sl = self._forecast_stop_loss if strategy == "forecast" else self._stop_loss
            sl_threshold = learned_thresholds.get(strategy, {}).get(
                "stop_loss_threshold", base_sl)

            if should_take_profit(side, entry_price, current_yes_price,
                                   tp_threshold):
                exit_reason = "take_profit"
            elif should_cut_loss(side, entry_price, current_yes_price,
                                  sl_threshold):
                exit_reason = "stop_loss"
            elif pos["strategy"] != "snipe" and pos["ensemble_probability"] is not None:
                ensemble_prob = float(pos["ensemble_probability"])
                if should_early_exit(
                    entry_price=entry_price,
                    current_price=current_yes_price,
                    side=side,
                    ensemble_prob=ensemble_prob,
                    early_exit_edge=self._early_exit_edge,
                ):
                    exit_reason = "early_exit"

            if not exit_reason:
                continue

            exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
            unrealized = compute_unrealized_return(side, entry_price, current_yes_price)

            if self._portfolio_lock:
                async with self._portfolio_lock:
                    pnl = await self._executor.exit_position(
                        trade_id=trade_id, exit_price=exit_price,
                        exit_reason=exit_reason)
            else:
                pnl = await self._executor.exit_position(
                    trade_id=trade_id, exit_price=exit_price,
                    exit_reason=exit_reason)

            if pnl is not None:
                exits_triggered += 1
                log.info("position_exit_triggered",
                         trade_id=trade_id, reason=exit_reason,
                         unrealized_return=round(unrealized, 4),
                         pnl=round(pnl, 4),
                         market=pos["question"][:60])
                await self._email.send(
                    f"[POLYBOT] Position exited: {exit_reason}",
                    f"<p><b>Market:</b> {pos['question']}</p>"
                    f"<p><b>Side:</b> {side} | Entry: ${entry_price:.4f} | "
                    f"Exit: ${exit_price:.4f}</p>"
                    f"<p><b>P&L:</b> ${pnl:+.2f} ({unrealized:.1%})</p>")

        if exits_triggered > 0:
            log.info("position_manager_cycle",
                     checked=len(positions), exits=exits_triggered)
