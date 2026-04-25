"""Tests for polybot.sports.totals_model."""
import pytest

from polybot.sports.totals_model import (
    compute_total_probability, SPORT_TOTALS_PARAMS,
)
from polybot.sports.win_prob import GameState


def _nba_state(score_home=55, score_away=50, period=2, clock_seconds=240,
               total_periods=4) -> GameState:
    return GameState(
        sport="nba", score_home=score_home, score_away=score_away,
        period=period, clock_seconds=clock_seconds, total_periods=total_periods,
    )


# ---- compute_total_probability: structural invariants ----

def test_total_unsupported_sport_returns_none():
    s = GameState(sport="cricket", score_home=1, score_away=0,
                  period=1, clock_seconds=0, total_periods=2)
    assert compute_total_probability(s, line=10.0, side="over") is None


def test_total_invalid_side_returns_none():
    s = _nba_state()
    assert compute_total_probability(s, line=200.0, side="bogus") is None


def test_total_negative_line_rejected():
    """Total lines should always be positive; defensive guard."""
    s = _nba_state()
    assert compute_total_probability(s, line=-5.0, side="over") is None


def test_total_over_under_sum_to_one():
    """For any state and line, P(over) + P(under) ≈ 1."""
    s = _nba_state()
    p_over = compute_total_probability(s, line=210.0, side="over")
    p_under = compute_total_probability(s, line=210.0, side="under")
    assert p_over is not None and p_under is not None
    assert p_over + p_under == pytest.approx(1.0, abs=0.01)


# ---- compute_total_probability: physical-sanity checks ----

def test_total_late_game_high_score_over_clears():
    """NBA total already 230 with 1 min left, line 210: over ≈ 1.0 (already over)."""
    s = _nba_state(score_home=120, score_away=110, period=4, clock_seconds=60)
    p = compute_total_probability(s, line=210.0, side="over")
    assert p is not None
    assert p > 0.99


def test_total_late_game_low_score_under_clears():
    """NBA total only 80 with 1 min left, line 210: under ≈ 1.0."""
    s = _nba_state(score_home=42, score_away=38, period=4, clock_seconds=60)
    p = compute_total_probability(s, line=210.0, side="under")
    assert p is not None
    assert p > 0.99


def test_total_midgame_pace_uncertain():
    """Midgame, on-pace for the line: ~50/50."""
    # NBA half-time, 105 total, line 210 (right on pace) → near 0.5
    s = _nba_state(score_home=55, score_away=50, period=3, clock_seconds=720)
    p = compute_total_probability(s, line=210.0, side="over")
    assert p is not None
    assert 0.40 < p < 0.60


def test_total_mlb_late_high_runs_over_clears():
    """MLB total runs 12 by 8th, line 8.5: over ≈ 1.0 (already over)."""
    s = GameState(sport="mlb", score_home=7, score_away=5,
                  period=8, clock_seconds=0, total_periods=9, outs=2)
    p = compute_total_probability(s, line=8.5, side="over")
    assert p is not None
    assert p > 0.99


def test_total_nhl_late_low_goals_under_clears():
    """NHL 1 goal in 3rd, line 5.5: under ≈ 1.0."""
    s = GameState(sport="nhl", score_home=1, score_away=0,
                  period=3, clock_seconds=300, total_periods=3)
    p = compute_total_probability(s, line=5.5, side="under")
    assert p is not None
    assert p > 0.95


def test_total_soccer_high_scoring_over():
    """Soccer 3-2 in 80th min, line 4.5: barely over."""
    # 80 min played out of 90 → ~89% elapsed, total 5 already > 4.5 → over ≈ 1.0
    s = GameState(sport="epl", score_home=3, score_away=2,
                  period=2, clock_seconds=10 * 60, total_periods=2)
    p = compute_total_probability(s, line=4.5, side="over")
    assert p is not None
    assert p > 0.95


# ---- compute_total_probability: variance scaling ----

def test_total_sigma_shrinks_toward_end_of_game():
    """Same on-pace state, but later in game → tighter distribution.

    Under linear-pace projection, two states on identical pace produce the
    same expected_final; the later state has smaller sigma, so the implied
    probability is more confident in whichever direction the line lies.
    Here both states are on-pace for 200 vs a 210 line, so late should be
    more confident UNDER (lower p_over).
    """
    # On-pace for 200 (under 210), early in game vs late in game
    s_early = _nba_state(score_home=25, score_away=25, period=2, clock_seconds=720)
    # period=2, clock=720 (start of Q2) → elapsed=0.25, total=50, pace=200
    s_late = _nba_state(score_home=50, score_away=50, period=3, clock_seconds=720)
    # period=3, clock=720 (start of Q3) → elapsed=0.50, total=100, pace=200
    p_over_early = compute_total_probability(s_early, line=210.0, side="over")
    p_over_late = compute_total_probability(s_late, line=210.0, side="over")
    assert p_over_early is not None and p_over_late is not None
    # Both are under 0.5 (line above pace), late more confident under
    assert p_over_early < 0.5 and p_over_late < 0.5
    assert p_over_late < p_over_early


def test_total_game_over_deterministic():
    """When sigma collapses (game effectively done), return 0 or 1 exactly."""
    s = _nba_state(score_home=110, score_away=100, period=4, clock_seconds=0)
    # Final total 210 vs line 200 → over = 1.0
    p = compute_total_probability(s, line=200.0, side="over")
    assert p == pytest.approx(1.0)


def test_total_game_over_under_deterministic():
    s = _nba_state(score_home=110, score_away=100, period=4, clock_seconds=0)
    p = compute_total_probability(s, line=220.0, side="under")
    assert p == pytest.approx(1.0)


# ---- per-sport coverage ----

def test_supported_sports_totals_params_exist():
    """Every sport in SUPPORTED_SPORTS should have totals params."""
    from polybot.sports.win_prob import SUPPORTED_SPORTS
    for sport in SUPPORTED_SPORTS:
        assert sport in SPORT_TOTALS_PARAMS, \
            f"Sport {sport} is in SUPPORTED_SPORTS but missing from SPORT_TOTALS_PARAMS"


def test_total_zero_line_handled():
    """Defensive: line=0 with positive current_total → over=1.0."""
    s = _nba_state(score_home=50, score_away=50, period=2, clock_seconds=240)
    p = compute_total_probability(s, line=0.0, side="over")
    # Already past 0; sigma still nonzero but z is very large positive
    assert p is not None
    assert p > 0.99


def test_total_far_above_line_at_start_returns_high_over():
    """If line is very low (e.g., 1.0) but expected pace will far exceed it,
    over should be > 0.95 even early."""
    s = _nba_state(score_home=20, score_away=20, period=1, clock_seconds=300)
    p = compute_total_probability(s, line=20.0, side="over")
    assert p is not None
    # Already at 40 total > 20 line → over ≈ 1.0
    assert p > 0.99
