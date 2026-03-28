import pytest
from polybot.learning.calibration import compute_brier_score, update_trust_weight, compute_calibration_correction
from polybot.learning.categories import compute_category_bias, CategoryStats


class TestBrierScore:
    def test_perfect_prediction(self):
        assert compute_brier_score(predicted=1.0, actual=1) == pytest.approx(0.0)

    def test_worst_prediction(self):
        assert compute_brier_score(predicted=0.0, actual=1) == pytest.approx(1.0)

    def test_moderate_prediction(self):
        assert compute_brier_score(predicted=0.6, actual=1) == pytest.approx(0.16)

    def test_correct_no(self):
        assert compute_brier_score(predicted=0.2, actual=0) == pytest.approx(0.04)


class TestTrustWeightUpdate:
    def test_ema_update(self):
        assert update_trust_weight(old_brier_ema=0.25, new_brier=0.10, alpha=0.1) == pytest.approx(0.235)

    def test_converges_to_good_score(self):
        brier = 0.25
        for _ in range(100):
            brier = update_trust_weight(brier, 0.05, alpha=0.1)
        assert brier < 0.06


class TestCalibrationCorrection:
    def test_overconfident_predictions(self):
        predictions = [0.80] * 100
        outcomes = [1] * 60 + [0] * 40
        correction = compute_calibration_correction(predictions, outcomes, bins=5)
        assert any(v < 0 for v in correction.values())

    def test_well_calibrated(self):
        predictions = [0.60] * 100
        outcomes = [1] * 60 + [0] * 40
        correction = compute_calibration_correction(predictions, outcomes, bins=5)
        relevant = [v for k, v in correction.items() if 0.5 <= k <= 0.7]
        for v in relevant:
            assert abs(v) < 0.05

    def test_empty_data(self):
        assert compute_calibration_correction([], [], bins=5) == {}


class TestCategoryBias:
    def test_profitable_category_gets_bonus(self):
        stats = CategoryStats(total_trades=30, total_pnl=50.0, win_count=20)
        assert compute_category_bias(stats, min_trades=20) > 0

    def test_losing_category_gets_penalty(self):
        stats = CategoryStats(total_trades=30, total_pnl=-40.0, win_count=10)
        assert compute_category_bias(stats, min_trades=20) < 0

    def test_insufficient_data_returns_neutral(self):
        stats = CategoryStats(total_trades=5, total_pnl=10.0, win_count=4)
        assert compute_category_bias(stats, min_trades=20) == pytest.approx(0.0)
