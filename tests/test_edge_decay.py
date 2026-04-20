"""Tests for polybot.learning.edge_decay."""
import pytest
from polybot.learning.edge_decay import evaluate_decay, DecayVerdict


def _make_outcomes(short_mean: float, long_mean: float, short_n: int, long_n: int):
    """Synthesize rows where the last `short_n` have mean `short_mean`
    and the full history has mean ≈ long_mean.
    """
    total_n = max(short_n, long_n)
    rows = []
    # Fill first (total_n - short_n) with values targeting long mean
    # while respecting the short-mean target over the last short_n entries.
    remaining_n = total_n - short_n
    # Sum of last short_n entries = short_mean * short_n
    last_sum = short_mean * short_n
    # Sum of entire long = long_mean * total_n
    total_sum = long_mean * total_n
    remaining_sum = total_sum - last_sum
    remaining_mean = remaining_sum / max(remaining_n, 1)
    for i in range(remaining_n):
        rows.append({"id": i, "pnl": remaining_mean})
    for i in range(short_n):
        rows.append({"id": remaining_n + i, "pnl": short_mean})
    return rows


def test_short_negative_long_positive_triggers_disable():
    """Short window mean < 0 AND long window mean > 0 → disable."""
    outcomes = _make_outcomes(short_mean=-0.5, long_mean=0.3,
                               short_n=50, long_n=200)
    verdict = evaluate_decay(outcomes, min_obs_for_disable=50)
    assert verdict.should_disable is True
    assert verdict.short_mean < 0
    assert verdict.long_mean > 0
    assert verdict.reason is not None


def test_short_positive_no_disable():
    outcomes = _make_outcomes(short_mean=0.5, long_mean=0.5,
                               short_n=50, long_n=200)
    verdict = evaluate_decay(outcomes, min_obs_for_disable=50)
    assert verdict.should_disable is False


def test_both_negative_no_disable():
    """If long is also negative we've known it's bad — daily kill-switch handles."""
    outcomes = _make_outcomes(short_mean=-0.5, long_mean=-0.2,
                               short_n=50, long_n=200)
    verdict = evaluate_decay(outcomes, min_obs_for_disable=50)
    assert verdict.should_disable is False


def test_insufficient_obs_no_disable():
    """Below min_obs_for_disable, never triggers."""
    outcomes = _make_outcomes(short_mean=-0.5, long_mean=0.5,
                               short_n=10, long_n=10)
    verdict = evaluate_decay(outcomes, min_obs_for_disable=50)
    assert verdict.should_disable is False
    assert verdict.short_n == 10


def test_empty_outcomes():
    verdict = evaluate_decay([])
    assert verdict.should_disable is False
    assert verdict.short_mean is None


def test_missing_pnl_rows_skipped():
    outcomes = [
        *[{"id": i, "pnl": 1.0} for i in range(150)],
        *[{"id": i + 150, "pnl": None} for i in range(50)],
        *[{"id": i + 200, "pnl": -0.8} for i in range(50)],
    ]
    # 150 positive at start, 50 None skipped, 50 negative at end
    # Sorted chronologically: last 50 valid = last 50 negative
    # But long window (200) includes the 150 positive rows
    verdict = evaluate_decay(outcomes, min_obs_for_disable=50)
    assert verdict.should_disable is True
