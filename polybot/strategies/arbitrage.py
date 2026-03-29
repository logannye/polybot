import asyncio
import structlog
from dataclasses import dataclass, field

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
    fee_rate: float = 0.02,
    min_net_edge: float = 0.01,
) -> ArbOpportunity | None:
    """Buy both YES and NO when they sum to < 1.0 (guaranteed profit)."""
    total_cost = yes_price + no_price
    gross_edge = 1.0 - total_cost
    if gross_edge <= 0:
        return None
    # Fee applies to both legs (two purchases)
    fee_cost = fee_rate * (yes_price + no_price)
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
    fee_rate: float = 0.02,
    min_net_edge: float = 0.01,
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

    # --- Overpriced: buy all NOs ---
    # Cost to buy NO on market i = (1 - yes_price_i)  i.e. no_price
    # Payout: exactly (N-1) outcomes are NO, each paying $1 → total = N-1
    no_cost = sum(1.0 - m["yes_price"] for m in markets)
    no_payout = float(n - 1)
    no_profit = no_payout - no_cost          # absolute gross profit
    no_gross_edge = no_profit                # absolute dollars of edge
    no_fee = fee_rate * no_cost
    no_net_edge = (no_profit - no_fee) / no_cost if no_cost > 0 else 0.0

    # --- Underpriced: buy all YESes ---
    # Cost = yes_sum, payout = 1.0 (exactly one YES wins)
    yes_profit = 1.0 - yes_sum
    yes_gross_edge = yes_profit              # absolute dollars of edge
    yes_fee = fee_rate * yes_sum
    yes_net_edge = (yes_profit - yes_fee) / yes_sum if yes_sum > 0 else 0.0

    # Pick the better opportunity, if any clears the min_net_edge bar
    best: ArbOpportunity | None = None

    if no_net_edge >= min_net_edge:
        best = ArbOpportunity(
            arb_type="exhaustive",
            side="NO",
            gross_edge=no_gross_edge,
            net_edge=no_net_edge,
            markets=list(markets),
        )

    if yes_net_edge >= min_net_edge:
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
        self._settings = settings

    async def run_once(self, ctx: TradingContext) -> None:
        scanner = ctx.scanner
        markets = await scanner.fetch_markets()
        if not markets:
            log.info("arb_no_markets")
            return

        opportunities: list[ArbOpportunity] = []

        # 1. Complement arb — check every market individually
        for m in markets:
            opp = detect_complement_arb(
                polymarket_id=m["polymarket_id"],
                yes_price=m["yes_price"],
                no_price=m["no_price"],
                fee_rate=getattr(self._settings, "fee_rate", 0.02),
            )
            if opp:
                log.info("complement_arb_found", market=m["polymarket_id"],
                         gross_edge=opp.gross_edge, net_edge=opp.net_edge)
                opportunities.append(opp)

        # 2. Exhaustive arb — check grouped (multi-outcome) markets
        groups = scanner.fetch_grouped_markets(markets)
        for slug, group_markets in groups.items():
            opp = detect_exhaustive_arb(
                group_markets,
                fee_rate=getattr(self._settings, "fee_rate", 0.02),
            )
            if opp:
                log.info("exhaustive_arb_found", group=slug, side=opp.side,
                         gross_edge=opp.gross_edge, net_edge=opp.net_edge)
                opportunities.append(opp)

        for opp in opportunities:
            await self._execute_arb(opp, ctx)

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

            legs = self._build_legs(opp, size_usd)
            if not legs:
                return

            results = await ctx.executor.place_multi_leg_order(legs, strategy=self.name)
            placed = sum(1 for r in results if r is not None)
            log.info("arb_executed", arb_type=opp.arb_type, side=opp.side,
                     legs=len(legs), placed=placed, size_usd=size_usd)
            await ctx.email_notifier.send(
                f"[POLYBOT] Trade executed: arb ({opp.arb_type})",
                format_trade_email(event="executed", market=f"Arb: {opp.arb_type}", side=opp.side,
                                   size=size_usd, price=0.0, edge=opp.net_edge))

    def _build_legs(self, opp: ArbOpportunity, size_usd: float) -> list[dict]:
        legs: list[dict] = []
        if opp.arb_type == "complement":
            m = opp.markets[0]
            legs = [
                {
                    "token_id": m.get("yes_token_id", ""),
                    "side": "YES",
                    "size_usd": size_usd / 2,
                    "price": m["yes_price"],
                    "market_id": m["polymarket_id"],
                    "analysis_id": None,
                },
                {
                    "token_id": m.get("no_token_id", ""),
                    "side": "NO",
                    "size_usd": size_usd / 2,
                    "price": m["no_price"],
                    "market_id": m["polymarket_id"],
                    "analysis_id": None,
                },
            ]
        elif opp.arb_type == "exhaustive":
            size_per_leg = size_usd / len(opp.markets)
            for m in opp.markets:
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
                    "market_id": m["polymarket_id"],
                    "analysis_id": None,
                })
        return legs
