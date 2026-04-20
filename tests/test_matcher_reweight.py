"""Tests for slug-aware matcher reweighting.

Observed in production (2026-04-20): Polymarket's Gamma API returns empty
slug for all sports markets. The original 0.55/0.25/0.20 weighting caps
max confidence at 0.75 even for perfect team+time matches, silently
rejecting all real matches. The slug-aware reweight fixes this without
weakening the 0.95 confidence floor.
"""
from datetime import datetime, timedelta, timezone
import pytest

from polybot.markets.sports_matcher import (
    compute_match_confidence, match_game_to_market,
    LiveGame, PolymarketMarket,
)


def _now():
    return datetime.now(timezone.utc)


def _game(home="Los Angeles Dodgers", away="New York Yankees") -> LiveGame:
    return LiveGame(
        sport="mlb", home_team=home, away_team=away,
        game_id="401", start_time=_now(),
        score_home=5, score_away=1, status="in_progress",
    )


def _market(question, slug="", hours_from_now=3.0) -> PolymarketMarket:
    return PolymarketMarket(
        polymarket_id="0x" + "a" * 40,
        question=question, slug=slug,
        resolution_time=_now() + timedelta(hours=hours_from_now),
    )


# ---- slug-empty path (the observed production data shape) ----

def test_empty_slug_perfect_match_passes_confidence_floor():
    """Both team names + 3h resolution — should clear 0.95."""
    game = _game()
    market = _market(
        question="Los Angeles Dodgers vs. New York Yankees",
        slug="", hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.confidence >= 0.95


def test_empty_slug_breakdown_reports_reweight():
    game = _game()
    market = _market(question="Los Angeles Dodgers vs. New York Yankees",
                      slug="", hours_from_now=3.0)
    _, breakdown = compute_match_confidence(game, market)
    assert breakdown["slug_present"] is False
    assert breakdown["name_weight"] == pytest.approx(0.80)


def test_empty_slug_single_team_still_rejected():
    """Production-safety: even with reweight, a one-team match must fail."""
    game = _game(home="Dodgers", away="Yankees")
    market = _market(
        question="Los Angeles Dodgers season outcome",   # only home team present
        slug="", hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None


def test_empty_slug_far_future_still_rejected():
    """Team names match but resolution is 48h away — time gate should reject."""
    game = _game()
    market = _market(
        question="Los Angeles Dodgers vs. New York Yankees",
        slug="", hours_from_now=48.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None


def test_empty_slug_requires_proximity_window():
    """At 5h proximity the match passes; at 10h it does not.
    Serves as a sanity check that the proximity window is actually
    enforced even after reweight."""
    game = _game()
    # 5h → time_score = 1.0 - (5/12)*0.5 ≈ 0.792
    # confidence = 0.80*1.0 + 0.20*0.792 ≈ 0.958 → passes
    ok = match_game_to_market(_game(),
        _market("Los Angeles Dodgers vs. New York Yankees", slug="",
                hours_from_now=5.0),
        min_confidence=0.95)
    assert ok is not None

    # 10h → time_score ≈ 0.583; confidence ≈ 0.917 → rejected
    rej = match_game_to_market(_game(),
        _market("Los Angeles Dodgers vs. New York Yankees", slug="",
                hours_from_now=10.0),
        min_confidence=0.95)
    assert rej is None




# ---- slug-present path (backward-compatible) ----

def test_slug_present_uses_original_weights():
    game = _game()
    market = _market(
        question="Los Angeles Dodgers vs. New York Yankees",
        slug="dodgers-vs-yankees-2026",   # slug populated
        hours_from_now=3.0,
    )
    _, breakdown = compute_match_confidence(game, market)
    assert breakdown["slug_present"] is True
    assert breakdown["name_weight"] == pytest.approx(0.55)


def test_slug_present_perfect_match_also_passes():
    """When slug is present and contains the canonical team names with
    spaces (not just hyphens), the slug path still clears the floor."""
    game = _game()
    # _slug_score is case-insensitive substring; use space-separated form
    market = _market(
        question="Los Angeles Dodgers vs. New York Yankees",
        slug="los angeles dodgers vs new york yankees",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.confidence >= 0.95


# ---- wrong-market rejection (production-safety invariant) ----

def test_wrong_teams_rejected_regardless_of_slug_state():
    """Neither reweight nor floor should let a wrong-market match through."""
    game = _game(home="Los Angeles Dodgers", away="New York Yankees")
    # Different game entirely
    market_a = _market(
        question="Atlanta Braves vs. San Francisco Giants",
        slug="", hours_from_now=3.0,
    )
    market_b = _market(
        question="Atlanta Braves vs. San Francisco Giants",
        slug="braves-vs-giants", hours_from_now=3.0,
    )
    assert match_game_to_market(game, market_a, min_confidence=0.95) is None
    assert match_game_to_market(game, market_b, min_confidence=0.95) is None


def test_confidence_floor_strictly_enforced():
    """Even with reweight, setting min_confidence=0.99 must still reject 0.969."""
    game = _game()
    market = _market(
        question="Los Angeles Dodgers vs. New York Yankees",
        slug="", hours_from_now=3.0,
    )
    # With reweight this passes at 0.95; but at 0.99 it must still fail
    strict = match_game_to_market(game, market, min_confidence=0.99)
    assert strict is None
