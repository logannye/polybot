"""Mean reversion strategy for Polymarket.

Detects markets where price has moved >10% in the last hour and takes a
contrarian position betting on partial reversion.
"""

import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size, conviction_multiplier
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def __init__(self, settings):
        self.interval_seconds = settings.mr_interval_seconds
        self.kelly_multiplier = settings.mr_kelly_mult
        self.max_single_pct = settings.mr_max_single_pct
        self._trigger = settings.mr_trigger_threshold
        self._reversion_frac = settings.mr_reversion_fraction
        self._max_concurrent = settings.mr_max_concurrent
        self._min_volume = settings.mr_min_volume_24h
        self._min_depth = settings.mr_min_book_depth
        self._cooldown_hours = settings.mr_cooldown_hours
        self._max_hold_hours = settings.mr_max_hold_hours
        self._settings = settings
        self._min_expected_reversion = getattr(settings, 'mr_min_expected_reversion', 0.0)
        _bmt = getattr(settings, 'mr_big_move_threshold', 0.15)
        self._big_move_threshold = float(_bmt) if isinstance(_bmt, (int, float)) else 0.15
        _bmk = getattr(settings, 'mr_big_move_kelly_boost', 1.3)
        self._big_move_kelly_boost = float(_bmk) if isinstance(_bmk, (int, float)) else 1.3
        self._conviction_enabled = getattr(settings, 'conviction_stack_enabled', False)
        self._conviction_per_signal = getattr(settings, 'conviction_stack_per_signal', 0.5)
        self._conviction_max = getattr(settings, 'conviction_stack_max', 3.0)
        # Sliding window of price snapshots (polymarket_id -> [(price, timestamp), ...])
        self._price_snapshots: dict[str, list[tuple[float, datetime]]] = {}
        self._snapshot_window: int = 5  # keep last N snapshots per market

    def inject_snapshots(self, market_id: str, price: float, old_price: float) -> None:
        """Inject a synthetic snapshot pair from external price history scanning.

        This allows the PriceHistoryScanner to feed detected big moves
        into the MR sliding window, enabling the standard run_once()
        candidate detection to pick them up on the next cycle.
        """
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # Only inject if we don't already have snapshots for this market
        if not self._price_snapshots.get(market_id):
            self._price_snapshots[market_id] = [
                (old_price, now - timedelta(minutes=5)),
            ]
            log.info("mr_snapshot_injected", market=market_id,
                     old_price=round(old_price, 4), current=round(price, 4))

    async def run_once(self, ctx: TradingContext) -> None:
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name)
        if enabled is False:
            log.debug("strategy_skipped", strategy=self.name, reason="disabled")
            return

        # Count open mean-reversion positions
        mr_open = await ctx.db.fetchval(
            """SELECT COUNT(*) FROM trades
               WHERE strategy = 'mean_reversion'
                 AND status IN ('open', 'filled', 'dry_run')""")
        if mr_open and mr_open >= self._max_concurrent:
            log.debug("mr_position_cap", open=mr_open, max=self._max_concurrent)
            return

        markets = await ctx.scanner.fetch_markets()
        if not markets:
            return

        now = datetime.now(timezone.utc)
        state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state_row:
            return
        bankroll = float(state_row["bankroll"])

        candidates = []

        for m in markets:
            pid = m["polymarket_id"]
            price = m["yes_price"]

            # Liquidity filters
            if m.get("volume_24h", 0) < self._min_volume:
                continue
            if m.get("book_depth", 0) < self._min_depth:
                continue
            # Skip extreme prices (near resolution, snipe territory)
            if price < 0.10 or price > 0.90:
                continue

            # Compare against sliding window of recent snapshots
            if pid in self._price_snapshots:
                snapshots = self._price_snapshots[pid]
                # Only consider snapshots from the last 30 minutes
                recent = [(p, ts) for p, ts in snapshots
                          if (now - ts).total_seconds() < 1800]
                if recent:
                    min_price_in_window = min(p for p, _ in recent)
                    max_price_in_window = max(p for p, _ in recent)
                    # Check for drop from recent high
                    move_down = price - max_price_in_window
                    # Check for rise from recent low
                    move_up = price - min_price_in_window
                    if move_down < 0 and abs(move_down) >= self._trigger:
                        candidates.append((abs(move_down), move_down, m, max_price_in_window))
                    elif move_up > 0 and abs(move_up) >= self._trigger:
                        candidates.append((abs(move_up), move_up, m, min_price_in_window))

            # Append to snapshot window
            self._price_snapshots.setdefault(pid, []).append((price, now))
            self._price_snapshots[pid] = self._price_snapshots[pid][-self._snapshot_window:]

        # Prune stale snapshots (markets not seen in 2h)
        stale = [pid for pid, snaps in self._price_snapshots.items()
                 if not snaps or (now - snaps[-1][1]).total_seconds() > 7200]
        for pid in stale:
            del self._price_snapshots[pid]

        if not candidates:
            return

        # Sort by move magnitude (largest first)
        candidates.sort(key=lambda x: x[0], reverse=True)

        trades_placed = 0
        for _abs_move, move, m, old_price in candidates:
            if trades_placed >= self._max_concurrent - (mr_open or 0):
                break

            pid = m["polymarket_id"]

            # Cooldown: check for recent mean-reversion trades on this market
            recent = await ctx.db.fetchval(
                """SELECT COUNT(*) FROM trades t JOIN markets mk ON t.market_id = mk.id
                   WHERE mk.polymarket_id = $1 AND t.strategy = 'mean_reversion'
                     AND t.opened_at > NOW() - INTERVAL '1 day'""",
                pid)
            if recent and recent > 0:
                continue

            # Check no existing position
            existing = await ctx.db.fetchval(
                """SELECT COUNT(*) FROM trades t JOIN markets mk ON t.market_id = mk.id
                   WHERE mk.polymarket_id = $1 AND t.strategy = 'mean_reversion'
                     AND t.status IN ('open', 'filled', 'dry_run')""",
                pid)
            if existing and existing > 0:
                continue

            # Direction: bet against the move
            # Price went UP → buy NO (expect reversion down)
            # Price went DOWN → buy YES (expect reversion up)
            if move > 0:
                side = "NO"
                buy_price = m.get("no_price", 1.0 - m["yes_price"])
            else:
                side = "YES"
                buy_price = m["yes_price"]

            # Edge estimate: expected reversion * fraction
            expected_reversion = abs(move) * self._reversion_frac
            net_edge = expected_reversion  # maker = 0% fee

            if net_edge < 0.02:
                continue

            # Big-moves-only filter: reject if expected reversion is below minimum
            if expected_reversion < self._min_expected_reversion:
                log.debug("mr_rejected_small_reversion", market=pid,
                          expected_reversion=round(expected_reversion, 4),
                          min_required=self._min_expected_reversion)
                continue

            # Position sizing
            async with ctx.portfolio_lock:
                fresh_state = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
                bankroll = float(fresh_state["bankroll"])
                kelly_adj = bankroll_kelly_adjustment(
                    bankroll=bankroll, base_kelly=self.kelly_multiplier,
                    post_breaker_until=fresh_state.get("post_breaker_until"),
                    post_breaker_reduction=ctx.settings.post_breaker_kelly_reduction,
                    survival_threshold=ctx.settings.bankroll_survival_threshold,
                    growth_threshold=ctx.settings.bankroll_growth_threshold,
                )
                kelly_fraction = net_edge / (1 - buy_price) if buy_price < 1.0 else 0.0
                # Tiered Kelly boost: bigger moves get more aggressive sizing
                if abs(move) >= self._big_move_threshold:
                    kelly_adj *= self._big_move_kelly_boost
                size = compute_position_size(
                    bankroll=bankroll, kelly_fraction=kelly_fraction,
                    kelly_mult=kelly_adj, confidence_mult=1.0,
                    max_single_pct=self.max_single_pct,
                    min_trade_size=ctx.settings.min_trade_size)
                if size <= 0:
                    continue

                # Conviction stacking: check if cross-venue agrees on this market
                if self._conviction_enabled and size > 0:
                    cv_confirms = await ctx.db.fetchval(
                        """SELECT COUNT(*) FROM trades
                           WHERE strategy = 'cross_venue'
                             AND status IN ('open', 'filled', 'dry_run')
                             AND market_id IN (
                                 SELECT id FROM markets WHERE polymarket_id = $1
                             )""", pid)
                    if cv_confirms and cv_confirms > 0:
                        mult = conviction_multiplier(
                            cv_confirms, self._conviction_per_signal, self._conviction_max)
                        old_size = size
                        size = min(size * mult, bankroll * self.max_single_pct)
                        if size > old_size:
                            log.info("mr_conviction_boost", market=pid,
                                     multiplier=round(mult, 2), old_size=old_size, new_size=size)

                open_trades = await ctx.db.fetch(
                    """SELECT t.position_size_usd, m.category
                       FROM trades t JOIN markets m ON t.market_id = m.id
                       WHERE t.status IN ('open', 'filled', 'dry_run')""")
                cat_deployed: dict[str, float] = {}
                for t in open_trades:
                    cat = t["category"]
                    cat_deployed[cat] = cat_deployed.get(cat, 0.0) + float(t["position_size_usd"])
                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(fresh_state["total_deployed"]),
                    daily_pnl=float(fresh_state["daily_pnl"]),
                    open_count=len(open_trades), category_deployed=cat_deployed,
                    circuit_breaker_until=fresh_state.get("circuit_breaker_until"))
                proposal = TradeProposal(size_usd=size,
                                          category=m.get("category", "unknown"),
                                          book_depth=m.get("book_depth", 1000.0))
                risk_result = ctx.risk_manager.check(portfolio, proposal,
                                                      max_single_pct=self.max_single_pct)
                if not risk_result.allowed:
                    log.info("mr_risk_rejected", market=pid, reason=risk_result.reason)
                    continue

                # Upsert market
                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                           current_price, volume_24h, book_depth)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (polymarket_id) DO UPDATE SET
                           current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
                       RETURNING id""",
                    pid, m["question"], m.get("category", "unknown"),
                    m["resolution_time"], m["yes_price"],
                    m.get("volume_24h"), m.get("book_depth"))

                # Compute exit targets for position manager
                if move > 0:
                    # Price went up, we bet NO (expect down reversion)
                    tp_price = m["yes_price"] - expected_reversion
                    sl_price = m["yes_price"] + abs(move) * 0.25
                else:
                    # Price went down, we bet YES (expect up reversion)
                    tp_price = m["yes_price"] + expected_reversion
                    sl_price = m["yes_price"] - abs(move) * 0.25

                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                       ensemble_stdev, quant_signals, edge)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    market_id, json.dumps([]),
                    tp_price,  # store reversion target as ensemble_probability
                    0.0, json.dumps({"move": move, "old_price": old_price}),
                    net_edge)

                token_id = m.get("yes_token_id", "") if side == "YES" else m.get("no_token_id", "")
                result = await ctx.executor.place_order(
                    token_id=token_id, side=side, size_usd=size,
                    price=buy_price, market_id=market_id,
                    analysis_id=analysis_id, strategy=self.name,
                    kelly_inputs={
                        "move": round(move, 4),
                        "old_price": round(old_price, 4),
                        "trigger_price": round(m["yes_price"], 4),
                        "expected_reversion": round(expected_reversion, 4),
                        "tp_yes_price": round(tp_price, 4),
                        "sl_yes_price": round(sl_price, 4),
                        "max_hold_hours": self._max_hold_hours,
                    },
                    post_only=self._settings.use_maker_orders)
                if not result:
                    continue

            trades_placed += 1
            log.info("mr_trade", market=pid, side=side, price=buy_price,
                     move=round(move, 4), edge=round(net_edge, 4), size=size,
                     question=m["question"][:60])
            await ctx.email_notifier.send(
                f"[POLYBOT] Mean reversion: {m['question'][:50]}",
                format_trade_email(event="executed", market=m["question"],
                                   side=side, size=size, price=buy_price,
                                   edge=net_edge))

        if candidates:
            log.info("mr_cycle_complete", candidates=len(candidates),
                     placed=trades_placed)
