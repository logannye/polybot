import pytest
from polybot.trading.kelly import compute_kelly, compute_position_size


class TestComputeKelly:
    def test_positive_edge_yes_bet(self):
        result = compute_kelly(ensemble_prob=0.60, market_price=0.50)
        assert result.edge == pytest.approx(0.10)
        assert result.side == "YES"
        assert result.kelly_fraction == pytest.approx(0.10 / (1 - 0.50))
        assert result.odds == pytest.approx((1 / 0.50) - 1)

    def test_positive_edge_no_bet(self):
        result = compute_kelly(ensemble_prob=0.30, market_price=0.50)
        assert result.side == "NO"
        assert result.edge == pytest.approx(0.20)

    def test_no_edge(self):
        result = compute_kelly(ensemble_prob=0.50, market_price=0.50)
        assert result.edge == pytest.approx(0.0)
        assert result.kelly_fraction == pytest.approx(0.0)

    def test_tiny_edge_below_threshold(self):
        result = compute_kelly(ensemble_prob=0.52, market_price=0.50)
        assert result.edge == pytest.approx(0.02)


class TestComputePositionSize:
    def test_basic_sizing(self):
        size = compute_position_size(bankroll=300.0, kelly_fraction=0.20, kelly_mult=0.25, confidence_mult=1.0)
        assert size == pytest.approx(15.0)

    def test_confidence_reduces_size(self):
        size = compute_position_size(bankroll=300.0, kelly_fraction=0.20, kelly_mult=0.25, confidence_mult=0.7)
        assert size == pytest.approx(10.5)

    def test_max_position_cap(self):
        size = compute_position_size(bankroll=300.0, kelly_fraction=0.80, kelly_mult=0.50, confidence_mult=1.0, max_single_pct=0.15)
        assert size == pytest.approx(45.0)

    def test_below_minimum_returns_zero(self):
        size = compute_position_size(bankroll=300.0, kelly_fraction=0.01, kelly_mult=0.25, confidence_mult=0.4, min_trade_size=2.0)
        assert size == 0.0

    def test_negative_kelly_returns_zero(self):
        size = compute_position_size(bankroll=300.0, kelly_fraction=-0.10, kelly_mult=0.25, confidence_mult=1.0)
        assert size == 0.0
