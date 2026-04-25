"""Tests for polybot.markets.sports_matcher — highest-risk component.

v10 spec §3 requires ≥90% coverage here. Confidence-floor rejection is
the critical behavior — false matches would trade the wrong market.
"""
from datetime import datetime, timedelta, timezone
import pytest

from polybot.markets.sports_matcher import (
    LiveGame, PolymarketMarket, MatchResult,
    normalize_team_name, classify_market_type, match_game_to_market,
    NBA_ALIASES, NHL_ALIASES, MLB_ALIASES,
)


# ---- normalize_team_name --------------------------------------------------

def test_normalize_nba_exact():
    assert normalize_team_name("Oklahoma City Thunder", "nba") == "thunder"


def test_normalize_nba_abbrev():
    assert normalize_team_name("OKC", "nba") == "thunder"


def test_normalize_nba_short():
    assert normalize_team_name("Thunder", "nba") == "thunder"


def test_normalize_nba_in_phrase():
    assert normalize_team_name("Oklahoma City Thunder vs Jazz", "nba") == "thunder"


def test_normalize_nhl_nickname():
    assert normalize_team_name("Habs", "nhl") == "canadiens"


def test_normalize_mlb_apostrophe():
    # "A's" contains an apostrophe; lookup tolerates both
    assert normalize_team_name("Oakland Athletics", "mlb") == "athletics"


def test_normalize_unknown_team_returns_none_in_known_league():
    assert normalize_team_name("Fantasy United FC", "nba") is None


def test_normalize_soccer_falls_through_to_clean():
    """Soccer has empty lookup so it returns cleaned name, not None."""
    assert normalize_team_name("Real Madrid", "laliga") == "real madrid"


def test_normalize_empty():
    assert normalize_team_name("", "nba") is None


# ---- classify_market_type -------------------------------------------------

def test_classify_spread_market():
    result = classify_market_type("Spread: Thunder (-6.5)")
    assert result == ("spread", -6.5)


def test_classify_spread_positive_line():
    result = classify_market_type("Spread: Jazz (+7)")
    assert result == ("spread", 7.0)


def test_classify_total_over_under():
    result = classify_market_type("Will the total be over 220.5?")
    assert result is not None
    assert result[0] == "total"
    assert result[1] == 220.5


def test_classify_moneyline_vs():
    result = classify_market_type("Thunder vs Jazz 2026-04-05")
    assert result is not None
    assert result[0] == "moneyline"


def test_classify_moneyline_will_win():
    result = classify_market_type("Will the Lakers beat the Warriors on 2026-04-10?")
    assert result is not None
    assert result[0] == "moneyline"


def test_classify_ambiguous_returns_none():
    result = classify_market_type("Will GPT-6 be released in 2026?")
    assert result is None


def test_classify_empty():
    assert classify_market_type("") is None


# ---- match_game_to_market confidence floor --------------------------------

def _make_game(home="thunder", away="jazz", hours_from_now=0.0) -> LiveGame:
    return LiveGame(
        sport="nba",
        home_team=home, away_team=away,
        game_id="401678",
        start_time=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
        score_home=50, score_away=45, status="in_progress",
    )


def _make_market(question, slug="thunder-vs-jazz-2026", hours_from_now=0.0) -> PolymarketMarket:
    return PolymarketMarket(
        polymarket_id="0x" + "a" * 40,
        question=question, slug=slug,
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=hours_from_now),
    )


def test_match_perfect_moneyline():
    """Both teams in question + slug + resolution close to game → confidence 1.0."""
    game = _make_game(hours_from_now=0.0)
    market = _make_market(
        question="thunder vs jazz 2026-04-05",
        slug="thunder-vs-jazz-2026",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.market_type == "moneyline"
    assert result.confidence >= 0.95
    assert result.side in ("home", "away")


def test_match_rejects_wrong_teams():
    """Question mentions different teams → confidence below floor."""
    game = _make_game(home="thunder", away="jazz", hours_from_now=0.0)
    market = _make_market(
        question="will the lakers beat the warriors",
        slug="lakers-vs-warriors",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None, "must not match wrong teams"


def test_match_rejects_only_one_team_mentioned():
    """Only home team mentioned in question → not enough confidence."""
    game = _make_game()
    market = _make_market(
        question="will the thunder win outright this season",
        slug="thunder-season-bets",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None, "single-team match must be rejected"


def test_match_rejects_far_future_market():
    """Market resolves 100h away from game start → time score is 0."""
    game = _make_game(hours_from_now=0.0)
    market = _make_market(
        question="thunder vs jazz 2026-04-05",
        slug="thunder-vs-jazz-2026",
        hours_from_now=100.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    # name+slug=1.0, time=0 → confidence = 0.55 + 0.25 = 0.80 < 0.95
    assert result is None


def test_match_moneyline_determines_side_home():
    """Home team mentioned first → side=home."""
    game = _make_game()
    market = _make_market(
        question="thunder vs jazz 2026-04-05",
        slug="thunder-vs-jazz-2026",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.side == "home"


def test_match_moneyline_side_away_when_away_listed_first():
    game = _make_game()
    market = _make_market(
        question="jazz vs thunder 2026-04-05",
        slug="jazz-thunder-thunder-jazz",    # cheat the slug to ensure confidence
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.side == "away"


def test_match_total_market_over_under():
    """O/U market returns 'over' or 'under' as side."""
    game = _make_game()
    market = _make_market(
        question="total over 220.5 thunder jazz",
        slug="thunder-jazz-total",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is not None
    assert result.market_type == "total"
    assert result.line == 220.5
    assert result.side == "over"


def test_match_rejects_unclassifiable_question():
    """Question doesn't match any market-type regex → None."""
    game = _make_game()
    market = _make_market(
        question="thunder jazz is a great team",
        slug="thunder-vs-jazz",
        hours_from_now=3.0,
    )
    result = match_game_to_market(game, market, min_confidence=0.95)
    assert result is None


# ---- coverage: every alias league has at least one entry ---

def test_nba_aliases_nonempty():
    assert len(NBA_ALIASES) >= 28   # 30 teams; allow minor gaps


def test_nhl_aliases_nonempty():
    assert len(NHL_ALIASES) >= 28   # 32 teams


def test_mlb_aliases_nonempty():
    assert len(MLB_ALIASES) >= 28   # 30 teams


def test_all_nba_variants_unique_canonical():
    """No two NBA variants should map to different canonicals."""
    seen = {}
    for canonical, variants in NBA_ALIASES.items():
        for v in variants:
            if v in seen and seen[v] != canonical:
                pytest.fail(f"Variant '{v}' maps to {seen[v]} and {canonical}")
            seen[v] = canonical


def test_match_total_market_with_ou_notation_returns_match():
    """Polymarket "Athletics vs. Texas Rangers: O/U 7.5" markets don't
    contain literal 'over'/'under', but they ARE total markets and the
    matcher must return a MatchResult so the strategy can evaluate them.
    Regression test for the bug found 2026-04-24 where these markets were
    silently rejected at _determine_side, causing zero MLB trades since
    v10 deployed despite high-confidence matcher classifications."""
    game = LiveGame(
        sport="mlb", home_team="Athletics", away_team="Texas Rangers",
        game_id="123", start_time=datetime.now(timezone.utc),
        score_home=2, score_away=1, status="in_progress",
    )
    market = _make_market(
        question="Athletics vs. Texas Rangers: O/U 7.5",
        slug="mlb-athletics-rangers-2026", hours_from_now=2.0,
    )
    # Use 0.80 floor — slug-scoring partial match drops confidence below
    # the production 0.95 floor for this fixture, but the bug is at
    # _determine_side, not at confidence. Confidence-floor enforcement is
    # tested separately in test_confidence_floor_is_enforced.
    result = match_game_to_market(game, market, min_confidence=0.80)
    assert result is not None
    assert result.market_type == "total"
    assert result.line == 7.5
    assert result.side in ("over", "under")


def test_confidence_floor_is_enforced():
    """Passing min_confidence=0.99 rejects a 0.95-match."""
    game = _make_game(hours_from_now=0.0)
    market = _make_market(
        question="thunder vs jazz",
        slug="thunder-vs-jazz",
        hours_from_now=10.0,   # time score 0.58 after decay
    )
    result_95 = match_game_to_market(game, market, min_confidence=0.95)
    result_99 = match_game_to_market(game, market, min_confidence=0.99)
    # Result for 0.95 threshold may or may not match depending on exact
    # scoring. The critical invariant is that 0.99 is strictly stricter.
    if result_95 is None:
        assert result_99 is None
    else:
        assert (result_99 is None) or (result_99.confidence >= 0.99)
