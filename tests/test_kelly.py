import pytest
from polybot.trading.kelly import compute_kelly, compute_position_size


class TestComputeKelly:
    def test_positive_edge_yes_bet(self):
        result = compute_kelly(ensemble_prob=0.60, market_price=0.50)
        # gross_edge = 0.10, fee = 0.02 * 0.60 = 0.012, net_edge = 0.088
        assert result.edge == pytest.approx(0.088)
        assert result.side == "YES"
        assert result.kelly_fraction == pytest.approx(0.088 / (1 - 0.50))
        assert result.odds == pytest.approx((1 / 0.50) - 1)

    def test_positive_edge_no_bet(self):
        result = compute_kelly(ensemble_prob=0.30, market_price=0.50)
        assert result.side == "NO"
        # gross_edge = 0.20, fee = 0.02 * 0.70 = 0.014, net_edge = 0.186
        assert result.edge == pytest.approx(0.186)

    def test_no_edge(self):
        result = compute_kelly(ensemble_prob=0.50, market_price=0.50)
        assert result.edge == pytest.approx(0.0)
        assert result.kelly_fraction == pytest.approx(0.0)

    def test_tiny_edge_below_threshold(self):
        result = compute_kelly(ensemble_prob=0.52, market_price=0.50)
        # gross_edge = 0.02, fee = 0.02 * 0.52 = 0.0104, net_edge = 0.0096
        assert result.edge == pytest.approx(0.0096)


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


def test_fee_adjusted_edge_reduces_edge():
    from polybot.trading.kelly import compute_kelly
    result_no_fee = compute_kelly(0.80, 0.70, fee_rate=0.0)
    result_with_fee = compute_kelly(0.80, 0.70, fee_rate=0.02)
    assert result_with_fee.edge < result_no_fee.edge
    assert result_with_fee.edge > 0


def test_fee_kills_marginal_edge():
    from polybot.trading.kelly import compute_kelly
    result = compute_kelly(0.51, 0.50, fee_rate=0.02)
    assert result.edge == 0.0
    assert result.kelly_fraction == 0.0


def test_fee_rate_zero_matches_original():
    from polybot.trading.kelly import compute_kelly
    result = compute_kelly(0.75, 0.60, fee_rate=0.0)
    assert result.side == "YES"
    assert abs(result.edge - 0.15) < 1e-9


def test_no_side_still_returns_zero():
    from polybot.trading.kelly import compute_kelly
    result = compute_kelly(0.50, 0.50, fee_rate=0.02)
    assert result.edge == 0.0


def test_position_size_no_double_fee():
    from polybot.trading.kelly import compute_position_size
    size = compute_position_size(bankroll=100.0, kelly_fraction=0.10, kelly_mult=0.25,
                                  confidence_mult=1.0, max_single_pct=0.15, min_trade_size=1.0)
    assert abs(size - 2.50) < 0.01
