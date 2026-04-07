"""Tests for LiveGameCloserStrategy — match_game_to_market, compute_game_edge."""

import json
import pytest

from polybot.strategies.live_game import (
    match_game_to_market,
    match_game_to_all_markets,
    compute_game_edge,
    _build_search_tokens,
    _parse_outcomes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_game(
    espn_id="401585855",
    sport="nba",
    home_team="Cleveland Cavaliers",
    away_team="Memphis Grizzlies",
    home_score=110,
    away_score=95,
    period=4,
    clock="2:30",
    status="in_progress",
    completed=False,
) -> dict:
    return {
        "espn_id": espn_id,
        "sport": sport,
        "name": f"{away_team} at {home_team}",
        "short_name": f"MEM @ CLE",
        "home_team": home_team,
        "away_team": away_team,
        "home_abbrev": "CLE",
        "away_abbrev": "MEM",
        "home_score": home_score,
        "away_score": away_score,
        "period": period,
        "clock": clock,
        "status": status,
        "completed": completed,
    }


def _make_market(
    polymarket_id="cond_abc123",
    question="Cavaliers vs. Grizzlies",
    yes_price=0.88,
    yes_token_id="tok_yes_123",
    no_token_id="tok_no_123",
    outcomes=None,
    book_depth=50000.0,
    volume_24h=120000.0,
    category="sports",
) -> dict:
    return {
        "polymarket_id": polymarket_id,
        "question": question,
        "yes_price": yes_price,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "outcomes": outcomes if outcomes is not None else ["Cavaliers", "Grizzlies"],
        "book_depth": book_depth,
        "volume_24h": volume_24h,
        "category": category,
        "resolution_time": "2026-04-07T04:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# _build_search_tokens
# ---------------------------------------------------------------------------

class TestBuildSearchTokens:
    def test_full_name_and_mascot(self):
        tokens = _build_search_tokens("Cleveland Cavaliers")
        assert "cleveland cavaliers" in tokens
        assert "cavaliers" in tokens

    def test_single_word_name(self):
        tokens = _build_search_tokens("Heat")
        assert tokens == ["heat"]

    def test_empty_string(self):
        assert _build_search_tokens("") == []


# ---------------------------------------------------------------------------
# _parse_outcomes
# ---------------------------------------------------------------------------

class TestParseOutcomes:
    def test_list_passthrough(self):
        assert _parse_outcomes(["Cavaliers", "Grizzlies"]) == ["Cavaliers", "Grizzlies"]

    def test_json_string(self):
        assert _parse_outcomes('["Cavaliers", "Grizzlies"]') == ["Cavaliers", "Grizzlies"]

    def test_none_returns_empty(self):
        assert _parse_outcomes(None) == []

    def test_bad_json_returns_empty(self):
        assert _parse_outcomes("{not json}") == []


# ---------------------------------------------------------------------------
# match_game_to_market
# ---------------------------------------------------------------------------

class TestMatchGameToMarket:
    def test_match_nba_team_to_market(self):
        """ESPN game with full team names matches Polymarket question with mascots."""
        game = _make_game(
            home_team="Cleveland Cavaliers",
            away_team="Memphis Grizzlies",
        )
        market = _make_market(
            question="Cavaliers vs. Grizzlies",
            outcomes=["Cavaliers", "Grizzlies"],
        )
        price_cache = {market["polymarket_id"]: market}

        result = match_game_to_market(game, price_cache)

        assert result is not None
        assert result["polymarket_id"] == "cond_abc123"
        assert result["question"] == "Cavaliers vs. Grizzlies"
        assert result["yes_price"] == 0.88
        assert result["home_outcome"] == "Cavaliers"
        assert result["away_outcome"] == "Grizzlies"

    def test_match_returns_none_when_no_match(self):
        """No matching market in cache returns None."""
        game = _make_game(
            home_team="Portland Trail Blazers",
            away_team="Sacramento Kings",
        )
        market = _make_market(question="Cavaliers vs. Grizzlies")
        price_cache = {market["polymarket_id"]: market}

        result = match_game_to_market(game, price_cache)
        assert result is None

    def test_match_handles_partial_name(self):
        """'Lakers vs. Warriors' matches full ESPN team names via mascot tokens."""
        game = _make_game(
            home_team="Golden State Warriors",
            away_team="Los Angeles Lakers",
        )
        market = _make_market(
            polymarket_id="cond_lal_gsw",
            question="Lakers vs. Warriors",
            outcomes=["Lakers", "Warriors"],
        )
        price_cache = {market["polymarket_id"]: market}

        result = match_game_to_market(game, price_cache)

        assert result is not None
        assert result["polymarket_id"] == "cond_lal_gsw"
        # Lakers = away (Los Angeles Lakers), Warriors = home (Golden State Warriors)
        assert result["away_outcome"] == "Lakers"
        assert result["home_outcome"] == "Warriors"

    def test_match_only_one_team_no_match(self):
        """Only one team matching should not produce a match."""
        game = _make_game(
            home_team="Cleveland Cavaliers",
            away_team="Memphis Grizzlies",
        )
        # Only "Cavaliers" appears, not "Grizzlies"
        market = _make_market(question="Cavaliers vs. Celtics")
        price_cache = {market["polymarket_id"]: market}

        result = match_game_to_market(game, price_cache)
        assert result is None

    def test_match_with_json_string_outcomes(self):
        """Outcomes stored as a JSON string should be parsed correctly."""
        game = _make_game(
            home_team="Cleveland Cavaliers",
            away_team="Memphis Grizzlies",
        )
        market = _make_market(
            question="Cavaliers vs. Grizzlies",
            outcomes='["Cavaliers", "Grizzlies"]',
        )
        price_cache = {market["polymarket_id"]: market}

        result = match_game_to_market(game, price_cache)
        assert result is not None
        assert result["home_outcome"] == "Cavaliers"
        assert result["away_outcome"] == "Grizzlies"

    def test_match_returns_market_fields(self):
        """Returned dict includes all expected fields from the market."""
        game = _make_game()
        market = _make_market(
            book_depth=75000.0,
            volume_24h=200000.0,
            category="nba",
        )
        price_cache = {market["polymarket_id"]: market}

        result = match_game_to_market(game, price_cache)
        assert result is not None
        assert result["book_depth"] == 75000.0
        assert result["volume_24h"] == 200000.0
        assert result["category"] == "nba"
        assert result["yes_token_id"] == "tok_yes_123"
        assert result["no_token_id"] == "tok_no_123"


# ---------------------------------------------------------------------------
# compute_game_edge
# ---------------------------------------------------------------------------

class TestComputeGameEdge:
    def test_edge_detected_when_polymarket_lags(self):
        """WP=0.95, price=0.88 -> YES edge 0.07."""
        result = compute_game_edge(win_prob=0.95, polymarket_price=0.88)
        assert result["side"] == "YES"
        assert abs(result["edge"] - 0.07) < 1e-9
        assert result["buy_price"] == 0.88

    def test_edge_detected_on_no_side(self):
        """WP=0.10, price=0.20 -> NO edge 0.10, buy_price=0.80."""
        result = compute_game_edge(win_prob=0.10, polymarket_price=0.20)
        assert result["side"] == "NO"
        assert abs(result["edge"] - 0.10) < 1e-9
        assert abs(result["buy_price"] - 0.80) < 1e-9

    def test_no_edge_when_prices_aligned(self):
        """WP=0.90, price=0.91 -> negative YES edge, zero NO edge."""
        result = compute_game_edge(win_prob=0.90, polymarket_price=0.91)
        # YES edge = 0.90 - 0.91 = -0.01
        # NO edge = 0.91 - 0.90 = 0.01
        # NO side wins with edge 0.01
        assert result["side"] == "NO"
        assert abs(result["edge"] - 0.01) < 1e-9

    def test_edge_equal_both_sides(self):
        """When YES and NO edges are equal, YES side is preferred."""
        result = compute_game_edge(win_prob=0.50, polymarket_price=0.50)
        assert result["side"] == "YES"
        assert abs(result["edge"]) < 1e-9

    def test_large_yes_edge(self):
        """WP=0.99, price=0.80 -> YES edge 0.19."""
        result = compute_game_edge(win_prob=0.99, polymarket_price=0.80)
        assert result["side"] == "YES"
        assert abs(result["edge"] - 0.19) < 1e-9
        assert result["buy_price"] == 0.80

    def test_large_no_edge(self):
        """WP=0.05, price=0.40 -> NO edge 0.35."""
        result = compute_game_edge(win_prob=0.05, polymarket_price=0.40)
        assert result["side"] == "NO"
        assert abs(result["edge"] - 0.35) < 1e-9
        assert abs(result["buy_price"] - 0.60) < 1e-9


# ---------------------------------------------------------------------------
# match_game_to_all_markets
# ---------------------------------------------------------------------------

class TestMatchGameToAllMarkets:
    def test_match_finds_moneyline_and_spread(self):
        """Should find both the moneyline and spread markets for the same game."""
        game = {
            "home_team": "Memphis Grizzlies",
            "away_team": "Cleveland Cavaliers",
            "sport": "nba",
        }
        price_cache = {
            "0xabc": {
                "polymarket_id": "0xabc",
                "question": "Cavaliers vs. Grizzlies",
                "outcomes": '["Cavaliers", "Grizzlies"]',
                "yes_price": 0.87,
                "yes_token_id": "tok1", "no_token_id": "tok2",
                "book_depth": 50000, "volume_24h": 1000000,
                "category": "sports",
            },
            "0xdef": {
                "polymarket_id": "0xdef",
                "question": "Spread: Cavaliers (-13.5)",
                "outcomes": '["Cavaliers -13.5", "Grizzlies +13.5"]',
                "yes_price": 0.55,
                "yes_token_id": "tok3", "no_token_id": "tok4",
                "book_depth": 30000, "volume_24h": 500000,
                "category": "sports",
            },
            "0xzzz": {
                "polymarket_id": "0xzzz",
                "question": "Will Trump win 2028?",
                "yes_price": 0.30,
            },
        }
        matches = match_game_to_all_markets(game, price_cache)
        assert len(matches) == 2
        pids = {m["polymarket_id"] for m in matches}
        assert "0xabc" in pids
        assert "0xdef" in pids

    def test_match_all_returns_empty_on_no_match(self):
        game = {"home_team": "Boston Celtics", "away_team": "Miami Heat", "sport": "nba"}
        matches = match_game_to_all_markets(game, {"0x1": {"question": "unrelated", "polymarket_id": "0x1"}})
        assert matches == []
