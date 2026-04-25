"""Tests for total (O/U) market trading path in LiveSportsStrategy."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
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
    s.lg_total_min_edge = 0.05
    s.lg_total_kelly_reduction = 0.50
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


def _mlb_late_high_total() -> GameState:
    """MLB: 12 total runs by 8th inning — already over most lines."""
    return GameState(
        sport="mlb", score_home=7, score_away=5,
        period=8, clock_seconds=0, total_periods=9, outs=2,
    )


def _nba_midgame_on_pace() -> GameState:
    """NBA half-time, on pace for 210 (likely under most NBA lines)."""
    return GameState(
        sport="nba", score_home=55, score_away=50,
        period=3, clock_seconds=720, total_periods=4,
    )


def _total_match(line: float, side: str = "over") -> MatchResult:
    """Build a MatchResult with total market_type."""
    market = PolymarketMarket(
        polymarket_id="0x" + "a" * 40,
        question=f"O/U {line}",
        slug="mlb-xxx-yyy-2026",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    return MatchResult(
        market=market,
        live_game=LiveGame(sport="mlb", home_team="Team A", away_team="Team B",
                           game_id="1", start_time=datetime.now(timezone.utc),
                           score_home=7, score_away=5, status="in_progress"),
        market_type="total", side=side, confidence=0.99, line=line,
    )


def test_total_evaluate_enters_yes_when_underpriced_over():
    """Late MLB game already over the line → YES (over) entry."""
    strategy = _strategy()
    match = _total_match(line=8.5)
    state = _mlb_late_high_total()
    market_dict = {
        "polymarket_id": "0x" + "a" * 40,
        "yes_price": 0.70, "no_price": 0.30,   # market underpricing the over
    }
    result = strategy._evaluate_total(match, state, market_dict)
    assert result is not None
    assert result["trade_side"] == "YES"
    assert result["prob_trade_wins"] > 0.90
    assert result["kelly_override"] == pytest.approx(0.50 * 0.50)
    assert result["extra_kelly_inputs"]["market_type"] == "total"
    assert result["extra_kelly_inputs"]["total_line"] == 8.5


def test_total_evaluate_enters_no_when_overpriced_over():
    """When market overprices the over but pace says under, bet NO."""
    strategy = _strategy()
    # NBA half-time on pace for 200 (line 215)
    match = _total_match(line=215.0)
    state = _nba_midgame_on_pace()
    match = MatchResult(
        market=match.market,
        live_game=LiveGame(sport="nba", home_team="A", away_team="B",
                            game_id="1", start_time=datetime.now(timezone.utc),
                            score_home=55, score_away=50, status="in_progress"),
        market_type="total", side="over", confidence=0.99, line=215.0,
    )
    market_dict = {
        "polymarket_id": "0x" + "b" * 40,
        "yes_price": 0.50, "no_price": 0.50,   # even-money but pace is under
    }
    result = strategy._evaluate_total(match, state, market_dict)
    assert result is not None
    # On-pace for 210, line 215 → P(over) ~0.36, edge on NO ~0.14
    assert result["trade_side"] == "NO"


def test_total_evaluate_rejects_when_edge_below_bar():
    """When market accurately prices the over, no trade."""
    strategy = _strategy()
    # MLB heavy-scoring late game, line 8.5, market correctly thinks over
    match = _total_match(line=8.5)
    state = _mlb_late_high_total()
    market_dict = {
        "polymarket_id": "0x" + "c" * 40,
        "yes_price": 0.96, "no_price": 0.04,   # market priced fully
    }
    result = strategy._evaluate_total(match, state, market_dict)
    assert result is None


def test_total_evaluate_rejects_near_end_of_game():
    """When >95% of game elapsed, total is effectively deterministic."""
    strategy = _strategy()
    # NBA Q4 with 5s left
    state = GameState(sport="nba", score_home=110, score_away=100,
                       period=4, clock_seconds=5, total_periods=4)
    match = MatchResult(
        market=PolymarketMarket(
            polymarket_id="0x" + "d" * 40, question="O/U 200",
            slug="", resolution_time=datetime.now(timezone.utc) + timedelta(hours=2),
        ),
        live_game=LiveGame(sport="nba", home_team="A", away_team="B",
                            game_id="1", start_time=datetime.now(timezone.utc),
                            score_home=110, score_away=100, status="in_progress"),
        market_type="total", side="over", confidence=0.98, line=200.0,
    )
    market_dict = {
        "polymarket_id": "0x" + "d" * 40,
        "yes_price": 0.50, "no_price": 0.50,
    }
    result = strategy._evaluate_total(match, state, market_dict)
    assert result is None


def test_total_evaluate_rejects_missing_line():
    strategy = _strategy()
    match = MatchResult(
        market=PolymarketMarket(
            polymarket_id="0x" + "e" * 40, question="O/U ?",
            slug="", resolution_time=datetime.now(timezone.utc) + timedelta(hours=2),
        ),
        live_game=LiveGame(sport="mlb", home_team="A", away_team="B",
                            game_id="1", start_time=datetime.now(timezone.utc),
                            score_home=7, score_away=5, status="in_progress"),
        market_type="total", side="over", confidence=0.98, line=None,
    )
    result = strategy._evaluate_total(
        match, _mlb_late_high_total(), {"polymarket_id": "0x" + "e" * 40})
    assert result is None


def test_total_evaluate_unknown_sport_rejects():
    strategy = _strategy()
    match = _total_match(line=8.5)
    state = GameState(sport="cricket", score_home=5, score_away=3,
                       period=1, clock_seconds=0, total_periods=2)
    result = strategy._evaluate_total(
        match, state, {"polymarket_id": "0x" + "f" * 40})
    assert result is None
