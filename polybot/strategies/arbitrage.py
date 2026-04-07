import asyncio
import json
import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import TradeProposal, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


@dataclass
class ArbOpportunity:
    arb_type: str          # "complement", "exhaustive", "temporal"
    side: str              # "YES", "NO", or "BOTH" for complement
    gross_edge: float
    net_edge: float
    markets: list[dict]    # list of market dicts involved


def detect_complement_arb(
    polymarket_id: str,
    yes_price: float,
    no_price: float,
    fee_rate: float = 0.04,
    min_net_edge: float = 0.01,
    is_maker: bool = True,
) -> ArbOpportunity | None:
    """Buy both YES and NO when they sum to < 1.0 (guaranteed profit)."""
    total_cost = yes_price + no_price
    gross_edge = 1.0 - total_cost
    if gross_edge <= 0:
        return None
    if is_maker:
        fee_cost = 0.0
    else:
        # Actual Polymarket fee: feeRate * p * (1-p) per share on each leg
        yes_fee = fee_rate * yes_price * (1.0 - yes_price)
        no_fee = fee_rate * no_price * (1.0 - no_price)
        fee_cost = yes_fee + no_fee
    net_edge = gross_edge - fee_cost
    if net_edge < min_net_edge:
        return None
    return ArbOpportunity(
        arb_type="complement",
        side="BOTH",
        gross_edge=gross_edge,
        net_edge=net_edge,
        markets=[{"polymarket_id": polymarket_id, "yes_price": yes_price, "no_price": no_price}],
    )


def detect_exhaustive_arb(
    markets: list[dict],
    fee_rate: float = 0.04,
    min_net_edge: float = 0.01,
    max_net_edge: float = 0.20,
    is_maker: bool = True,
) -> ArbOpportunity | None:
    """
    Exhaustive (mutually exclusive + collectively exhaustive) group arb.

    Exactly one outcome pays $1. So:
      - Buy all YESes: cost = sum(yes_i), payout = 1.0  → profit if sum < 1
      - Buy all NOs:   cost = sum(1 - yes_i) per market, but exactly (N-1)
                       of them pay $1 → payout = N-1
    """
    n = len(markets)
    if n < 2:
        return None

    yes_sum = sum(m["yes_price"] for m in markets)

    # Sanity check: truly exhaustive groups should have probabilities summing
    # near 1.0 (with some overround for fees). Reject groups that are clearly
    # not exhaustive — cosmetic slug groupings often include unrelated markets.
    if yes_sum < 0.85 or yes_sum > 1.15:
        return None

    # --- Overpriced: buy all NOs ---
    no_cost = sum(1.0 - m["yes_price"] for m in markets)
    no_payout = float(n - 1)
    no_profit = no_payout - no_cost
    no_gross_edge = no_profit
    if is_maker:
        no_fee = 0.0
    else:
        # Actual fee per leg: feeRate * p * (1-p) where p = no_price = 1 - yes_price
        no_fee = sum(fee_rate * (1.0 - m["yes_price"]) * m["yes_price"] for m in markets)
    no_net_edge = (no_profit - no_fee) / no_cost if no_cost > 0 else 0.0

    # --- Underpriced: buy all YESes ---
    yes_profit = 1.0 - yes_sum
    yes_gross_edge = yes_profit
    if is_maker:
        yes_fee = 0.0
    else:
        yes_fee = sum(fee_rate * m["yes_price"] * (1.0 - m["yes_price"]) for m in markets)
    yes_net_edge = (yes_profit - yes_fee) / yes_sum if yes_sum > 0 else 0.0

    # Pick the better opportunity, if any clears the min_net_edge bar
    # Cap: edges above max_net_edge are almost certainly data quality issues
    best: ArbOpportunity | None = None

    if no_net_edge >= min_net_edge and no_net_edge <= max_net_edge:
        best = ArbOpportunity(
            arb_type="exhaustive",
            side="NO",
            gross_edge=no_gross_edge,
            net_edge=no_net_edge,
            markets=list(markets),
        )

    if yes_net_edge >= min_net_edge and yes_net_edge <= max_net_edge:
        candidate = ArbOpportunity(
            arb_type="exhaustive",
            side="YES",
            gross_edge=yes_gross_edge,
            net_edge=yes_net_edge,
            markets=list(markets),
        )
        if best is None or candidate.net_edge > best.net_edge:
            best = candidate

    return best


def detect_temporal_arb(
    earlier_question: str,
    earlier_price: float,
    later_question: str,
    later_price: float,
) -> bool:
    """
    Returns True when a temporal mispricing exists.

    A shorter deadline event must be <= the longer deadline event in probability
    (if X happens by June, it must also happen by July). If earlier_price >
    later_price the market is inverted and an arb exists.
    """
    return earlier_price > later_price


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, settings):
        self.interval_seconds = float(getattr(settings, "arb_interval_seconds", 60.0))
        self.kelly_multiplier = float(getattr(settings, "arb_kelly_multiplier", 0.20))
        self.max_single_pct = float(getattr(settings, "arb_max_single_pct", 0.40))
        self._seen_arbs: set[str] = set()
        self._dedup_loaded: bool = False
        self._settings = settings
        self._min_bankroll = float(getattr(settings, "arb_min_bankroll", 2000.0))

    async def run_once(self, ctx: TradingContext) -> None:
        # Check if this strategy is enabled in DB
        enabled_row = await ctx.db.fetchrow(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name,
        )
        if enabled_row and not enabled_row["enabled"]:
            log.debug("strategy_skipped", strategy=self.name, reason="strategy_disabled")
            return

        # Bankroll gate: don't lock capital in arb at small bankrolls
        state = await ctx.db.fetchrow("SELECT bankroll FROM system_state WHERE id = 1")
        if state and float(state["bankroll"]) < self._min_bankroll:
            log.debug("arb_bankroll_gate", bankroll=float(state["bankroll"]),
                      min_required=self._min_bankroll)
            return

        # Per-strategy position cap: reserve slots for forecast/snipe
        arb_max = int(getattr(self._settings, "arb_max_concurrent", 8))
        arb_open = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = 'arbitrage' AND status IN ('open', 'dry_run', 'filled')")
        if arb_open and arb_open >= arb_max:
            log.debug("arb_position_cap", open=arb_open, max=arb_max)
            return

        # Warm dedup cache from recent DB trades (once per process lifetime)
        if not self._dedup_loaded:
            recent = await ctx.db.fetch(
                """SELECT DISTINCT m.polymarket_id
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.strategy = 'arbitrage'
                     AND t.opened_at > NOW() - INTERVAL '24 hours'
                     AND t.status IN ('open', 'filled', 'dry_run')""")
            for r in recent:
                self._seen_arbs.add(r["polymarket_id"])
            self._dedup_loaded = True
            if self._seen_arbs:
                log.info("arb_dedup_loaded", count=len(self._seen_arbs))

        scanner = ctx.scanner
        markets = await scanner.fetch_markets()
        if not markets:
            log.info("arb_no_markets")
            return

        opportunities: list[ArbOpportunity] = []

        # 1. Complement arb — check every market individually
        for m in markets:
            from polybot.trading.fees import get_fee_rate as _get_fee_rate
            category = m.get("category", "unknown")
            opp = detect_complement_arb(
                polymarket_id=m["polymarket_id"],
                yes_price=m["yes_price"],
                no_price=m["no_price"],
                fee_rate=_get_fee_rate(category),
                is_maker=self._settings.use_maker_orders,
            )
            if opp:
                log.info("complement_arb_found", market=m["polymarket_id"],
                         gross_edge=opp.gross_edge, net_edge=opp.net_edge)
                opportunities.append(opp)

        # 2. Exhaustive arb — check grouped (multi-outcome) markets
        groups = scanner.fetch_event_groups(markets)
        # Filter out groups with illiquid legs
        arb_min_leg_liquidity = float(getattr(self._settings, "arb_min_leg_liquidity", 5000.0))
        for slug, group_markets in groups.items():
            # Skip groups with any illiquid leg
            if any(m.get("book_depth", 0) < arb_min_leg_liquidity for m in group_markets):
                continue
            # Use category from first market in group for fee rate
            _cat = group_markets[0].get("category", "unknown") if group_markets else "unknown"
            opp = detect_exhaustive_arb(
                group_markets,
                fee_rate=_get_fee_rate(_cat),
                max_net_edge=float(getattr(self._settings, "arb_max_net_edge", 0.20)),
                is_maker=self._settings.use_maker_orders,
            )
            if opp:
                log.info("exhaustive_arb_found", group=slug, side=opp.side,
                         gross_edge=opp.gross_edge, net_edge=opp.net_edge)
                opportunities.append(opp)

        # Deduplicate: only process new arb opportunities not seen recently
        new_opps = []
        for opp in opportunities:
            market_ids = [m["polymarket_id"] for m in opp.markets]
            if any(mid in self._seen_arbs for mid in market_ids):
                continue
            # Check DB for recent arb trades on any of the involved markets
            recent_count = await ctx.db.fetchval(
                """SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.strategy = 'arbitrage'
                     AND m.polymarket_id = ANY($1)
                     AND t.opened_at > NOW() - INTERVAL '24 hours'
                     AND t.status IN ('open', 'filled', 'dry_run')""",
                market_ids)
            if recent_count and recent_count > 0:
                self._seen_arbs.update(market_ids)
                continue
            new_opps.append((market_ids, opp))

        if opportunities and not new_opps:
            log.debug("arb_all_known", total=len(opportunities))

        # Re-fetch open count and enforce cap including leg count
        arb_open = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = 'arbitrage' AND status IN ('open', 'dry_run', 'filled')")
        arb_open = arb_open or 0

        for market_ids, opp in new_opps:
            num_legs = len(opp.markets) if opp.arb_type == "exhaustive" else (2 if opp.arb_type == "complement" else 1)
            if arb_open + num_legs > arb_max:
                log.info("arb_would_exceed_cap", open=arb_open, legs=num_legs,
                         max=arb_max, arb_type=opp.arb_type)
                continue
            self._seen_arbs.update(market_ids)
            await self._execute_arb(opp, ctx)
            arb_open += num_legs

    async def _execute_arb(self, opp: ArbOpportunity, ctx: TradingContext) -> None:
        async with ctx.portfolio_lock:
            state = await ctx.risk_manager.get_portfolio_state(ctx.db)
            bankroll = state.bankroll

            adjusted_kelly = bankroll_kelly_adjustment(
                bankroll=bankroll,
                base_kelly=self.kelly_multiplier,
                post_breaker_until=state.circuit_breaker_until,
                post_breaker_reduction=getattr(ctx.settings, "post_breaker_kelly_reduction", 0.50),
                survival_threshold=getattr(ctx.settings, "bankroll_survival_threshold", 50.0),
                growth_threshold=getattr(ctx.settings, "bankroll_growth_threshold", 500.0),
            )

            # Use net_edge as a proxy for kelly fraction (guaranteed-profit arb)
            size_usd = compute_position_size(
                bankroll=bankroll,
                kelly_fraction=opp.net_edge,
                kelly_mult=adjusted_kelly,
                max_single_pct=self.max_single_pct,
            )
            if size_usd <= 0:
                log.info("arb_size_zero", arb_type=opp.arb_type)
                return

            proposal = TradeProposal(
                size_usd=size_usd,
                category="arbitrage",
                book_depth=size_usd * 20,  # conservative — assume deep enough
            )
            check = ctx.risk_manager.check(
                state, proposal, max_single_pct=self.max_single_pct
            )
            if not check.allowed:
                log.info("arb_risk_blocked", reason=check.reason)
                return

            legs = await self._build_legs(opp, size_usd, ctx)
            if not legs:
                return

            results = await ctx.executor.place_multi_leg_order(
                legs, strategy=self.name,
                kelly_inputs={
                    "arb_type": opp.arb_type,
                    "gross_edge": round(opp.gross_edge, 4),
                    "net_edge": round(opp.net_edge, 4),
                    "num_legs": len(legs),
                },
                post_only=self._settings.use_maker_orders)
            placed = sum(1 for r in results if r is not None)
            log.info("arb_executed", arb_type=opp.arb_type, side=opp.side,
                     legs=len(legs), placed=placed, size_usd=size_usd)
            await ctx.email_notifier.send(
                f"[POLYBOT] Trade executed: arb ({opp.arb_type})",
                format_trade_email(event="executed", market=f"Arb: {opp.arb_type}", side=opp.side,
                                   size=size_usd, price=0.0, edge=opp.net_edge))

    async def _upsert_market(self, m: dict, ctx: TradingContext) -> int:
        return await ctx.db.fetchval(
            """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                   current_price, volume_24h, book_depth)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (polymarket_id) DO UPDATE SET
                   current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
               RETURNING id""",
            m["polymarket_id"], m.get("question", ""), m.get("category", "unknown"),
            m.get("resolution_time", datetime.now(timezone.utc)), m["yes_price"],
            m.get("volume_24h"), m.get("book_depth"),
        )

    async def _create_arb_analysis(self, market_id: int, edge: float, ctx: TradingContext) -> int:
        return await ctx.db.fetchval(
            """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
               ensemble_stdev, quant_signals, edge)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
            market_id, json.dumps([]), 0.0, 0.0, json.dumps({}), edge,
        )

    async def _build_legs(self, opp: ArbOpportunity, size_usd: float, ctx: TradingContext) -> list[dict]:
        legs: list[dict] = []
        if opp.arb_type == "complement":
            m = opp.markets[0]
            market_id = await self._upsert_market(m, ctx)
            analysis_id = await self._create_arb_analysis(market_id, opp.net_edge, ctx)
            legs = [
                {
                    "token_id": m.get("yes_token_id", ""),
                    "side": "YES",
                    "size_usd": size_usd / 2,
                    "price": m["yes_price"],
                    "market_id": market_id,
                    "analysis_id": analysis_id,
                },
                {
                    "token_id": m.get("no_token_id", ""),
                    "side": "NO",
                    "size_usd": size_usd / 2,
                    "price": m["no_price"],
                    "market_id": market_id,
                    "analysis_id": analysis_id,
                },
            ]
        elif opp.arb_type == "exhaustive":
            size_per_leg = size_usd / len(opp.markets)
            for m in opp.markets:
                market_id = await self._upsert_market(m, ctx)
                analysis_id = await self._create_arb_analysis(market_id, opp.net_edge, ctx)
                if opp.side == "NO":
                    price = m.get("no_price", 1.0 - m["yes_price"])
                    token_id = m.get("no_token_id", "")
                else:
                    price = m["yes_price"]
                    token_id = m.get("yes_token_id", "")
                legs.append({
                    "token_id": token_id,
                    "side": opp.side,
                    "size_usd": size_per_leg,
                    "price": price,
                    "market_id": market_id,
                    "analysis_id": analysis_id,
                })
        return legs
