import pytest
from datetime import datetime, timezone, timedelta
from polybot.trading.risk import RiskManager, PortfolioState, TradeProposal


@pytest.fixture
def risk_mgr():
    return RiskManager(
        max_single_pct=0.15, max_total_deployed_pct=0.50, max_per_category_pct=0.25,
        max_concurrent=8, daily_loss_limit_pct=0.20, circuit_breaker_hours=12,
        min_trade_size=2.0, book_depth_max_pct=0.10,
    )


class TestPortfolioCheck:
    def test_trade_passes_all_checks(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=50.0, daily_pnl=0.0, open_count=2, category_deployed={"politics": 10.0}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=20.0, category="politics", book_depth=500.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is True

    def test_rejects_exceeds_max_deployed(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=140.0, daily_pnl=0.0, open_count=2, category_deployed={}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=20.0, category="crypto", book_depth=500.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is False
        assert "total_deployed" in result.reason

    def test_rejects_exceeds_category_limit(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=30.0, daily_pnl=0.0, open_count=1, category_deployed={"politics": 70.0}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=10.0, category="politics", book_depth=500.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is False
        assert "category" in result.reason

    def test_rejects_max_concurrent(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=100.0, daily_pnl=0.0, open_count=8, category_deployed={}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=10.0, category="crypto", book_depth=500.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is False
        assert "concurrent" in result.reason

    def test_rejects_circuit_breaker_active(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=0.0, daily_pnl=-70.0, open_count=0, category_deployed={}, circuit_breaker_until=datetime.now(timezone.utc) + timedelta(hours=6))
        proposal = TradeProposal(size_usd=10.0, category="crypto", book_depth=500.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is False
        assert "circuit_breaker" in result.reason

    def test_rejects_book_depth_exceeded(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=0.0, daily_pnl=0.0, open_count=0, category_deployed={}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=20.0, category="crypto", book_depth=100.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is False
        assert "book_depth" in result.reason


class TestCircuitBreaker:
    def test_triggers_on_daily_loss(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=50.0, daily_pnl=-65.0, open_count=2, category_deployed={}, circuit_breaker_until=None)
        triggered, until = risk_mgr.check_circuit_breaker(state)
        assert triggered is True
        assert until > datetime.now(timezone.utc)

    def test_no_trigger_within_limit(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=50.0, daily_pnl=-30.0, open_count=2, category_deployed={}, circuit_breaker_until=None)
        triggered, until = risk_mgr.check_circuit_breaker(state)
        assert triggered is False
        assert until is None


class TestConfidenceMultiplier:
    def test_low_stdev(self, risk_mgr):
        mult = risk_mgr.confidence_multiplier(stdev=0.03, quant_score=0.5, stdev_low=0.05, stdev_high=0.12, mult_low=1.0, mult_mid=0.7, mult_high=0.4, quant_neg_mult=0.75)
        assert mult == pytest.approx(1.0)

    def test_mid_stdev(self, risk_mgr):
        mult = risk_mgr.confidence_multiplier(stdev=0.08, quant_score=0.5, stdev_low=0.05, stdev_high=0.12, mult_low=1.0, mult_mid=0.7, mult_high=0.4, quant_neg_mult=0.75)
        assert mult == pytest.approx(0.7)

    def test_high_stdev_negative_quant(self, risk_mgr):
        mult = risk_mgr.confidence_multiplier(stdev=0.15, quant_score=-0.5, stdev_low=0.05, stdev_high=0.12, mult_low=1.0, mult_mid=0.7, mult_high=0.4, quant_neg_mult=0.75)
        assert mult == pytest.approx(0.30)
