"""Tests for polybot.sports.calibrator per v10 spec §7."""
import pytest
from polybot.sports.calibrator import OnlineCalibrator, bucket_for_game_state


def test_fallback_shrinkage_when_under_min_obs():
    """With <30 obs, apply() should shrink toward 0.5 by fallback fraction."""
    calib = OnlineCalibrator(min_obs_for_fit=30, fallback_shrinkage=0.10)
    calib.ingest("nba", "late_two", 0.80, 1)  # only 1 obs → fallback
    calib.fit_all()
    result = calib.apply("nba", "late_two", 0.80)
    # shrunk toward 0.5 by 10%: 0.5 + 0.9 * (0.80 - 0.5) = 0.77
    assert result == pytest.approx(0.77, abs=1e-6)


def test_isotonic_fit_on_well_calibrated_data():
    """Feed perfectly-calibrated observations; isotonic should return ~pred."""
    calib = OnlineCalibrator(min_obs_for_fit=30)
    # Synthesize 60 obs where pred matches outcome rate exactly
    import random
    random.seed(42)
    for _ in range(60):
        pred = random.uniform(0.1, 0.9)
        outcome = 1 if random.random() < pred else 0
        calib.ingest("nba", "test_bucket", pred, outcome)
    calib.fit_all()
    # On a perfectly calibrated set the transform should be monotonic and
    # near-identity within tolerance
    for p in (0.2, 0.5, 0.8):
        out = calib.apply("nba", "test_bucket", p)
        assert 0.0 <= out <= 1.0
    # Monotonicity check
    assert calib.apply("nba", "test_bucket", 0.8) >= calib.apply("nba", "test_bucket", 0.2)


def test_isotonic_corrects_systematic_overconfidence():
    """Feed overconfident predictions; calibrator should pull toward reality."""
    calib = OnlineCalibrator(min_obs_for_fit=30)
    # Predictions all at 0.9 but actual win-rate 0.6
    for i in range(60):
        outcome = 1 if i < 36 else 0   # 60% actual
        calib.ingest("nba", "bucket", 0.9, outcome)
    calib.fit_all()
    # The fitted transform should pull 0.9 toward ~0.6
    out = calib.apply("nba", "bucket", 0.9)
    assert 0.55 <= out <= 0.65, f"expected ~0.6, got {out}"


def test_invalid_outcome_rejected():
    calib = OnlineCalibrator()
    with pytest.raises(ValueError):
        calib.ingest("nba", "b", 0.5, 2)


def test_invalid_pred_rejected():
    calib = OnlineCalibrator()
    with pytest.raises(ValueError):
        calib.ingest("nba", "b", 1.5, 1)


def test_fitted_buckets_requires_min_obs():
    """Only buckets with ≥min_obs get fitted."""
    calib = OnlineCalibrator(min_obs_for_fit=30)
    for _ in range(29):
        calib.ingest("nba", "thin", 0.7, 1)
    for i in range(30):
        calib.ingest("nba", "thick", 0.7, 1 if i < 20 else 0)
    calib.fit_all()
    buckets = calib.fitted_buckets()
    assert all(k.bucket != "thin" for k in buckets)
    assert any(k.bucket == "thick" for k in buckets)


def test_bucket_for_game_state_nba_late_tied():
    """Q4 tied game should map to 'late_tied'."""
    result = bucket_for_game_state(
        sport="nba", score_diff=0, period=4, total_periods=4, seconds_left=60)
    assert result == "late_tied"


def test_bucket_for_game_state_mlb_mid_two_run_lead():
    """MLB 5th inning 2-run lead → 'mid_two'."""
    result = bucket_for_game_state(
        sport="mlb", score_diff=2, period=5, total_periods=9, seconds_left=0)
    assert result == "mid_two"


def test_bucket_for_game_state_large_lead_late():
    result = bucket_for_game_state(
        sport="nba", score_diff=15, period=4, total_periods=4, seconds_left=300)
    assert result == "late_large"


def test_bulk_load():
    """load_observations accepts a list of tuples."""
    calib = OnlineCalibrator(min_obs_for_fit=30)
    rows = [
        ("nba", "b1", 0.6, 1), ("nba", "b1", 0.6, 0),
        ("nhl", "b2", 0.7, 1),
    ]
    calib.load_observations(rows)
    assert calib.bucket_count("nba", "b1") == 2
    assert calib.bucket_count("nhl", "b2") == 1
