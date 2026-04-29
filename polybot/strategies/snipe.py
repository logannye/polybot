"""Snipe v12 — single-tier LLM-verified resolution-arbitrage strategy.

Edge thesis: Polymarket prediction markets at ≥0.96 (or ≤0.04) within
hours of resolution haven't fully converged because the last few cents of
drift sit below most traders' attention threshold. We verify with Gemini
that the market is *mechanically locked* (outcome decided by a known,
verifiable real-world event) and hold to resolution — no exit transaction,
so the spread tax is paid once at entry and bounded by the entry price.

Differences from v10/v11:
- T0 deleted (no LLM verification → 6/214 historical hit rate, structurally
  negative-EV at ≥0.96 entry).
- Single tier with structured-schema verifier and grounding regex.
- Every candidate logged to `shadow_signal` regardless of fill outcome,
  so we can measure verifier accuracy and counterfactual P&L.
- Hit-rate killswitch gates entries (rolling-50 must be ≥97%).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import structlog

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.kelly import compute_position_size
from polybot.analysis.gemini_client import GeminiClient, GeminiResult
from polybot.learning import shadow_log
from polybot.learning import hit_rate_killswitch

log = structlog.get_logger()


@dataclass(frozen=True)
class SnipeCandidate:
    polymarket_id: str
    yes_price: float
    hours_remaining: float
    side: Literal["YES", "NO"]    # the side we'd BUY (high-probability side)
    buy_price: float


def classify_snipe(
    yes_price: float, hours_remaining: float,
    *, min_price: float = 0.96, max_hours: float = 12.0,
) -> Optional[SnipeCandidate]:
    """Classify a market as a snipe candidate, or None.

    Mirrors prices below 0.5 onto the NO side, so a market trading at 0.04
    YES surfaces as a NO snipe at 0.96.
    """
    if hours_remaining <= 0:
        return None
    if yes_price >= 0.5:
        extreme_price, side = yes_price, "YES"
    else:
        extreme_price, side = 1.0 - yes_price, "NO"
    if extreme_price >= min_price and hours_remaining <= max_hours:
        return SnipeCandidate(
            polymarket_id="",
            yes_price=yes_price,
            hours_remaining=hours_remaining,
            side=side,                                                # type: ignore
            buy_price=extreme_price,
        )
    return None


def compute_net_edge(buy_price: float, fee_per_dollar: float = 0.0) -> float:
    """Per-dollar net edge for a binary buy: (1 - buy_price) - fees."""
    return (1.0 - buy_price) - fee_per_dollar


@dataclass(frozen=True)
class SizingTier:
    """Per-trade edge floor + bankroll cap, selected by verifier confidence."""
    name: str            # "high" | "mid" | "low"
    min_edge: float
    max_pct: float


def select_tier(verifier_confidence: float, settings) -> Optional[SizingTier]:
    """Pure-function tier dispatch. Returns None if confidence is below the
    low-tier floor (verdict should be UNCERTAIN already, but defensive).

    The tier assigns BOTH the edge floor (lower for higher confidence — we
    trust the verifier more so we can take thinner edges) AND the bankroll
    cap (lower for higher confidence on thin edges — a thin-edge mistake on
    a bigger position is catastrophic, so we shrink the bet).
    """
    high_conf = float(getattr(settings, "snipe_tier_high_min_conf", 0.99))
    mid_conf = float(getattr(settings, "snipe_tier_mid_min_conf", 0.97))
    low_conf = float(getattr(settings, "snipe_tier_low_min_conf", 0.95))

    if verifier_confidence >= high_conf:
        return SizingTier(
            name="high",
            min_edge=float(getattr(settings, "snipe_tier_high_min_edge", 0.002)),
            max_pct=float(getattr(settings, "snipe_tier_high_max_pct", 0.01)))
    if verifier_confidence >= mid_conf:
        return SizingTier(
            name="mid",
            min_edge=float(getattr(settings, "snipe_tier_mid_min_edge", 0.01)),
            max_pct=float(getattr(settings, "snipe_tier_mid_max_pct", 0.02)))
    if verifier_confidence >= low_conf:
        return SizingTier(
            name="low",
            min_edge=float(getattr(settings, "snipe_tier_low_min_edge", 0.02)),
            max_pct=float(getattr(settings, "snipe_tier_low_max_pct", 0.05)))
    return None


class ResolutionSnipeStrategy(Strategy):
    name = "snipe"

    def __init__(self, settings, gemini_client: Optional[GeminiClient] = None):
        self._settings = settings
        self._gemini = gemini_client
        self.interval_seconds = float(getattr(settings, "snipe_interval_seconds", 120))
        self.kelly_multiplier = float(getattr(settings, "snipe_kelly_mult", 0.25))
        self.max_single_pct = float(getattr(settings, "snipe_max_single_pct", 0.05))
        self._max_concurrent = int(getattr(settings, "snipe_max_concurrent", 4))
        self._min_confidence = float(getattr(settings, "snipe_min_verifier_confidence", 0.95))

    @property
    def _min_book_depth(self) -> float:
        if getattr(self._settings, "dry_run", False):
            return float(getattr(self._settings, "snipe_min_book_depth_dryrun", 500.0))
        return float(getattr(self._settings, "snipe_min_book_depth", 1000.0))

    @property
    def _max_hours(self) -> float:
        if getattr(self._settings, "dry_run", False):
            return float(getattr(self._settings, "snipe_max_hours_dryrun", 168.0))
        return float(getattr(self._settings, "snipe_max_hours", 12.0))

    @property
    def _min_price(self) -> float:
        return float(getattr(self._settings, "snipe_min_price", 0.96))

    async def run_once(self, ctx: TradingContext) -> None:
        if not getattr(self._settings, "snipe_enabled", True):
            return

        # Killswitch gate — hard halt of all entries.
        if await hit_rate_killswitch.is_tripped(ctx.db):
            log.info("snipe_killswitch_tripped_skip")
            return

        # Concurrency gate.
        open_count = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = 'snipe' "
            "AND status IN ('open', 'filled', 'dry_run')")
        if open_count and open_count >= self._max_concurrent:
            log.info("snipe_max_concurrent_reached",
                     open_count=open_count, max=self._max_concurrent)
            return

        # Total-deployed gate (hard cap across all open snipe positions).
        max_deployed_pct = float(getattr(self._settings, "snipe_max_total_deployed_pct", 0.20))
        state = await ctx.db.fetchrow(
            "SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
        if state:
            deployed_pct = float(state["total_deployed"]) / max(float(state["bankroll"]), 1.0)
            if deployed_pct >= max_deployed_pct:
                log.info("snipe_max_deployed_reached",
                         deployed_pct=round(deployed_pct, 3),
                         cap=max_deployed_pct)
                return

        try:
            all_markets = await ctx.scanner.fetch_markets()
        except Exception as e:
            log.error("snipe_fetch_markets_error", error=str(e))
            return

        # v12.4: Build the set of `group_slug` values currently held by an
        # open snipe position. Markets sharing a group_slug are bracket
        # markets on the same news event (e.g. all "Q1 2026 GDP" buckets);
        # opening multiple positions on them creates phantom diversification
        # — one news event drives correlated PnL across all of them.
        held_group_slugs: set[str] = set()
        if getattr(self._settings, "snipe_correlation_filter_enabled", True):
            held_rows = await ctx.db.fetch(
                """SELECT m.polymarket_id FROM trades t
                   JOIN markets m ON m.id = t.market_id
                   WHERE t.strategy='snipe'
                     AND t.status IN ('open','filled','dry_run')""")
            for row in held_rows:
                cached = ctx.scanner.get_cached_price(row["polymarket_id"])
                if cached:
                    g = cached.get("group_slug")
                    if g:
                        held_group_slugs.add(g)

        now = datetime.now(timezone.utc)
        n_classify_none = 0
        n_past_resolution = 0
        n_no_resolution = 0
        n_depth_below = 0
        n_dup = 0
        n_verifier_rejected = 0
        n_signal = 0
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
            cand = classify_snipe(
                yes_price, hours_remaining,
                min_price=self._min_price, max_hours=self._max_hours)
            if cand is None:
                n_classify_none += 1
                continue
            cand = SnipeCandidate(
                polymarket_id=market["polymarket_id"],
                yes_price=cand.yes_price,
                hours_remaining=cand.hours_remaining,
                side=cand.side, buy_price=cand.buy_price,
            )

            book_depth = float(market.get("book_depth", 0))
            depth_ok = book_depth >= self._min_book_depth

            existing = await ctx.db.fetchval(
                "SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id "
                "WHERE m.polymarket_id = $1 AND t.strategy='snipe' "
                "AND t.status IN ('open', 'filled', 'dry_run')",
                cand.polymarket_id)
            already_in = bool(existing and existing > 0)

            # v12.4 correlation gate: same news event already held.
            market_group = market.get("group_slug")
            event_held = bool(market_group and market_group in held_group_slugs)

            # Verify with Gemini regardless of depth/dup gates so the shadow
            # log captures the verifier's read on every candidate.
            verifier_result = await self._verify(cand, market)
            verified = (
                verifier_result.verdict == ("YES_LOCKED" if cand.side == "YES" else "NO_LOCKED")
                and verifier_result.confidence >= self._min_confidence
            )
            if not verified:
                n_verifier_rejected += 1

            # Determine reject reason in priority order.
            reject_reason: Optional[str] = None
            if not depth_ok:
                reject_reason = "depth_below_floor"
            elif already_in:
                reject_reason = "position_already_open"
            elif event_held:
                reject_reason = "event_group_already_held"
            elif not verified:
                reject_reason = (f"verifier:{verifier_result.verdict}@"
                                 f"{verifier_result.confidence:.2f}")

            passes = reject_reason is None

            # Record the shadow signal for every candidate.
            try:
                await shadow_log.record_signal(ctx.db, shadow_log.SignalRecord(
                    polymarket_id=cand.polymarket_id,
                    yes_price=cand.yes_price,
                    hours_remaining=cand.hours_remaining,
                    side=cand.side,
                    buy_price=cand.buy_price,
                    verifier_verdict=verifier_result.verdict,
                    verifier_confidence=verifier_result.confidence,
                    verifier_reason=verifier_result.reason,
                    passed_filter=passes,
                    fill_attempted=False,
                    filled=False,
                    reject_reason=reject_reason,
                ))
                n_signal += 1
            except Exception as e:
                log.error("shadow_log_failed", error=str(e))

            if not depth_ok:
                n_depth_below += 1
                continue
            if already_in:
                n_dup += 1
                continue
            if event_held:
                continue
            if not verified:
                continue

            await self._enter(ctx, market, cand, verifier_result)
            n_entered += 1
            # Reserve this group_slug so the same cycle can't double-enter.
            if market_group:
                held_group_slugs.add(market_group)

        log.info("snipe_cycle",
                 markets_scanned=len(all_markets),
                 signals=n_signal, entered=n_entered,
                 verifier_rejected=n_verifier_rejected,
                 depth_below=n_depth_below, dup=n_dup,
                 no_resolution=n_no_resolution,
                 past_resolution=n_past_resolution,
                 classify_none=n_classify_none,
                 max_hours=self._max_hours,
                 min_depth=self._min_book_depth,
                 min_price=self._min_price)

    async def _verify(self, candidate: SnipeCandidate, market: dict) -> GeminiResult:
        if self._gemini is None:
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                                reason="no_gemini_client")
        if not self._gemini.can_spend():
            log.info("snipe_gemini_daily_cap_hit",
                     spend_usd=round(self._gemini.current_spend(), 4))
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                                reason="daily_spend_cap_hit")
        try:
            return await self._gemini.verify_snipe(
                question=market.get("question", ""),
                resolution_time_iso=str(market.get("resolution_time", "")),
                hours_remaining=candidate.hours_remaining,
                yes_price=candidate.yes_price,
                polymarket_id=candidate.polymarket_id,
            )
        except Exception as e:
            log.error("snipe_gemini_error", error=str(e))
            return GeminiResult(verdict="UNCERTAIN", confidence=0.0,
                                reason=f"exception:{str(e)[:80]}")

    async def _enter(self, ctx: TradingContext, market: dict,
                      candidate: SnipeCandidate, verifier: GeminiResult) -> None:
        # Tier dispatch: pick the (min_edge, max_pct) pair that matches the
        # verifier's confidence. Higher confidence → thinner edge allowed and
        # smaller per-trade cap (so a single false positive can't blow up).
        tier = select_tier(verifier.confidence, self._settings)
        if tier is None:
            return    # below low-tier confidence floor (defensive — verdict
                      # should already be UNCERTAIN at this point)

        net_edge = compute_net_edge(candidate.buy_price)
        if net_edge < tier.min_edge:
            log.debug("snipe_below_tier_edge_floor",
                      tier=tier.name, edge=round(net_edge, 4),
                      floor=tier.min_edge,
                      market=candidate.polymarket_id[:18])
            return

        state = await ctx.db.fetchrow("SELECT bankroll FROM system_state WHERE id = 1")
        if not state:
            return
        bankroll = float(state["bankroll"])

        # Binary Kelly: edge / (1 - p_buy).
        kelly_fraction = (net_edge / (1.0 - candidate.buy_price)
                          if candidate.buy_price < 1.0 else 0.0)
        size = compute_position_size(
            bankroll=bankroll,
            kelly_fraction=kelly_fraction,
            kelly_mult=self.kelly_multiplier,
            confidence_mult=1.0,
            max_single_pct=tier.max_pct,
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

        token_id = (market.get("yes_token_id") if candidate.side == "YES"
                    else market.get("no_token_id"))
        if not token_id:
            return

        log.info("snipe_entry",
                 market=candidate.polymarket_id, side=candidate.side,
                 tier=tier.name, size=round(size, 2),
                 price=round(candidate.buy_price, 4),
                 edge=round(net_edge, 4),
                 hours_remaining=round(candidate.hours_remaining, 1),
                 verifier_confidence=round(verifier.confidence, 3),
                 verifier_reason=verifier.reason[:120])

        async with ctx.portfolio_lock:
            await ctx.executor.place_order(
                token_id=token_id, side=candidate.side,
                size_usd=size, price=candidate.buy_price,
                market_id=market_id, analysis_id=None, strategy=self.name,
                kelly_inputs={
                    "buy_price": round(candidate.buy_price, 4),
                    "hours_remaining": round(candidate.hours_remaining, 2),
                    "net_edge": round(net_edge, 4),
                    "kelly_fraction": round(kelly_fraction, 4),
                    "verifier_confidence": round(verifier.confidence, 3),
                    "verifier_reason": verifier.reason[:200],
                    "tier": tier.name,
                    "tier_max_pct": tier.max_pct,
                },
                post_only=True)
