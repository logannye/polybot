import pytest
from polybot.analysis.quant import (compute_line_movement, compute_volume_spike, compute_book_imbalance,
    compute_spread_signal, compute_time_decay, compute_composite_score, QuantSignals)


class TestLineMovement:
    def test_price_moving_toward_estimate(self):
        signal = compute_line_movement(price_history=[0.40, 0.42, 0.44, 0.45], ensemble_prob=0.60)
        assert signal > 0

    def test_price_moving_away(self):
        signal = compute_line_movement(price_history=[0.50, 0.48, 0.44, 0.40], ensemble_prob=0.60)
        assert signal < 0

    def test_flat_price(self):
        assert compute_line_movement(price_history=[0.50, 0.50, 0.50], ensemble_prob=0.60) == pytest.approx(0.0)

    def test_clamps_to_range(self):
        signal = compute_line_movement(price_history=[0.10, 0.90], ensemble_prob=0.95)
        assert -1.0 <= signal <= 1.0


class TestVolume:
    def test_high_volume_bullish(self):
        assert compute_volume_spike(current_volume=2000, avg_volume=500) > 0

    def test_low_volume(self):
        assert compute_volume_spike(current_volume=100, avg_volume=500) < 0

    def test_zero_avg(self):
        assert compute_volume_spike(current_volume=100, avg_volume=0) == pytest.approx(0.0)


class TestBookImbalance:
    def test_bid_heavy(self):
        assert compute_book_imbalance(bid_depth=800, ask_depth=200) > 0

    def test_ask_heavy(self):
        assert compute_book_imbalance(bid_depth=200, ask_depth=800) < 0

    def test_balanced(self):
        assert compute_book_imbalance(bid_depth=500, ask_depth=500) == pytest.approx(0.0)


class TestSpread:
    def test_tight_spread(self):
        assert compute_spread_signal(bid=0.49, ask=0.51) > 0

    def test_wide_spread(self):
        assert compute_spread_signal(bid=0.40, ask=0.60) < 0


class TestTimeDecay:
    def test_plenty_of_time(self):
        assert compute_time_decay(hours_remaining=48) > 0

    def test_very_little_time(self):
        assert compute_time_decay(hours_remaining=1) < 0

    def test_zero_time(self):
        assert compute_time_decay(hours_remaining=0) == pytest.approx(-1.0)


class TestComposite:
    def test_all_bullish(self):
        signals = QuantSignals(line_movement=0.8, volume_spike=0.6, book_imbalance=0.5, spread=0.7, time_decay=0.3)
        weights = {"line_movement": 0.30, "volume_spike": 0.25, "book_imbalance": 0.20, "spread": 0.15, "time_decay": 0.10}
        expected = 0.8*0.30 + 0.6*0.25 + 0.5*0.20 + 0.7*0.15 + 0.3*0.10
        assert compute_composite_score(signals, weights) == pytest.approx(expected)

    def test_mixed_signals(self):
        signals = QuantSignals(line_movement=-0.5, volume_spike=0.3, book_imbalance=0.0, spread=-0.8, time_decay=0.2)
        weights = {"line_movement": 0.30, "volume_spike": 0.25, "book_imbalance": 0.20, "spread": 0.15, "time_decay": 0.10}
        expected = -0.5*0.30 + 0.3*0.25 + 0.0*0.20 + -0.8*0.15 + 0.2*0.10
        assert compute_composite_score(signals, weights) == pytest.approx(expected)
