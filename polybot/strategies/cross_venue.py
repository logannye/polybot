"""Cross-venue arbitrage strategy.

Compares Polymarket prices against sportsbook consensus (via The Odds API)
and trades when Polymarket diverges significantly.
"""

import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size, conviction_multiplier
from polybot.analysis.odds_client import find_divergences
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


class CrossVenueStrategy(Strategy):
    name = "cross_venue"

    def __init__(self, settings, odds_client):
        self.interval_seconds = settings.cv_interval_seconds
        self.kelly_multiplier = settings.cv_kelly_mult
        self.max_single_pct = settings.cv_max_single_pct
        self._min_divergence = settings.cv_min_divergence
        self._cooldown_hours = settings.cv_cooldown_hours
        self._odds_client = odds_client
        self._settings = settings
        self._traded_events: dict[str, datetime] = {}
        self._conviction_enabled = getattr(settings, 'conviction_stack_enabled', False)
        self._conviction_per_signal = getattr(settings, 'conviction_stack_per_signal', 0.5)
        self._conviction_max = getattr(settings, 'conviction_stack_max', 3.0)

    async def run_once(self, ctx: TradingContext) -> None:
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name)
        if enabled is False:
            return

        all_events = await self._odds_client.fetch_all_sports()
        if not all_events:
            return

        divergences = find_divergences(all_events, min_divergence=self._min_divergence)
        if not divergences:
            log.debug("cv_no_divergences", events_checked=len(all_events))
            return

        log.info("cv_divergences_found", count=len(divergences),
                 events_checked=len(all_events))

        now = datetime.now(timezone.utc)
        price_cache = ctx.scanner.get_all_cached_prices()

        for div in divergences:
            event_id = div["event_id"]

            if event_id in self._traded_events:
                elapsed = (now - self._traded_events[event_id]).total_seconds() / 3600
                if elapsed < self._cooldown_hours:
                    continue

            target_name = div["outcome_name"].lower()
            matching_market = None
            for m in price_cache.values():
                q = m.get("question", "").lower()
                if target_name in q:
                    matching_market = m
                    break

            if not matching_market:
                log.debug("cv_no_matching_market", outcome=div["outcome_name"])
                continue

            side = div["side"]
            divergence = abs(div["divergence"])
            buy_price = matching_market["yes_price"] if side == "YES" else (1 - matching_market["yes_price"])

            kelly_fraction = divergence / (1 - buy_price) if buy_price < 1.0 else 0.0

            async with ctx.portfolio_lock:
                state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
                if not state_row:
                    continue
                bankroll = float(state_row["bankroll"])
                kelly_adj = bankroll_kelly_adjustment(
                    bankroll=bankroll, base_kelly=self.kelly_multiplier,
                    post_breaker_until=state_row.get("post_breaker_until"),
                    post_breaker_reduction=ctx.settings.post_breaker_kelly_reduction,
                    survival_threshold=ctx.settings.bankroll_survival_threshold,
                    growth_threshold=ctx.settings.bankroll_growth_threshold,
                )
                size = compute_position_size(
                    bankroll=bankroll, kelly_fraction=kelly_fraction,
                    kelly_mult=kelly_adj, confidence_mult=1.0,
                    max_single_pct=self.max_single_pct,
                    min_trade_size=ctx.settings.min_trade_size)
                if size <= 0:
                    continue

                pid = matching_market["polymarket_id"]

                # Conviction stacking: check if MR has a position on matching market
                if self._conviction_enabled and size > 0 and matching_market:
                    mr_confirms = await ctx.db.fetchval(
                        """SELECT COUNT(*) FROM trades
                           WHERE strategy = 'mean_reversion'
                             AND status IN ('open', 'filled', 'dry_run')
                             AND market_id IN (
                                 SELECT id FROM markets WHERE polymarket_id = $1
                             )""", pid)
                    if mr_confirms and mr_confirms > 0:
                        mult = conviction_multiplier(
                            mr_confirms, self._conviction_per_signal, self._conviction_max)
                        old_size = size
                        size = min(size * mult, bankroll * self.max_single_pct)
                        if size > old_size:
                            log.info("cv_conviction_boost", market=pid,
                                     multiplier=round(mult, 2), old_size=old_size, new_size=size)

                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=0, category_deployed={},
                    circuit_breaker_until=state_row.get("circuit_breaker_until"))
                proposal = TradeProposal(
                    size_usd=size,
                    category=matching_market.get("category", "unknown"),
                    book_depth=matching_market.get("book_depth", 1000.0))
                risk_result = ctx.risk_manager.check(portfolio, proposal,
                                                      max_single_pct=self.max_single_pct)
                if not risk_result.allowed:
                    log.info("cv_risk_rejected", outcome=div["outcome_name"],
                             reason=risk_result.reason)
                    continue

                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                           current_price, volume_24h, book_depth)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (polymarket_id) DO UPDATE SET
                           current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
                       RETURNING id""",
                    pid, matching_market["question"],
                    matching_market.get("category", "unknown"),
                    matching_market.get("resolution_time"),
                    matching_market["yes_price"],
                    matching_market.get("volume_24h"),
                    matching_market.get("book_depth"))

                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                       ensemble_stdev, quant_signals, edge)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    market_id, json.dumps([]),
                    div["consensus_prob"], 0.0,
                    json.dumps({"source": "cross_venue",
                                "sportsbook_consensus": div["consensus_prob"],
                                "polymarket_prob": div["polymarket_prob"]}),
                    divergence)

                token_id = matching_market.get("yes_token_id", "") if side == "YES" else matching_market.get("no_token_id", "")
                result = await ctx.executor.place_order(
                    token_id=token_id, side=side, size_usd=size,
                    price=buy_price, market_id=market_id,
                    analysis_id=analysis_id, strategy=self.name,
                    kelly_inputs={
                        "consensus_prob": div["consensus_prob"],
                        "polymarket_prob": div["polymarket_prob"],
                        "divergence": div["divergence"],
                        "sport": div["sport"],
                        "outcome": div["outcome_name"],
                    },
                    post_only=self._settings.use_maker_orders)
                if not result:
                    continue

            self._traded_events[event_id] = now
            log.info("cv_trade", outcome=div["outcome_name"], side=side,
                     divergence=round(divergence, 4), size=size,
                     consensus=div["consensus_prob"], polymarket=div["polymarket_prob"])
            await ctx.email_notifier.send(
                f"[POLYBOT] Cross-venue: {div['outcome_name']}",
                format_trade_email(event="executed",
                                   market=f"{div['outcome_name']} ({div['sport']})",
                                   side=side, size=size, price=buy_price,
                                   edge=divergence))

        self._traded_events = {
            k: v for k, v in self._traded_events.items()
            if (now - v).total_seconds() / 3600 < self._cooldown_hours * 2
        }
