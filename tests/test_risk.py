import pytest
from datetime import datetime, timezone, timedelta
from polybot.trading.risk import RiskManager, PortfolioState, TradeProposal


@pytest.fixture
def risk_mgr():
    return RiskManager(
        max_single_pct=0.15, max_total_deployed_pct=0.70, max_per_category_pct=0.25,
        max_concurrent=12, daily_loss_limit_pct=0.15, circuit_breaker_hours=6,
        min_trade_size=1.0, book_depth_max_pct=0.10,
    )


class TestPortfolioCheck:
    def test_trade_passes_all_checks(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=50.0, daily_pnl=0.0, open_count=2, category_deployed={"politics": 10.0}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=20.0, category="politics", book_depth=500.0)
        result = risk_mgr.check(state, proposal)
        assert result.allowed is True

    def test_rejects_exceeds_max_deployed(self, risk_mgr):
        state = PortfolioState(bankroll=300.0, total_deployed=200.0, daily_pnl=0.0, open_count=2, category_deployed={}, circuit_breaker_until=None)
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
        state = PortfolioState(bankroll=300.0, total_deployed=100.0, daily_pnl=0.0, open_count=12, category_deployed={}, circuit_breaker_until=None)
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


class TestBankrollKellyAdjustment:
    def test_post_breaker_cooldown_reduces_kelly(self):
        from polybot.trading.risk import bankroll_kelly_adjustment
        adj = bankroll_kelly_adjustment(
            bankroll=100.0, base_kelly=0.80,
            post_breaker_until=datetime.now(timezone.utc) + timedelta(hours=12),
            post_breaker_reduction=0.50, survival_threshold=50.0, growth_threshold=500.0)
        assert abs(adj - 0.40) < 1e-9

    def test_survival_mode_halves_kelly(self):
        from polybot.trading.risk import bankroll_kelly_adjustment
        adj = bankroll_kelly_adjustment(
            bankroll=30.0, base_kelly=0.25, post_breaker_until=None,
            post_breaker_reduction=0.50, survival_threshold=50.0, growth_threshold=500.0)
        assert abs(adj - 0.125) < 1e-9

    def test_normal_range_unchanged(self):
        from polybot.trading.risk import bankroll_kelly_adjustment
        adj = bankroll_kelly_adjustment(
            bankroll=100.0, base_kelly=0.25, post_breaker_until=None,
            post_breaker_reduction=0.50, survival_threshold=50.0, growth_threshold=500.0)
        assert abs(adj - 0.25) < 1e-9

    def test_preservation_mode_reduces_kelly(self):
        from polybot.trading.risk import bankroll_kelly_adjustment
        adj = bankroll_kelly_adjustment(
            bankroll=600.0, base_kelly=0.25, post_breaker_until=None,
            post_breaker_reduction=0.50, survival_threshold=50.0, growth_threshold=500.0)
        assert abs(adj - 0.25 * 0.85) < 1e-9


class TestEdgeSkepticismDiscount:
    def test_small_edge_no_discount(self):
        assert RiskManager.edge_skepticism_discount(0.05) == 1.0

    def test_edge_at_threshold(self):
        assert RiskManager.edge_skepticism_discount(0.12) == 1.0

    def test_edge_above_threshold(self):
        result = RiskManager.edge_skepticism_discount(0.15)
        assert 0.5 < result < 1.0

    def test_edge_at_20pct(self):
        result = RiskManager.edge_skepticism_discount(0.20)
        assert result == pytest.approx(1.0 - (0.08 / 0.18) * 0.5, abs=1e-4)

    def test_edge_at_30pct(self):
        assert RiskManager.edge_skepticism_discount(0.30) == pytest.approx(0.5)

    def test_edge_above_30pct(self):
        assert RiskManager.edge_skepticism_discount(0.40) == 0.5

    def test_monotonically_decreasing(self):
        edges = [0.05, 0.12, 0.15, 0.20, 0.25, 0.30, 0.35]
        discounts = [RiskManager.edge_skepticism_discount(e) for e in edges]
        for i in range(len(discounts) - 1):
            assert discounts[i] >= discounts[i + 1]


class TestStrategyAwareRiskLimits:
    def test_risk_check_with_strategy_max_single(self):
        rm = RiskManager()
        state = PortfolioState(bankroll=100.0, total_deployed=0.0, daily_pnl=0.0,
            open_count=0, category_deployed={}, circuit_breaker_until=None)
        proposal = TradeProposal(size_usd=20.0, category="politics", book_depth=1000.0)
        result = rm.check(state, proposal, max_single_pct=0.40)
        assert result.allowed
        result2 = rm.check(state, proposal, max_single_pct=0.15)
        assert not result2.allowed

    def test_updated_risk_defaults(self):
        rm = RiskManager()
        assert rm.max_per_category_pct == 0.50
        assert rm.max_total_deployed_pct == 0.70
        assert rm.max_concurrent == 12
        assert rm.daily_loss_limit_pct == 0.15
        assert rm.circuit_breaker_hours == 6
        assert rm.min_trade_size == 1.0
