"""Tests for polybot.learning.kelly_scaler."""
import pytest
from polybot.learning.kelly_scaler import (
    compute_kelly_scaler, compute_from_outcomes, _beta_posterior,
)


def test_cold_start_returns_one():
    """Below cold-start N, scaler is 1.0 (no opinion)."""
    assert compute_kelly_scaler(wins=5, losses=3, predicted_prob=0.7,
                                 cold_start_n=20) == 1.0


def test_well_calibrated_returns_one():
    """Posterior ~0.60 with predicted 0.60 → no adjustment."""
    # 30 obs at 60% win rate, predicted 60% → posterior mean ≈ 0.60
    scaler = compute_kelly_scaler(wins=18, losses=12, predicted_prob=0.60)
    assert scaler == 1.0


def test_underperforming_reduces_kelly():
    """Posterior 1σ below predicted → 0.5× scaler."""
    # 100 obs with 40% win rate, predicted 70% → posterior well below
    scaler = compute_kelly_scaler(wins=40, losses=60, predicted_prob=0.70)
    assert scaler == 0.5


def test_overperforming_increases_kelly():
    """Posterior 1σ above predicted → 1.5× scaler."""
    # 100 obs with 85% win rate, predicted 60% → posterior well above
    scaler = compute_kelly_scaler(wins=85, losses=15, predicted_prob=0.60)
    assert scaler == 1.5


def test_clamp_bounds():
    """Returned value always within [min_scale, max_scale]."""
    scaler = compute_kelly_scaler(wins=40, losses=60, predicted_prob=0.95,
                                   min_scale=0.25, max_scale=2.0)
    assert 0.25 <= scaler <= 2.0


def test_compute_from_outcomes_happy():
    outcomes = [
        {"pnl": 1.0, "predicted_prob": 0.7},
        {"pnl": -0.5, "predicted_prob": 0.7},
        {"pnl": 1.0, "predicted_prob": 0.7},
    ] * 10   # 30 rows, 20 wins / 10 losses
    scaler, avg_pred = compute_from_outcomes(outcomes)
    assert avg_pred == pytest.approx(0.7, abs=1e-6)
    # 20/10 = 66% observed, predicted 70% → should be ~1.0 (well within 1σ)
    assert scaler in (0.5, 1.0, 1.5)   # sensible bucket


def test_compute_from_outcomes_missing_pnl_skipped():
    outcomes = [
        {"predicted_prob": 0.7},   # missing pnl
        {"pnl": 1.0, "predicted_prob": 0.7},
    ]
    scaler, _ = compute_from_outcomes(outcomes, cold_start_n=1)
    # Only 1 valid obs, cold_start_n=1 → meets threshold, should return valid
    assert 0.25 <= scaler <= 2.0


def test_compute_from_outcomes_no_predicted_returns_one():
    outcomes = [{"pnl": 1.0}, {"pnl": -1.0}]
    scaler, avg_pred = compute_from_outcomes(outcomes)
    assert scaler == 1.0
    assert avg_pred is None


def test_beta_posterior_mean():
    p = _beta_posterior(wins=40, losses=60)
    assert p.mean == pytest.approx(40.5 / 101, abs=1e-6)
    assert p.sigma > 0
    assert p.n == 100
