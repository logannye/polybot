"""Tests for polybot.sports.win_prob per v10 spec §7."""
import pytest
from polybot.sports.win_prob import (
    compute_win_prob, GameState, SUPPORTED_SPORTS, SOCCER_SPORTS,
)


def _nba_state(**overrides) -> GameState:
    defaults = dict(
        sport="nba", score_home=100, score_away=95,
        period=4, clock_seconds=60.0, total_periods=4,
    )
    defaults.update(overrides)
    return GameState(**defaults)


def test_all_supported_sports_returnable():
    """Every sport in SUPPORTED_SPORTS must produce a number, not None."""
    for sport in SUPPORTED_SPORTS:
        total = 2 if sport in SOCCER_SPORTS else 4 if sport != "mlb" else 9
        state = GameState(
            sport=sport, score_home=2, score_away=1,
            period=1, clock_seconds=300.0, total_periods=total,
        )
        result = compute_win_prob(state)
        assert result is not None
        assert 0.0 < result < 1.0, f"{sport}: {result} out of (0,1)"


def test_unsupported_sport_returns_none():
    state = GameState(
        sport="cricket", score_home=5, score_away=3,
        period=1, clock_seconds=100, total_periods=2,
    )
    assert compute_win_prob(state) is None


def test_tied_game_returns_half():
    """A tied score at any point returns 0.5."""
    for sport in ("nba", "nhl", "mlb", "epl"):
        state = GameState(
            sport=sport, score_home=10, score_away=10,
            period=2, clock_seconds=300,
            total_periods=4 if sport == "nba" else (3 if sport == "nhl" else 9 if sport == "mlb" else 2),
        )
        result = compute_win_prob(state)
        assert result == pytest.approx(0.5), f"{sport}: tied should be 0.5 got {result}"


def test_nba_late_game_large_lead_high_confidence():
    """NBA 4th quarter, up 20 with 1 min left should be essentially certain."""
    state = _nba_state(score_home=120, score_away=100, clock_seconds=60)
    result = compute_win_prob(state)
    assert result > 0.97


def test_nba_early_game_small_lead_near_half():
    """NBA 1st quarter, up 3 with 10min left should be only slightly above 0.5."""
    state = _nba_state(score_home=15, score_away=12, period=1, clock_seconds=600)
    result = compute_win_prob(state)
    assert 0.50 < result < 0.62, f"expected slight edge, got {result}"


def test_nba_late_tied_returns_half():
    state = _nba_state(score_home=100, score_away=100, clock_seconds=30)
    result = compute_win_prob(state)
    assert result == pytest.approx(0.5)


def test_nhl_two_goal_lead_third_period():
    """NHL up 2 goals with 5 min left in 3rd should be strong.
    0.83+ is in line with historical NHL third-period WP charts."""
    state = GameState(
        sport="nhl", score_home=4, score_away=2,
        period=3, clock_seconds=300, total_periods=3,
    )
    result = compute_win_prob(state)
    assert result > 0.80


def test_nhl_one_goal_lead_first_period():
    """NHL up 1 in 1st period should not be confident."""
    state = GameState(
        sport="nhl", score_home=1, score_away=0,
        period=1, clock_seconds=600, total_periods=3,
    )
    result = compute_win_prob(state)
    assert 0.50 < result < 0.70


def test_mlb_large_late_lead():
    """MLB up 6 in bottom of 8th should be near-certain."""
    state = GameState(
        sport="mlb", score_home=8, score_away=2,
        period=8, clock_seconds=0, total_periods=9, outs=2,
    )
    result = compute_win_prob(state)
    assert result > 0.93


def test_mlb_one_run_lead_early():
    state = GameState(
        sport="mlb", score_home=2, score_away=1,
        period=2, clock_seconds=0, total_periods=9, outs=1,
    )
    result = compute_win_prob(state)
    assert 0.50 < result < 0.70


def test_soccer_two_goal_lead_near_end():
    """EPL up 2 with 5 min left — very high probability."""
    state = GameState(
        sport="epl", score_home=3, score_away=1,
        period=2, clock_seconds=300, total_periods=2,
    )
    result = compute_win_prob(state)
    assert result > 0.94


def test_soccer_one_goal_lead_halftime():
    """Soccer up 1 at half — moderate."""
    state = GameState(
        sport="epl", score_home=1, score_away=0,
        period=2, clock_seconds=45 * 60, total_periods=2,
    )
    result = compute_win_prob(state)
    assert 0.60 < result < 0.85


def test_all_soccer_flavors_produce_same_value():
    """UCL/EPL/La Liga/Bundesliga/MLS share the same model."""
    states = []
    for sport in ("ucl", "epl", "laliga", "bundesliga", "mls"):
        states.append(compute_win_prob(GameState(
            sport=sport, score_home=2, score_away=1,
            period=2, clock_seconds=600, total_periods=2,
        )))
    assert len(set(states)) == 1, f"soccer leagues diverge: {states}"


def test_end_of_regulation_nonzero_diff_returns_near_one():
    """Clock expires with non-zero score_diff → leader essentially won."""
    state = GameState(
        sport="nba", score_home=100, score_away=99,
        period=4, clock_seconds=0, total_periods=4,
    )
    result = compute_win_prob(state)
    assert result > 0.99
