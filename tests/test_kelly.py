import pytest
from polybot.trading.kelly import compute_kelly, compute_position_size


class TestComputeKelly:
    def test_positive_edge_yes_bet_no_fee(self):
        result = compute_kelly(ensemble_prob=0.60, market_price=0.50)
        # gross_edge = 0.10, fee = 0.0 (maker default), net_edge = 0.10
        assert result.edge == pytest.approx(0.10)
        assert result.side == "YES"
        assert result.kelly_fraction == pytest.approx(0.10 / (1 - 0.50))
        assert result.odds == pytest.approx((1 / 0.50) - 1)

    def test_positive_edge_no_bet_no_fee(self):
        result = compute_kelly(ensemble_prob=0.30, market_price=0.50)
        assert result.side == "NO"
        # gross_edge = 0.20, fee = 0.0, net_edge = 0.20
        assert result.edge == pytest.approx(0.20)

    def test_positive_edge_with_taker_fee(self):
        # fee_per_dollar = 0.02 (e.g. finance at p=0.50)
        result = compute_kelly(ensemble_prob=0.60, market_price=0.50, fee_per_dollar=0.02)
        # gross_edge = 0.10, net_edge = 0.10 - 0.02 = 0.08
        assert result.edge == pytest.approx(0.08)
        assert result.side == "YES"

    def test_no_edge(self):
        result = compute_kelly(ensemble_prob=0.50, market_price=0.50)
        assert result.edge == pytest.approx(0.0)
        assert result.kelly_fraction == pytest.approx(0.0)

    def test_tiny_edge_survives_maker(self):
        # With maker (0 fee), even small edges survive
        result = compute_kelly(ensemble_prob=0.52, market_price=0.50)
        assert result.edge == pytest.approx(0.02)
        assert result.kelly_fraction > 0

    def test_tiny_edge_killed_by_taker_fee(self):
        result = compute_kelly(ensemble_prob=0.52, market_price=0.50, fee_per_dollar=0.03)
        assert result.edge == 0.0
        assert result.kelly_fraction == 0.0


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
    result_no_fee = compute_kelly(0.80, 0.70, fee_per_dollar=0.0)
    result_with_fee = compute_kelly(0.80, 0.70, fee_per_dollar=0.02)
    assert result_with_fee.edge < result_no_fee.edge
    assert result_with_fee.edge > 0


def test_fee_kills_marginal_edge():
    result = compute_kelly(0.51, 0.50, fee_per_dollar=0.02)
    assert result.edge == 0.0
    assert result.kelly_fraction == 0.0


def test_fee_per_dollar_zero_matches_no_fee():
    result = compute_kelly(0.75, 0.60, fee_per_dollar=0.0)
    assert result.side == "YES"
    assert abs(result.edge - 0.15) < 1e-9


def test_no_side_still_returns_zero():
    result = compute_kelly(0.50, 0.50, fee_per_dollar=0.02)
    assert result.edge == 0.0


def test_position_size_no_double_fee():
    size = compute_position_size(bankroll=100.0, kelly_fraction=0.10, kelly_mult=0.25,
                                  confidence_mult=1.0, max_single_pct=0.15, min_trade_size=1.0)
    assert abs(size - 2.50) < 0.01


def test_extreme_price_taker_fee_much_lower():
    """At p=0.95 with sports rate, fee_per_dollar = 0.03 * 0.05 = 0.0015."""
    from polybot.trading.fees import compute_taker_fee_per_dollar
    fee_pd = compute_taker_fee_per_dollar(0.95, 0.03)
    result = compute_kelly(0.98, 0.95, fee_per_dollar=fee_pd)
    # edge = 0.03 - 0.0015 = 0.0285
    assert result.edge == pytest.approx(0.0285)
    assert result.side == "YES"


from polybot.trading.kelly import conviction_multiplier


class TestConvictionMultiplier:
    def test_no_confirmations(self):
        assert conviction_multiplier(0) == 1.0

    def test_one_confirmation(self):
        assert conviction_multiplier(1) == 1.5

    def test_two_confirmations(self):
        assert conviction_multiplier(2) == 2.0

    def test_capped_at_max(self):
        assert conviction_multiplier(5, max_multiplier=2.5) == 2.5

    def test_custom_per_signal(self):
        assert conviction_multiplier(1, per_signal=0.75) == 1.75
