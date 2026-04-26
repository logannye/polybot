"""Polybot v12 engine — single-strategy (snipe) async loop.

Periodic coroutines:
  - strategy.run_once (snipe), every snipe_interval_seconds
  - _resolution_monitor: closes filled/dry_run trades + backfills shadow + updates killswitch
  - _fill_monitor (live mode only): cancels expired post_only orders
  - _check_drawdown_halt + _check_capital_divergence: v10 safeguards
  - _hourly_summary: emails a brief status report each hour

Removed in v12:
  - _hourly_kelly_edge_adjust (no Kelly scaler in v12)
  - _hourly_learning legacy path (replaced by killswitch update)
  - _maybe_self_assess (referenced deleted analyses/calibration tables)
  - _check_positions (snipe holds to resolution; no TP/SL logic)
  - _cleanup_stale_arbs (no arb strategy)
"""
from __future__ import annotations

import asyncio
import resource
import time
import structlog
from datetime import datetime, timezone, timedelta

from polybot.strategies.base import Strategy, TradingContext
from polybot.safeguards import (
    DrawdownHalt, CapitalDivergenceMonitor, DeploymentStageGate)
from polybot.learning import shadow_log
from polybot.learning import hit_rate_killswitch

log = structlog.get_logger()


class Engine:
    def __init__(self, db, scanner, executor, recorder,
                 risk_manager, settings, email_notifier,
                 clob=None, portfolio_lock=None):
        self._db = db
        self._scanner = scanner
        self._executor = executor
        self._recorder = recorder
        self._risk = risk_manager
        self._settings = settings
        self._email = email_notifier
        self._clob = clob
        self._context = TradingContext(
            db=db, scanner=scanner, risk_manager=risk_manager,
            portfolio_lock=portfolio_lock or asyncio.Lock(),
            executor=executor, email_notifier=email_notifier,
            settings=settings, clob=clob)
        self._strategies: list[Strategy] = []
        self._last_heartbeats: dict[str, datetime] = {}
        self._drawdown_halt = DrawdownHalt(
            db=db, settings=settings, email_notifier=email_notifier)
        self._divergence_monitor = CapitalDivergenceMonitor(
            db=db, clob=clob, settings=settings, email_notifier=email_notifier)
        self._deployment_gate = DeploymentStageGate(db=db, settings=settings)
        # Compatibility shims for the safeguards (read by _check_drawdown_halt
        # / _check_capital_divergence below). These mirror v10 internals.
        self._capital_divergence_halted = False
        self._capital_divergence_ok_count = 0
        self._drawdown_cache: tuple[bool, float] | None = None

    def add_strategy(self, strategy: Strategy) -> None:
        self._strategies.append(strategy)

    async def run_forever(self):
        stage = getattr(self._settings, 'live_deployment_stage', 'dry_run')
        if not self._settings.dry_run:
            if stage == 'dry_run':
                log.critical("DEPLOYMENT_STAGE_BLOCK",
                             message="live_deployment_stage='dry_run' but dry_run=false. Refusing to start.")
                return
            if stage == 'micro_test':
                self._settings.max_total_deployed_pct = 0.05
                log.warning("MICRO_TEST_MODE", max_deployed_pct=5)

        log.info("engine_starting",
                 strategies=[s.name for s in self._strategies],
                 deployment_stage=stage,
                 dry_run=self._settings.dry_run)

        await self._reconcile_on_startup()

        tasks = [self._run_strategy(s) for s in self._strategies]
        tasks.append(self._run_periodic(self._health_check,
                                         self._settings.health_check_interval))
        tasks.append(self._run_periodic(self._resolution_monitor, 60))
        if not self._settings.dry_run and self._clob:
            tasks.append(self._run_periodic(self._fill_monitor, 30))
            tasks.append(self._run_periodic(self._check_capital_divergence, 60))
        tasks.append(self._run_periodic(self._reconcile_capital, 300))
        tasks.append(self._run_periodic(self._hourly_summary, 3600))
        await asyncio.gather(*tasks)

    async def _run_strategy(self, strategy: Strategy):
        consecutive_errors = 0
        max_backoff = 600
        kill_threshold = 30
        while True:
            try:
                if await self._check_drawdown_halt():
                    await asyncio.sleep(60)
                    continue
                if self._capital_divergence_halted:
                    await asyncio.sleep(60)
                    continue
                await strategy.run_once(self._context)
                consecutive_errors = 0
                self._last_heartbeats[strategy.name] = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                log.info("strategy_shutdown", strategy=strategy.name)
                return
            except Exception as e:
                consecutive_errors += 1
                backoff = min(30 * (2 ** (consecutive_errors - 1)), max_backoff)
                log.error("strategy_error", strategy=strategy.name,
                          error=str(e), consecutive=consecutive_errors,
                          backoff_s=backoff)
                if consecutive_errors >= kill_threshold:
                    log.critical("strategy_disabled", strategy=strategy.name)
                    try:
                        await self._email.send(
                            f"[POLYBOT CRITICAL] {strategy.name} disabled",
                            f"Strategy disabled after {kill_threshold} errors: {e}")
                    except Exception:
                        pass
                    return
                if consecutive_errors % 5 == 0:
                    try:
                        await self._email.send(
                            f"[POLYBOT WARNING] {strategy.name} errors: {consecutive_errors}",
                            f"Backing off {backoff}s. Latest: {e}")
                    except Exception:
                        pass
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(strategy.interval_seconds)

    async def _run_periodic(self, func, interval_seconds):
        while True:
            try:
                await func()
            except asyncio.CancelledError:
                log.info("periodic_shutdown", func=func.__name__)
                return
            except Exception as e:
                log.error("periodic_error", func=func.__name__, error=str(e))
            await asyncio.sleep(interval_seconds)

    async def _reconcile_on_startup(self):
        try:
            open_trades = await self._db.fetch(
                "SELECT * FROM trades WHERE status IN ('open', 'dry_run')")
            now = datetime.now(timezone.utc)
            for trade in open_trades:
                market = await self._db.fetchrow(
                    "SELECT * FROM markets WHERE id = $1", trade["market_id"])
                if market and market["resolution_time"] and market["resolution_time"] < now:
                    try:
                        resolved = await self._scanner.fetch_market_resolution(
                            market["polymarket_id"])
                    except Exception as e:
                        log.error("reconcile_resolution_fetch_failed",
                                  trade_id=trade["id"], error=str(e))
                        continue
                    if resolved is not None:
                        await self._close_resolved_trade(trade, market, resolved, now)
            if not self._settings.dry_run and self._clob:
                try:
                    balance = await self._clob.get_balance()
                    await self._db.execute(
                        "UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                    log.info("startup_bankroll_synced", balance=balance)
                except Exception as e:
                    log.error("startup_bankroll_sync_failed", error=str(e))
        except Exception as e:
            log.error("reconciliation_error", error=str(e))

    async def _check_drawdown_halt(self) -> bool:
        if self._drawdown_cache is not None:
            cached_result, cached_at = self._drawdown_cache
            if time.monotonic() - cached_at < 30:
                return cached_result
        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            self._drawdown_cache = (False, time.monotonic())
            return False
        bankroll = float(state["bankroll"])
        high_water = float(state.get("high_water_bankroll", bankroll) or bankroll)
        halt_until = state.get("drawdown_halt_until")
        if halt_until and halt_until > datetime.now(timezone.utc):
            self._drawdown_cache = (True, time.monotonic())
            return True
        if bankroll > high_water:
            await self._db.execute(
                "UPDATE system_state SET high_water_bankroll = $1 WHERE id = 1", bankroll)
            self._drawdown_cache = (False, time.monotonic())
            return False
        if high_water > 0:
            drawdown = 1.0 - (bankroll / high_water)
            max_drawdown = getattr(self._settings, 'max_total_drawdown_pct', 0.30)
            if drawdown >= max_drawdown:
                halt_time = datetime.now(timezone.utc) + timedelta(days=365)
                await self._db.execute(
                    "UPDATE system_state SET drawdown_halt_until = $1 WHERE id = 1",
                    halt_time)
                log.critical("DRAWDOWN_HALT", bankroll=bankroll,
                             high_water=high_water,
                             drawdown_pct=round(drawdown * 100, 1))
                try:
                    await self._email.send(
                        "[POLYBOT CRITICAL] DRAWDOWN HALT",
                        f"<p>Bankroll ${bankroll:.2f} is {drawdown*100:.1f}% below "
                        f"high-water ${high_water:.2f}. All trading halted.</p>")
                except Exception:
                    pass
                self._drawdown_cache = (True, time.monotonic())
                return True
        self._drawdown_cache = (False, time.monotonic())
        return False

    async def _check_capital_divergence(self):
        if not self._clob or self._settings.dry_run:
            return
        try:
            state = await self._db.fetchrow(
                "SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
            clob_balance = await self._clob.get_balance()
            expected_cash = float(state["bankroll"]) - float(state["total_deployed"])
            if expected_cash <= 0:
                return
            divergence = abs(clob_balance - expected_cash) / expected_cash
            max_div = getattr(self._settings, 'max_capital_divergence_pct', 0.10)
            if divergence > max_div:
                self._capital_divergence_halted = True
                self._capital_divergence_ok_count = 0
                log.critical("CAPITAL_DIVERGENCE_HALT", clob=clob_balance,
                             expected=expected_cash,
                             divergence_pct=round(divergence * 100, 1))
                try:
                    await self._email.send(
                        "[POLYBOT CRITICAL] Capital divergence halt",
                        f"<p>CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}, "
                        f"Divergence: {divergence*100:.1f}%</p>")
                except Exception:
                    pass
            elif self._capital_divergence_halted:
                self._capital_divergence_ok_count += 1
                if self._capital_divergence_ok_count >= 3:
                    self._capital_divergence_halted = False
                    self._capital_divergence_ok_count = 0
                    log.info("CAPITAL_DIVERGENCE_RECOVERED",
                             clob=clob_balance, expected=expected_cash)
        except Exception as e:
            log.error("capital_divergence_check_error", error=str(e))

    async def _health_check(self):
        now = datetime.now(timezone.utc)
        for name, last in self._last_heartbeats.items():
            elapsed = (now - last).total_seconds()
            if elapsed > self._settings.heartbeat_critical_seconds:
                try:
                    await self._email.send(
                        f"[POLYBOT CRITICAL] {name} unresponsive",
                        f"Strategy {name} has not completed a cycle in {elapsed:.0f}s")
                except Exception:
                    pass
            elif elapsed > self._settings.heartbeat_warn_seconds:
                log.warning("heartbeat_warn", strategy=name, elapsed=elapsed)
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss_mb = usage.ru_maxrss / 1024 / 1024
            if rss_mb > 512:
                log.warning("high_memory", rss_mb=rss_mb)
        except Exception:
            pass

    async def _fill_monitor(self):
        """Live-only: cancel post_only limits that have aged past the timeout."""
        if not self._clob or self._settings.dry_run:
            return
        open_orders = await self._db.fetch(
            "SELECT * FROM trades WHERE status = 'open' "
            "AND clob_order_id IS NOT NULL AND strategy = 'snipe'")
        timeout = float(getattr(self._settings, 'fill_timeout_seconds', 60))
        for trade in open_orders:
            try:
                status = await self._clob.get_order_status(trade["clob_order_id"])
            except Exception as e:
                log.error("fill_check_failed", trade_id=trade["id"], error=str(e))
                continue
            if status["status"] == "matched":
                await self._db.execute(
                    "UPDATE trades SET status = 'filled' WHERE id = $1", trade["id"])
                try:
                    balance = await self._clob.get_balance()
                    await self._db.execute(
                        "UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                except Exception as e:
                    log.error("bankroll_sync_failed", error=str(e))
                log.info("order_filled", trade_id=trade["id"])
            elif status["status"] == "cancelled":
                await self._db.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade["id"])
                await self._db.execute(
                    "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                    float(trade["position_size_usd"]))
            elif status["status"] == "live":
                elapsed = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
                if elapsed > timeout:
                    try:
                        await self._clob.cancel_order(trade["clob_order_id"])
                    except Exception as e:
                        log.error("cancel_failed", trade_id=trade["id"], error=str(e))
                        continue
                    await self._db.execute(
                        "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade["id"])
                    await self._db.execute(
                        "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                        float(trade["position_size_usd"]))
                    log.info("order_timed_out", trade_id=trade["id"], elapsed=elapsed)

    async def _resolution_monitor(self):
        """For each filled/dry_run trade past resolution time:
        close the trade, backfill shadow signals, and refresh the killswitch.
        """
        now = datetime.now(timezone.utc)
        resolvable = await self._db.fetch(
            """SELECT t.* FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.status IN ('filled', 'dry_run')
                 AND m.resolution_time <= $1""", now)
        log.info("resolution_monitor_check", resolvable_count=len(resolvable))
        for trade in resolvable:
            market = await self._db.fetchrow(
                "SELECT * FROM markets WHERE id = $1", trade["market_id"])
            if not market:
                continue
            try:
                outcome = await self._scanner.fetch_market_resolution(
                    market["polymarket_id"])
            except Exception as e:
                log.error("resolution_check_failed",
                          trade_id=trade["id"], error=str(e))
                continue
            if outcome is None:
                log.debug("resolution_pending", trade_id=trade["id"])
                continue
            await self._close_resolved_trade(trade, market, outcome, now)

        # Refresh killswitch + rolling hit rate gauge after every cycle.
        try:
            await hit_rate_killswitch.update_and_check(
                self._db,
                window=int(getattr(self._settings, 'killswitch_window', 50)),
                min_hit_rate=float(getattr(self._settings, 'killswitch_min_hit_rate', 0.97)),
                min_n=int(getattr(self._settings, 'killswitch_min_n', 50)),
                email_notifier=self._email,
            )
        except Exception as e:
            log.error("killswitch_update_failed", error=str(e))

    async def _close_resolved_trade(self, trade, market, outcome: int, now) -> None:
        """Common close path for filled (live) and dry_run trades.

        Updates the trade row, system_state, strategy_performance,
        trade_outcome, and backfills the shadow signal log.
        """
        entry = float(trade["entry_price"])
        shares = float(trade["shares"])
        side = trade["side"]
        if side == "YES":
            pnl = shares * (outcome - entry)
        else:
            pnl = shares * ((1 - outcome) - (1 - entry))

        if trade["status"] == "filled":
            await self._db.execute(
                """UPDATE trades SET status='closed', exit_price=$1,
                   exit_reason='resolution', pnl=$2, closed_at=$3 WHERE id=$4""",
                float(outcome), pnl, now, trade["id"])
            await self._db.execute(
                "UPDATE system_state SET total_deployed = total_deployed - $1, "
                "daily_pnl = daily_pnl + $2 WHERE id = 1",
                float(trade["position_size_usd"]), pnl)
            if self._clob:
                try:
                    balance = await self._clob.get_balance()
                    await self._db.execute(
                        "UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                except Exception as e:
                    log.error("bankroll_sync_failed", error=str(e))
        else:    # dry_run
            await self._db.execute(
                """UPDATE trades SET status='dry_run_resolved', pnl=$1,
                   exit_price=$2, exit_reason='resolution', closed_at=$3
                   WHERE id=$4""",
                pnl, float(outcome), now, trade["id"])
            await self._db.execute(
                """UPDATE system_state SET bankroll = bankroll + $1,
                   total_deployed = total_deployed - $2,
                   daily_pnl = daily_pnl + $1 WHERE id = 1""",
                pnl, float(trade["position_size_usd"]))

        # Update strategy_performance.
        await self._db.execute(
            """UPDATE strategy_performance SET
               total_trades = total_trades + 1,
               winning_trades = winning_trades + CASE WHEN $1 > 0 THEN 1 ELSE 0 END,
               total_pnl = total_pnl + $1, last_updated = $2
               WHERE strategy = $3""",
            pnl, now, trade.get("strategy", "snipe"))

        # Write trade_outcome row (the v12 source-of-truth for hit rate).
        kelly_inputs = trade.get("kelly_inputs") or {}
        if isinstance(kelly_inputs, str):
            try:
                import json
                kelly_inputs = json.loads(kelly_inputs)
            except Exception:
                kelly_inputs = {}
        verifier_conf = kelly_inputs.get("verifier_confidence")
        verifier_reason = kelly_inputs.get("verifier_reason", "")
        won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
        try:
            await self._db.execute(
                """INSERT INTO trade_outcome (
                       strategy, market_id, market_category,
                       entry_price, exit_price, pnl, predicted_prob,
                       realized_outcome, kelly_inputs, exit_reason,
                       duration_minutes, verifier_confidence, verifier_reason)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
                trade.get("strategy", "snipe"),
                trade["market_id"],
                market.get("category", "unknown"),
                entry, float(outcome), pnl, entry,
                1 if won else 0,
                kelly_inputs if isinstance(kelly_inputs, (dict, list, str)) else {},
                'resolution',
                (now - trade["opened_at"]).total_seconds() / 60.0,
                verifier_conf, verifier_reason)
        except Exception as e:
            log.error("trade_outcome_insert_failed",
                      trade_id=trade["id"], error=str(e))

        # Backfill shadow_signal rows for this market.
        try:
            await shadow_log.backfill_resolution(
                self._db, market["polymarket_id"], int(outcome))
        except Exception as e:
            log.error("shadow_backfill_failed",
                      polymarket_id=market["polymarket_id"], error=str(e))

        log.info("trade_resolved", trade_id=trade["id"], outcome=outcome,
                 pnl=round(pnl, 4), strategy=trade.get("strategy"),
                 verifier_confidence=verifier_conf)

    async def _reconcile_capital(self):
        """Ensure system_state.total_deployed matches actual open positions."""
        actual = await self._db.fetchval(
            """SELECT COALESCE(SUM(position_size_usd), 0) FROM trades
               WHERE status IN ('open', 'filled', 'dry_run')""")
        actual = float(actual)
        state = await self._db.fetchrow(
            "SELECT total_deployed FROM system_state WHERE id = 1")
        if not state:
            return
        recorded = float(state["total_deployed"])
        if abs(recorded - actual) > 1.0:
            log.warning("capital_reconciliation",
                        recorded=recorded, actual=actual)
            await self._db.execute(
                "UPDATE system_state SET total_deployed = $1 WHERE id = 1",
                actual)

    async def _hourly_summary(self):
        """Emit a structured-log summary line every hour. Cheap, observable,
        and survives restart. Email digest is daily (handled by ops, not bot)."""
        state = await self._db.fetchrow(
            """SELECT bankroll, total_deployed, daily_pnl, rolling_hit_rate,
                      rolling_hit_rate_n, killswitch_tripped_at,
                      live_deployment_stage
               FROM system_state WHERE id = 1""")
        if not state:
            return
        open_count = await self._db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy='snipe' "
            "AND status IN ('open', 'filled', 'dry_run')")
        closed_24h = await self._db.fetchval(
            "SELECT COUNT(*) FROM trade_outcome "
            "WHERE strategy='snipe' AND closed_at > NOW() - INTERVAL '24 hours'")
        signals_24h = await self._db.fetchval(
            "SELECT COUNT(*) FROM shadow_signal "
            "WHERE signaled_at > NOW() - INTERVAL '24 hours'")
        log.info("hourly_summary",
                 bankroll=float(state["bankroll"]),
                 total_deployed=float(state["total_deployed"]),
                 daily_pnl=float(state["daily_pnl"]),
                 open_positions=int(open_count or 0),
                 closed_24h=int(closed_24h or 0),
                 signals_24h=int(signals_24h or 0),
                 rolling_hit_rate=(float(state["rolling_hit_rate"])
                                   if state["rolling_hit_rate"] is not None else None),
                 rolling_hit_rate_n=int(state["rolling_hit_rate_n"]),
                 killswitch_tripped=bool(state["killswitch_tripped_at"]),
                 deployment_stage=state["live_deployment_stage"])
