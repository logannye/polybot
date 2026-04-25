"""Snipe v10 — 2-tier resolution-convergence capital recycler (spec §4).

Edge thesis: prediction markets at $0.96+ with a few hours to resolution
haven't converged yet because the last 3 cents of drift are below most
traders' attention threshold. Very high win rate, small edge per trade.
Uncorrelated with Live Sports.

Tiers:
- T0: price ≥ 0.96 / ≤ 12h resolution / no verification / 0.50× Kelly / 10% cap
- T1: price 0.88–0.96 / ≤ 8h resolution / Gemini Flash required / 0.30× / 7%

Previous tiers T2/T3 deleted per spec — edge per trade was below
realistic slippage + fees. Odds-based verification deleted (Odds API
removed in Phase A).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import structlog

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.kelly import compute_position_size
from polybot.analysis.gemini_client import GeminiClient, GeminiResult

log = structlog.get_logger()

Tier = Literal[0, 1]


@dataclass(frozen=True)
class SnipeCandidate:
    polymarket_id: str
    yes_price: float
    hours_remaining: float
    tier: Tier
    side: Literal["YES", "NO"]    # the side we'd BUY (high-probability side)
    buy_price: float              # effective price of that side (yes_price or 1-yes_price)


def classify_snipe(
    yes_price: float, hours_remaining: float, *,
    t0_min_price: float = 0.96, t0_max_hours: float = 12.0,
    t1_min_price: float = 0.88, t1_max_hours: float = 8.0,
) -> Optional[SnipeCandidate]:
    """Classify a market into T0 or T1 (or None). Returns the canonical
    side+buy_price so the strategy doesn't repeat the logic.

    Prices below 0.5 trigger on the NO side mirror (so a market trading
    at 0.04 YES = 0.96 NO, classified as T0 NO).
    """
    if hours_remaining <= 0:
        return None

    # Normalize to "extreme-side price"
    if yes_price >= 0.5:
        extreme_price = yes_price
        side: Literal["YES", "NO"] = "YES"
    else:
        extreme_price = 1.0 - yes_price
        side = "NO"

    if extreme_price >= t0_min_price and hours_remaining <= t0_max_hours:
        return SnipeCandidate(
            polymarket_id="",   # filled by caller
            yes_price=yes_price,
            hours_remaining=hours_remaining,
            tier=0, side=side, buy_price=extreme_price,
        )
    if (t1_min_price <= extreme_price < t0_min_price
            and hours_remaining <= t1_max_hours):
        return SnipeCandidate(
            polymarket_id="",
            yes_price=yes_price,
            hours_remaining=hours_remaining,
            tier=1, side=side, buy_price=extreme_price,
        )
    return None


def compute_net_edge(buy_price: float, fee_per_dollar: float = 0.0) -> float:
    """Binary snipe net edge: (1 - buy_price) - fees."""
    return (1.0 - buy_price) - fee_per_dollar


class ResolutionSnipeStrategy(Strategy):
    name = "snipe"

    def __init__(self, settings, gemini_client: Optional[GeminiClient] = None):
        self._settings = settings
        self._gemini = gemini_client
        self.interval_seconds = float(getattr(settings, "snipe_interval_seconds", 120))
        # T0 and T1 have distinct Kelly fractions per spec §4
        self._t0_kelly = float(getattr(settings, "snipe_t0_kelly_mult", 0.50))
        self._t1_kelly = float(getattr(settings, "snipe_t1_kelly_mult", 0.30))
        self._t0_max_single = float(getattr(settings, "snipe_t0_max_single_pct", 0.10))
        self._t1_max_single = float(getattr(settings, "snipe_t1_max_single_pct", 0.07))
        self._max_concurrent = int(getattr(settings, "snipe_max_concurrent", 3))
        # Backwards-compat for Strategy attributes — use T0 as "nominal"
        self.kelly_multiplier = self._t0_kelly
        self.max_single_pct = self._t0_max_single

    @property
    def _min_book_depth(self) -> float:
        """Live: \\$2K. Dry-run: configurable lower (default \\$500), so the
        observation pipeline sees what fills would look like on tighter
        books. Floor: \\$500 to filter ghost-book noise."""
        if getattr(self._settings, "dry_run", False):
            return max(500.0, float(getattr(
                self._settings, "snipe_min_book_depth_dryrun", 500.0)))
        return float(getattr(self._settings, "snipe_min_book_depth", 2000.0))

    @property
    def _t0_max_hours(self) -> float:
        """Live: 12h per spec §4. Dry-run: configurable longer window
        (default 168h / 7d) so observation captures the long-tail
        near-certain markets that dominate current Polymarket structure.
        Live capital still bounded by the conservative spec ceiling."""
        if getattr(self._settings, "dry_run", False):
            return float(getattr(self._settings, "snipe_t0_max_hours_dryrun", 168.0))
        return float(getattr(self._settings, "snipe_t0_max_hours", 12.0))

    @property
    def _t1_max_hours(self) -> float:
        if getattr(self._settings, "dry_run", False):
            return float(getattr(self._settings, "snipe_t1_max_hours_dryrun", 168.0))
        return float(getattr(self._settings, "snipe_t1_max_hours", 8.0))

    async def run_once(self, ctx: TradingContext) -> None:
        # Enabled?
        if not getattr(self._settings, "snipe_enabled", True):
            return

        # Max concurrent gate
        open_count = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = 'snipe' "
            "AND status IN ('open', 'filled', 'dry_run')")
        if open_count and open_count >= self._max_concurrent:
            log.info("snipe_max_concurrent_reached",
                     open_count=open_count, max=self._max_concurrent)
            return

        # Fetch markets
        try:
            all_markets = await ctx.scanner.fetch_markets()
        except Exception as e:
            log.error("snipe_fetch_markets_error", error=str(e))
            return

        now = datetime.now(timezone.utc)
        # Telemetry counters per cycle
        n_no_resolution = 0
        n_past_resolution = 0
        n_classify_none = 0
        n_depth_below = 0
        n_dup = 0
        n_t1_llm_rejected = 0
        n_candidates_t0 = 0
        n_candidates_t1 = 0
        n_entered = 0
        for market in all_markets:
            resolution_time = market.get("resolution_time")
            if not resolution_time:
                n_no_resolution += 1
                continue
            hours_remaining = (resolution_time - now).total_seconds() / 3600.0
            if hours_remaining <= 0:
                n_past_resolution += 1
                continue

            yes_price = float(market.get("yes_price") or 0.5)
            candidate = classify_snipe(
                yes_price, hours_remaining,
                t0_max_hours=self._t0_max_hours,
                t1_max_hours=self._t1_max_hours)
            if candidate is None:
                n_classify_none += 1
                continue
            # Fill in the polymarket_id (classify_snipe doesn't see it)
            candidate = SnipeCandidate(
                polymarket_id=market["polymarket_id"],
                yes_price=candidate.yes_price,
                hours_remaining=candidate.hours_remaining,
                tier=candidate.tier, side=candidate.side,
                buy_price=candidate.buy_price,
            )
            if candidate.tier == 0:
                n_candidates_t0 += 1
            else:
                n_candidates_t1 += 1

            # Book depth
            if float(market.get("book_depth", 0)) < self._min_book_depth:
                n_depth_below += 1
                continue

            # Dedup — max_entries_per_market gate
            existing = await ctx.db.fetchval(
                "SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id "
                "WHERE m.polymarket_id = $1 AND t.strategy='snipe' "
                "AND t.status IN ('open', 'filled', 'dry_run')",
                candidate.polymarket_id)
            if existing and existing > 0:
                n_dup += 1
                continue

            # T1 requires LLM verification
            if candidate.tier == 1:
                verified = await self._verify_via_gemini(candidate, market)
                if not verified:
                    n_t1_llm_rejected += 1
                    log.debug("snipe_t1_rejected_by_llm",
                              market=candidate.polymarket_id)
                    continue

            await self._enter(ctx, market, candidate)
            n_entered += 1

        log.info("snipe_cycle",
                 markets_scanned=len(all_markets),
                 candidates_t0=n_candidates_t0, candidates_t1=n_candidates_t1,
                 entered=n_entered, depth_below=n_depth_below, dup=n_dup,
                 t1_llm_rejected=n_t1_llm_rejected,
                 no_resolution=n_no_resolution, past_resolution=n_past_resolution,
                 t0_max_hours=self._t0_max_hours,
                 min_depth=self._min_book_depth)

    async def _verify_via_gemini(self, candidate: SnipeCandidate, market: dict) -> bool:
        if self._gemini is None:
            return False
        if not self._gemini.can_spend():
            log.info("snipe_gemini_daily_cap_hit",
                     spend_usd=round(self._gemini.current_spend(), 4))
            return False
        min_conf = float(getattr(self._settings, "snipe_t1_min_confidence", 0.85))
        try:
            result = await self._gemini.verify_snipe(
                question=market.get("question", ""),
                resolution_time_iso=str(market.get("resolution_time", "")),
                hours_remaining=candidate.hours_remaining,
                yes_price=candidate.yes_price,
            )
        except Exception as e:
            log.error("snipe_gemini_error", error=str(e))
            return False
        expected_verdict = "YES_LOCKED" if candidate.side == "YES" else "NO_LOCKED"
        return result.verdict == expected_verdict and result.confidence >= min_conf

    async def _enter(self, ctx: TradingContext, market: dict,
                      candidate: SnipeCandidate) -> None:
        net_edge = compute_net_edge(candidate.buy_price)
        min_edge = float(getattr(self._settings, "snipe_min_net_edge", 0.02))
        if net_edge < min_edge:
            return

        state = await ctx.db.fetchrow("SELECT bankroll FROM system_state WHERE id = 1")
        if not state:
            return
        bankroll = float(state["bankroll"])

        kelly_mult = self._t0_kelly if candidate.tier == 0 else self._t1_kelly
        max_single_pct = self._t0_max_single if candidate.tier == 0 else self._t1_max_single

        # Binary Kelly fraction = edge / odds_against. For buy_price p,
        # odds_against = (1-p)/p. Kelly ≈ edge × p / (1-p) — simplified to
        # compute_position_size below which handles the common case.
        kelly_fraction = net_edge / (1.0 - candidate.buy_price) if candidate.buy_price < 1.0 else 0.0
        size = compute_position_size(
            bankroll=bankroll,
            kelly_fraction=kelly_fraction,
            kelly_mult=kelly_mult,
            confidence_mult=1.0,
            max_single_pct=max_single_pct,
            min_trade_size=float(getattr(self._settings, "min_trade_size", 1.0)))
        if size <= 0:
            return

        market_id = await ctx.db.fetchval(
            """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                   current_price, volume_24h, book_depth)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (polymarket_id) DO UPDATE SET
                   current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
               RETURNING id""",
            candidate.polymarket_id, market.get("question", ""),
            market.get("category", "unknown"), market.get("resolution_time"),
            candidate.yes_price, float(market.get("volume_24h", 0)),
            float(market.get("book_depth", 0)))

        token_id = market.get("yes_token_id") if candidate.side == "YES" \
            else market.get("no_token_id")
        if not token_id:
            return

        log.info("snipe_entry",
                 market=candidate.polymarket_id, tier=candidate.tier,
                 side=candidate.side, size=round(size, 2),
                 price=round(candidate.buy_price, 4),
                 edge=round(net_edge, 4),
                 hours_remaining=round(candidate.hours_remaining, 1))

        async with ctx.portfolio_lock:
            await ctx.executor.place_order(
                token_id=token_id, side=candidate.side,
                size_usd=size, price=candidate.buy_price,
                market_id=market_id, analysis_id=None, strategy=self.name,
                kelly_inputs={
                    "tier": candidate.tier,
                    "buy_price": round(candidate.buy_price, 4),
                    "hours_remaining": round(candidate.hours_remaining, 2),
                    "net_edge": round(net_edge, 4),
                    "kelly_fraction": round(kelly_fraction, 4),
                },
                post_only=True)   # maker-first — T0/T1 both prefer 0% fees
