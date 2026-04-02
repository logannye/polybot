import pytest
from polybot.markets.rewards import compute_reward_score


def test_perfect_spread():
    """Zero spread should score 1.0."""
    assert compute_reward_score(spread=0.0, max_spread=0.05, size=10, min_size=5) == 1.0


def test_half_spread():
    """Half of max spread should score 0.25 (quadratic)."""
    assert compute_reward_score(spread=0.025, max_spread=0.05, size=10, min_size=5) == pytest.approx(0.25)


def test_spread_at_max():
    """Spread at max should score 0."""
    assert compute_reward_score(spread=0.05, max_spread=0.05, size=10, min_size=5) == 0.0


def test_spread_beyond_max():
    """Spread beyond max should score 0."""
    assert compute_reward_score(spread=0.06, max_spread=0.05, size=10, min_size=5) == 0.0


def test_size_below_min():
    """Size below minimum should score 0."""
    assert compute_reward_score(spread=0.01, max_spread=0.05, size=3, min_size=5) == 0.0


def test_zero_max_spread():
    """Edge case: max_spread=0 should score 0."""
    assert compute_reward_score(spread=0.01, max_spread=0.0, size=10, min_size=5) == 0.0


def test_quadratic_scaling():
    """Quarter spread should score (3/4)^2 = 0.5625."""
    score = compute_reward_score(spread=0.0125, max_spread=0.05, size=10, min_size=5)
    assert score == pytest.approx(0.5625)
