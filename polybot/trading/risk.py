from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


@dataclass
class PortfolioState:
    bankroll: float
    total_deployed: float
    daily_pnl: float
    open_count: int
    category_deployed: dict[str, float]
    circuit_breaker_until: datetime | None


@dataclass
class TradeProposal:
    size_usd: float
    category: str
    book_depth: float


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str = ""


def bankroll_kelly_adjustment(
    bankroll: float, base_kelly: float, post_breaker_until: datetime | None,
    post_breaker_reduction: float = 0.50, survival_threshold: float = 50.0,
    growth_threshold: float = 500.0,
) -> float:
    mult = base_kelly
    if post_breaker_until and post_breaker_until > datetime.now(timezone.utc):
        return mult * post_breaker_reduction
    if bankroll < survival_threshold:
        return mult * 0.50
    elif bankroll > growth_threshold:
        return mult * 0.85
    return mult


class RiskManager:
    def __init__(self, max_single_pct=0.15, max_total_deployed_pct=0.70,
                 max_per_category_pct=0.25, max_concurrent=12,
                 daily_loss_limit_pct=0.15, circuit_breaker_hours=6,
                 min_trade_size=1.0, book_depth_max_pct=0.10):
        self.max_single_pct = max_single_pct
        self.max_total_deployed_pct = max_total_deployed_pct
        self.max_per_category_pct = max_per_category_pct
        self.max_concurrent = max_concurrent
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.circuit_breaker_hours = circuit_breaker_hours
        self.min_trade_size = min_trade_size
        self.book_depth_max_pct = book_depth_max_pct

    def check(self, state: PortfolioState, proposal: TradeProposal,
              max_single_pct: float | None = None) -> RiskCheckResult:
        effective_max_single = max_single_pct if max_single_pct is not None else self.max_single_pct
        now = datetime.now(timezone.utc)
        if state.circuit_breaker_until and state.circuit_breaker_until > now:
            return RiskCheckResult(False, "circuit_breaker active")
        if state.open_count >= self.max_concurrent:
            return RiskCheckResult(False, "max concurrent positions reached")
        if state.total_deployed + proposal.size_usd > state.bankroll * self.max_total_deployed_pct:
            return RiskCheckResult(False, "total_deployed would exceed limit")
        if proposal.size_usd > state.bankroll * effective_max_single:
            return RiskCheckResult(False, "single position exceeds limit")
        cat_deployed = state.category_deployed.get(proposal.category, 0.0)
        if cat_deployed + proposal.size_usd > state.bankroll * self.max_per_category_pct:
            return RiskCheckResult(False, "category deployment exceeds limit")
        if proposal.size_usd < self.min_trade_size:
            return RiskCheckResult(False, "below min trade size")
        if proposal.size_usd > proposal.book_depth * self.book_depth_max_pct:
            return RiskCheckResult(False, "exceeds book_depth capacity")
        return RiskCheckResult(True)

    def check_circuit_breaker(self, state: PortfolioState) -> tuple[bool, datetime | None]:
        if state.bankroll <= 0:
            until = datetime.now(timezone.utc) + timedelta(hours=self.circuit_breaker_hours)
            return True, until
        loss_pct = abs(state.daily_pnl) / state.bankroll
        if state.daily_pnl < 0 and loss_pct > self.daily_loss_limit_pct:
            until = datetime.now(timezone.utc) + timedelta(hours=self.circuit_breaker_hours)
            return True, until
        return False, None

    async def get_portfolio_state(self, db) -> PortfolioState:
        state = await db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        open_trades = await db.fetch(
            """SELECT t.position_size_usd, m.category
               FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.status IN ('open', 'filled', 'dry_run')""")
        cat_deployed: dict[str, float] = {}
        for t in open_trades:
            cat = t["category"]
            cat_deployed[cat] = cat_deployed.get(cat, 0.0) + float(t["position_size_usd"])
        return PortfolioState(
            bankroll=float(state["bankroll"]),
            total_deployed=float(state["total_deployed"]),
            daily_pnl=float(state["daily_pnl"]),
            open_count=len(open_trades),
            category_deployed=cat_deployed,
            circuit_breaker_until=state.get("circuit_breaker_until"),
        )

    @staticmethod
    def confidence_multiplier(stdev, quant_score, stdev_low, stdev_high,
                              mult_low, mult_mid, mult_high, quant_neg_mult):
        if stdev < stdev_low:
            mult = mult_low
        elif stdev < stdev_high:
            mult = mult_mid
        else:
            mult = mult_high
        if quant_score < -0.3:
            mult *= quant_neg_mult
        return mult
