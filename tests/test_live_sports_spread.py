"""Tests for spread-market trading path in LiveSportsStrategy."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from polybot.strategies.live_sports import LiveSportsStrategy
from polybot.markets.sports_matcher import MatchResult, LiveGame, PolymarketMarket
from polybot.sports.win_prob import GameState


def _settings():
    s = MagicMock()
    s.lg_interval_seconds = 30.0
    s.lg_kelly_mult = 0.50
    s.lg_max_single_pct = 0.20
    s.lg_min_edge = 0.04
    s.lg_spread_min_edge = 0.06
    s.lg_spread_kelly_reduction = 0.50
    s.lg_min_win_prob = 0.85
    s.lg_min_win_prob_dryrun = 0.65
    s.dry_run = True
    s.lg_min_book_depth = 10000.0
    s.lg_matcher_min_confidence = 0.95
    s.lg_max_staleness_s = 60.0
    s.lg_max_hold_hours = 6.0
    s.lg_take_profit_price = 0.97
    s.lg_emergency_exit_wp = 0.70
    s.min_trade_size = 1.0
    return s


def _strategy():
    return LiveSportsStrategy(settings=_settings(), espn_client=MagicMock())


def _nba_late_leading() -> GameState:
    return GameState(
        sport="nba", score_home=110, score_away=100,
        period=4, clock_seconds=180, total_periods=4,
    )


def _spread_match(line: float, side: str) -> MatchResult:
    """Build a MatchResult with spread market_type."""
    market = PolymarketMarket(
        polymarket_id="0x" + "a" * 40,
        question=f"Spread: Team ({line:+})",
        slug="nba-xxx-yyy-2026",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    return MatchResult(
        market=market,
        live_game=LiveGame(sport="nba", home_team="Team A", away_team="Team B",
                           game_id="1", start_time=datetime.now(timezone.utc),
                           score_home=110, score_away=100, status="in_progress"),
        market_type="spread", side=side, confidence=0.98, line=line,
    )


def test_spread_evaluate_rejects_when_edge_below_bar():
    """10-point lead with plenty of time and line=-1.5: cover_prob high but
    poly_price near 1.0 could still be small edge → rejected."""
    strategy = _strategy()
    match = _spread_match(line=-1.5, side="home")
    state = _nba_late_leading()
    market_dict = {
        "polymarket_id": "0x" + "a" * 40,
        "yes_price": 0.95, "no_price": 0.05,   # priced high → tight edge
    }
    result = strategy._evaluate_spread(match, state, market_dict)
    assert result is None


def test_spread_evaluate_enters_yes_when_favorable_edge():
    """Strong cover probability vs a mispriced book → YES entry."""
    strategy = _strategy()
    match = _spread_match(line=-1.5, side="home")
    state = _nba_late_leading()
    market_dict = {
        "polymarket_id": "0x" + "b" * 40,
        "yes_price": 0.70, "no_price": 0.30,   # market underpricing cover
    }
    result = strategy._evaluate_spread(match, state, market_dict)
    assert result is not None
    assert result["trade_side"] == "YES"
    assert result["prob_trade_wins"] > 0.70
    # Kelly is reduced for spread
    assert result["kelly_override"] == pytest.approx(0.50 * 0.50)
    assert result["extra_kelly_inputs"]["market_type"] == "spread"
    assert result["extra_kelly_inputs"]["cover_side"] == "home"


def test_spread_evaluate_enters_no_when_cover_prob_low():
    """When market overprices cover, bet NO instead."""
    strategy = _strategy()
    # Tied game at halftime — small favorites struggle to cover -3.5
    match = _spread_match(line=-3.5, side="home")
    state = GameState(sport="nba", score_home=50, score_away=50,
                       period=3, clock_seconds=720, total_periods=4)
    market_dict = {
        "polymarket_id": "0x" + "c" * 40,
        "yes_price": 0.50, "no_price": 0.50,   # even-money book
    }
    result = strategy._evaluate_spread(match, state, market_dict)
    assert result is not None
    # NO should win — covering -3.5 at even from tied is hard
    assert result["trade_side"] == "NO"


def test_spread_evaluate_rejects_near_end_of_game():
    """When >95% of game elapsed, spread is effectively deterministic —
    skip entry (would be detecting resolved outcome)."""
    strategy = _strategy()
    match = _spread_match(line=-1.5, side="home")
    state = GameState(sport="nba", score_home=120, score_away=100,
                       period=4, clock_seconds=5, total_periods=4)
    market_dict = {
        "polymarket_id": "0x" + "d" * 40,
        "yes_price": 0.50, "no_price": 0.50,
    }
    result = strategy._evaluate_spread(match, state, market_dict)
    assert result is None


def test_spread_evaluate_rejects_missing_line():
    """Spread match without a line (shouldn't happen) is rejected."""
    strategy = _strategy()
    match = MatchResult(
        market=PolymarketMarket(
            polymarket_id="0x" + "e" * 40, question="Spread: ?",
            slug="", resolution_time=datetime.now(timezone.utc) + timedelta(hours=2),
        ),
        live_game=LiveGame(sport="nba", home_team="A", away_team="B",
                            game_id="1", start_time=datetime.now(timezone.utc),
                            score_home=110, score_away=100, status="in_progress"),
        market_type="spread", side="home", confidence=0.98, line=None,
    )
    result = strategy._evaluate_spread(match, _nba_late_leading(),
                                         {"polymarket_id": "0x" + "e" * 40})
    assert result is None


def test_spread_evaluate_unknown_sport_rejects():
    strategy = _strategy()
    match = _spread_match(line=-1.5, side="home")
    state = GameState(sport="cricket", score_home=5, score_away=3,
                       period=1, clock_seconds=0, total_periods=2)
    result = strategy._evaluate_spread(match, state,
                                         {"polymarket_id": "0x" + "f" * 40})
    assert result is None


# ---- moneyline path still unchanged ----

def test_moneyline_evaluate_unchanged_from_prior_behavior():
    """The moneyline helper should produce the same semantics as before."""
    strategy = _strategy()
    market = PolymarketMarket(
        polymarket_id="0x1" + "a" * 39, question="Team A vs. Team B",
        slug="", resolution_time=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    match = MatchResult(
        market=market,
        live_game=LiveGame(sport="nba", home_team="A", away_team="B",
                            game_id="1", start_time=datetime.now(timezone.utc),
                            score_home=110, score_away=100, status="in_progress"),
        market_type="moneyline", side="home", confidence=0.98, line=None,
    )
    state = _nba_late_leading()
    result = strategy._evaluate_moneyline(
        match, calibrated_wp=0.90, active_threshold=0.65, state=state,
        market_dict={})
    assert result is not None
    assert result["trade_side"] == "YES"
    assert result["prob_trade_wins"] == pytest.approx(0.90)
    # No kelly_override — moneyline uses full Kelly
    assert "kelly_override" not in result
