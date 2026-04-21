"""Tests for polybot.sports.margin_model."""
import math
import pytest

from polybot.sports.margin_model import (
    compute_cover_probability, time_elapsed_fraction, SPORT_MARGIN_PARAMS,
)
from polybot.sports.win_prob import GameState


def _nba_state(score_home=100, score_away=95, period=4, clock_seconds=120,
               total_periods=4) -> GameState:
    return GameState(
        sport="nba", score_home=score_home, score_away=score_away,
        period=period, clock_seconds=clock_seconds, total_periods=total_periods,
    )


# ---- time_elapsed_fraction ----

def test_elapsed_fraction_nba_start():
    s = _nba_state(period=1, clock_seconds=720)   # 12:00 left in Q1
    assert time_elapsed_fraction(s) == pytest.approx(0.0)


def test_elapsed_fraction_nba_half():
    s = _nba_state(period=3, clock_seconds=720)   # start of Q3 (2 of 4 periods done)
    assert time_elapsed_fraction(s) == pytest.approx(0.5)


def test_elapsed_fraction_nba_late():
    s = _nba_state(period=4, clock_seconds=60)   # 1:00 left in Q4
    # 3 full periods done + (12 - 1 min) = 47/48 = 0.979
    assert time_elapsed_fraction(s) == pytest.approx(47/48, abs=0.01)


def test_elapsed_fraction_mlb():
    s = GameState(sport="mlb", score_home=3, score_away=2,
                   period=5, clock_seconds=0, total_periods=9, outs=2)
    # 4 innings fully played + 2/3 of inning 5 = 4.667 of 9
    assert time_elapsed_fraction(s) == pytest.approx(4.667/9, abs=0.01)


def test_elapsed_fraction_unknown_sport_midpoint_default():
    s = GameState(sport="cricket", score_home=1, score_away=0,
                   period=1, clock_seconds=0, total_periods=2)
    assert time_elapsed_fraction(s) == 0.5


# ---- compute_cover_probability ----

def test_cover_prob_late_game_large_lead_approaches_one():
    """NBA leader up 20 with 1 min left, needs to cover -1.5."""
    s = _nba_state(score_home=120, score_away=100, period=4, clock_seconds=60)
    p = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    assert p is not None
    assert p > 0.99


def test_cover_prob_tied_midgame_cover_neg_line_under_half():
    """Tied at half needing to cover -1.5 (must still win by 2+) → below 0.5."""
    s = _nba_state(score_home=50, score_away=50, period=3, clock_seconds=720)
    p = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    assert p is not None
    assert p < 0.5


def test_cover_prob_small_lead_midgame_uncertain():
    """NBA up 3 with 20 min left, must cover -1.5 → somewhere near coin flip."""
    s = _nba_state(score_home=60, score_away=57, period=2, clock_seconds=240)
    p = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    assert p is not None
    assert 0.40 < p < 0.70


def test_cover_prob_underdog_plus_line_symmetric():
    """Underdog with +1.5 has higher cover prob than favorite with -1.5."""
    s = _nba_state(score_home=100, score_away=99, period=4, clock_seconds=120)
    fav_prob = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    dog_prob = compute_cover_probability(s, spread_line=+1.5, cover_side="away")
    assert fav_prob < dog_prob
    # They should sum to ~1.0 for the same game view
    assert fav_prob + dog_prob == pytest.approx(1.0, abs=0.01)


def test_cover_prob_away_side_leading():
    """Away team leading — cover side = away returns high prob."""
    s = _nba_state(score_home=90, score_away=110, period=4, clock_seconds=120)
    p = compute_cover_probability(s, spread_line=-5.5, cover_side="away")
    assert p is not None
    assert p > 0.90


def test_cover_prob_unsupported_sport_returns_none():
    s = GameState(sport="cricket", score_home=1, score_away=0,
                   period=1, clock_seconds=0, total_periods=2)
    assert compute_cover_probability(s, -1.5, "home") is None


def test_cover_prob_invalid_side_returns_none():
    s = _nba_state()
    assert compute_cover_probability(s, -1.5, "bogus") is None


def test_cover_prob_game_over_deterministic():
    """When sigma collapses (game effectively done), return 0 or 1 exactly."""
    s = _nba_state(score_home=110, score_away=100, period=4, clock_seconds=0)
    # elapsed ≈ 1.0 → sigma ≈ 0 → deterministic based on current margin
    p = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    # 10-point lead > 1.5 threshold, so = 1.0
    assert p == pytest.approx(1.0)


def test_cover_prob_mlb_late_large_lead():
    """MLB up 5 in the 8th, covering -1.5."""
    s = GameState(sport="mlb", score_home=8, score_away=3,
                   period=8, clock_seconds=0, total_periods=9, outs=2)
    p = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    assert p is not None
    assert p > 0.95


def test_cover_prob_nhl_two_goal_third_period():
    """NHL up 2 in 3rd period — should cover -1.5 confidently."""
    s = GameState(sport="nhl", score_home=4, score_away=2,
                   period=3, clock_seconds=300, total_periods=3)
    p = compute_cover_probability(s, spread_line=-1.5, cover_side="home")
    assert p is not None
    assert p > 0.65   # Not super confident given NHL variance


def test_cover_prob_sigma_shrinks_toward_end_of_game():
    """Same current margin, but later in game → higher cover probability."""
    s_early = _nba_state(score_home=100, score_away=95, period=2, clock_seconds=600)
    s_late = _nba_state(score_home=100, score_away=95, period=4, clock_seconds=60)
    p_early = compute_cover_probability(s_early, -1.5, "home")
    p_late = compute_cover_probability(s_late, -1.5, "home")
    assert p_late > p_early


def test_supported_sports_cover_params_exist():
    """Every sport in SUPPORTED_SPORTS should have margin params."""
    from polybot.sports.win_prob import SUPPORTED_SPORTS
    for sport in SUPPORTED_SPORTS:
        assert sport in SPORT_MARGIN_PARAMS, \
            f"Sport {sport} is in SUPPORTED_SPORTS but missing from SPORT_MARGIN_PARAMS"
