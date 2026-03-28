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


class RiskManager:
    def __init__(self, max_single_pct=0.15, max_total_deployed_pct=0.50, max_per_category_pct=0.25,
                 max_concurrent=8, daily_loss_limit_pct=0.20, circuit_breaker_hours=12,
                 min_trade_size=2.0, book_depth_max_pct=0.10):
        self.max_single_pct = max_single_pct
        self.max_total_deployed_pct = max_total_deployed_pct
        self.max_per_category_pct = max_per_category_pct
        self.max_concurrent = max_concurrent
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.circuit_breaker_hours = circuit_breaker_hours
        self.min_trade_size = min_trade_size
        self.book_depth_max_pct = book_depth_max_pct

    def check(self, state: PortfolioState, proposal: TradeProposal) -> RiskCheckResult:
        now = datetime.now(timezone.utc)
        if state.circuit_breaker_until and state.circuit_breaker_until > now:
            return RiskCheckResult(False, "circuit_breaker active")
        if state.open_count >= self.max_concurrent:
            return RiskCheckResult(False, "max concurrent positions reached")
        if state.total_deployed + proposal.size_usd > state.bankroll * self.max_total_deployed_pct:
            return RiskCheckResult(False, "total_deployed would exceed limit")
        if proposal.size_usd > state.bankroll * self.max_single_pct:
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

    @staticmethod
    def confidence_multiplier(stdev, quant_score, stdev_low, stdev_high, mult_low, mult_mid, mult_high, quant_neg_mult):
        if stdev < stdev_low:
            mult = mult_low
        elif stdev < stdev_high:
            mult = mult_mid
        else:
            mult = mult_high
        if quant_score < -0.3:
            mult *= quant_neg_mult
        return mult
