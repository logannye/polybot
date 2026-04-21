"""Tests for normalization-aware matcher scoring.

Real-world shape (observed 2026-04-20): Polymarket questions use short
forms like "Raptors vs. Cavaliers" while ESPN sends long forms like
"Toronto Raptors". The matcher must bridge both.
"""
from datetime import datetime, timedelta, timezone
import pytest
from polybot.markets.sports_matcher import (
    _team_name_score, _determine_side,
    compute_match_confidence, match_game_to_market,
    LiveGame, PolymarketMarket,
)


def _now():
    return datetime.now(timezone.utc)


def _nba_game(home="Toronto Raptors", away="Cleveland Cavaliers") -> LiveGame:
    return LiveGame(
        sport="nba", home_team=home, away_team=away,
        game_id="401", start_time=_now(),
        score_home=100, score_away=120, status="in_progress",
    )


def _poly_market(question, slug="nba-tor-cle-2026-04-20", hours=2.0) -> PolymarketMarket:
    return PolymarketMarket(
        polymarket_id="0x" + "a" * 40,
        question=question, slug=slug,
        resolution_time=_now() + timedelta(hours=hours),
    )


def test_long_form_espn_matches_short_form_polymarket_question():
    """ESPN 'Toronto Raptors' must match Polymarket 'Raptors vs. Cavaliers'."""
    game = _nba_game()
    market = _poly_market("Raptors vs. Cavaliers")
    # Both teams should score
    assert _team_name_score(game, market) == pytest.approx(1.0)


def test_normalization_aware_scoring_for_each_sport():
    """Verify NBA/NHL/MLB all bridge long→short via canonical aliases."""
    cases = [
        ("nba", "Oklahoma City Thunder", "Los Angeles Lakers",
         "Thunder vs. Lakers"),
        ("nhl", "Boston Bruins", "Toronto Maple Leafs",
         "Bruins vs. Maple Leafs"),
        ("mlb", "Los Angeles Dodgers", "New York Yankees",
         "Dodgers vs. Yankees"),
    ]
    for sport, home, away, question in cases:
        game = LiveGame(
            sport=sport, home_team=home, away_team=away, game_id="0",
            start_time=_now(), score_home=1, score_away=0,
            status="in_progress",
        )
        market = PolymarketMarket(
            polymarket_id="0x1", question=question, slug="",
            resolution_time=_now() + timedelta(hours=2),
        )
        assert _team_name_score(game, market) == pytest.approx(1.0), \
            f"{sport}: long→short failed for '{question}'"


def test_determine_side_uses_normalized_names():
    """ESPN long form should produce the correct home/away resolution
    even when the question uses short form."""
    game = _nba_game(home="Toronto Raptors", away="Cleveland Cavaliers")
    # Raptors first in question → home (since game.home_team=Raptors)
    market = _poly_market("Raptors vs. Cavaliers")
    side = _determine_side(game, market, "moneyline")
    assert side == "home"

    # Cavaliers first → away
    market2 = _poly_market("Cavaliers vs. Raptors", slug="nba-cle-tor-2026-04-20")
    side2 = _determine_side(game, market2, "moneyline")
    assert side2 == "away"


def test_real_world_nba_game_passes_confidence_floor():
    """Live-data shape: Toronto Raptors @ Cleveland Cavaliers game, Polymarket
    question 'Raptors vs. Cavaliers', event slug 'nba-tor-cle-2026-04-20',
    resolution 2h out. Must clear 0.95 with my fix."""
    game = _nba_game()
    market = _poly_market("Raptors vs. Cavaliers",
                           slug="nba-tor-cle-2026-04-20", hours=2.0)
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.confidence >= 0.95


def test_abbreviated_slug_does_not_corrupt_scoring():
    """Slug 'nba-tor-cle-2026' has no long-form name so slug_score=0;
    reweight must kick in to salvage the match."""
    game = _nba_game()
    market = _poly_market("Raptors vs. Cavaliers",
                           slug="nba-tor-cle-2026-04-20", hours=2.0)
    conf, breakdown = compute_match_confidence(game, market)
    assert breakdown["slug_score"] == 0.0
    # Reweight activates because slug_score is 0 (not because slug is empty)
    assert breakdown["name_weight"] == pytest.approx(0.80)
    assert conf > 0.95


def test_wrong_teams_still_rejected_with_normalization():
    """Production-safety: even with normalization, a game that doesn't
    match the market's teams must be rejected."""
    game = _nba_game(home="Toronto Raptors", away="Cleveland Cavaliers")
    # Different game entirely
    market = _poly_market("Lakers vs. Warriors", slug="nba-lal-gsw-2026-04-20")
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None


def test_single_team_match_with_normalization_still_insufficient():
    """If only ONE team is normalizable into the question, name_score=0.5,
    confidence capped below 0.95."""
    game = _nba_game(home="Toronto Raptors", away="Cleveland Cavaliers")
    # Only Raptors is in the question
    market = _poly_market("Raptors season over/under wins 50.5",
                           slug="nba-tor-wins-season")
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None


def test_team_with_space_in_canonical_key_still_matches():
    """Trail Blazers → canonical 'trail_blazers' → search term 'trail blazers'
    (underscore replaced with space)."""
    game = LiveGame(
        sport="nba", home_team="Portland Trail Blazers", away_team="Boston Celtics",
        game_id="0", start_time=_now(), score_home=1, score_away=0,
        status="in_progress",
    )
    market = PolymarketMarket(
        polymarket_id="0x", question="Trail Blazers vs. Celtics",
        slug="nba-por-bos-2026-04-20",
        resolution_time=_now() + timedelta(hours=2),
    )
    assert _team_name_score(game, market) == pytest.approx(1.0)
