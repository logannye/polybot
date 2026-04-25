"""Tests for v11.0b PregameSharpStrategy.

Pregame strategy uses ESPN BPI (predictor.gameProjection) as the
closing-line proxy. Entry window: 15-60 minutes before game start.
Exits: hold to resolution; pre-tip emergency if BPI WP < 0.50.

V1 is moneyline-only — pregame spread/total markets deferred.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from polybot.strategies.pregame_sharp import PregameSharpStrategy
from polybot.markets.sports_matcher import MatchResult, LiveGame, PolymarketMarket


def _settings():
    s = MagicMock()
    s.pg_interval_seconds = 60.0
    s.pg_kelly_mult = 0.40
    s.pg_max_single_pct = 0.12
    s.pg_min_edge = 0.04
    s.pg_min_book_depth = 5000.0
    s.pg_min_book_depth_dryrun = 1000.0
    s.pg_matcher_min_confidence = 0.95
    s.pg_min_calibrated_wp = 0.60
    s.pg_emergency_exit_wp = 0.50
    s.pg_take_profit_price = 0.95
    s.pg_min_minutes_to_start = 15
    s.pg_max_minutes_to_start = 60
    s.pg_max_bpi_staleness_s = 21600.0  # 6h
    s.dry_run = True
    s.min_trade_size = 1.0
    return s


def _strategy():
    return PregameSharpStrategy(settings=_settings(), espn_client=MagicMock())


def _ml_match(side: str = "home", confidence: float = 0.98) -> MatchResult:
    market = PolymarketMarket(
        polymarket_id="0x" + "a" * 40,
        question="Yankees vs. Red Sox",
        slug="mlb-bos-nyy-2026-04-25",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    return MatchResult(
        market=market,
        live_game=LiveGame(
            sport="mlb", home_team="Yankees", away_team="Red Sox",
            game_id="123", start_time=datetime.now(timezone.utc) + timedelta(minutes=30),
            score_home=0, score_away=0, status="scheduled",
        ),
        market_type="moneyline", side=side, confidence=confidence, line=None,
    )


# ---- _evaluate_pregame: structural -------------------------------------

def test_evaluate_pregame_yes_when_bpi_above_market():
    """ESPN BPI says 75% home, market priced at 65% home → bet YES."""
    strategy = _strategy()
    match = _ml_match(side="home")
    market_dict = {
        "polymarket_id": "0x" + "a" * 40,
        "yes_price": 0.65, "no_price": 0.35,
    }
    result = strategy._evaluate_pregame(
        match=match, home_win_prob=0.75, market_dict=market_dict)
    assert result is not None
    assert result["trade_side"] == "YES"
    assert result["prob_trade_wins"] == pytest.approx(0.75)


def test_evaluate_pregame_no_when_bpi_strongly_below_market():
    """ESPN BPI says 25% home (75% away), market YES (home) at 0.40 → bet NO.

    Both sides must clear the 0.60 calibrated-WP floor before being
    eligible. Coin-flip BPI (~50%) is correctly rejected (covered by
    test_evaluate_pregame_rejects_below_min_calibrated_wp).
    """
    strategy = _strategy()
    match = _ml_match(side="home")
    market_dict = {
        "polymarket_id": "0x" + "b" * 40,
        "yes_price": 0.40, "no_price": 0.60,   # market YES (home) underpriced
    }
    result = strategy._evaluate_pregame(
        match=match, home_win_prob=0.25, market_dict=market_dict)
    assert result is not None
    assert result["trade_side"] == "NO"
    # P(NO wins) = 1 - P(home wins) = 0.75
    assert result["prob_trade_wins"] == pytest.approx(0.75)


def test_evaluate_pregame_rejects_below_min_calibrated_wp():
    """Calibrated WP must clear 0.60 floor on either side."""
    strategy = _strategy()
    match = _ml_match(side="home")
    market_dict = {
        "polymarket_id": "0x" + "c" * 40,
        "yes_price": 0.50, "no_price": 0.50,
    }
    # 0.55 home WP → both sides under 0.60 floor → reject
    result = strategy._evaluate_pregame(
        match=match, home_win_prob=0.55, market_dict=market_dict)
    assert result is None


def test_evaluate_pregame_rejects_below_min_edge():
    """Even with calibrated WP above 0.60, reject if edge < 4%."""
    strategy = _strategy()
    match = _ml_match(side="home")
    market_dict = {
        "polymarket_id": "0x" + "d" * 40,
        "yes_price": 0.73, "no_price": 0.27,   # only 2% edge below ESPN's 0.75
    }
    result = strategy._evaluate_pregame(
        match=match, home_win_prob=0.75, market_dict=market_dict)
    assert result is None


def test_evaluate_pregame_away_side_market():
    """Matcher says the market resolves on AWAY team. Inversions handled."""
    strategy = _strategy()
    match = _ml_match(side="away")
    market_dict = {
        "polymarket_id": "0x" + "e" * 40,
        "yes_price": 0.30, "no_price": 0.70,   # market YES = away team wins
    }
    # ESPN says home 75% → away 25%, market YES (away) at 0.30 → 5% edge on NO
    result = strategy._evaluate_pregame(
        match=match, home_win_prob=0.75, market_dict=market_dict)
    # Calibrated wp on YES side (away winning) = 0.25, below 0.60 → also
    # check NO side: P(NO wins) = P(home wins) = 0.75 ≥ 0.60 ✓
    # Edge on NO = 0.75 - 0.70 = 0.05 ≥ 0.04 ✓
    assert result is not None
    assert result["trade_side"] == "NO"
    assert result["prob_trade_wins"] == pytest.approx(0.75)


# ---- timing window ------------------------------------------------------

def test_within_pregame_window_passes():
    strategy = _strategy()
    # 30 minutes from now is within [15, 60]
    start = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert strategy._within_pregame_window(start) is True


def test_outside_pregame_window_too_close():
    strategy = _strategy()
    start = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert strategy._within_pregame_window(start) is False


def test_outside_pregame_window_too_far():
    strategy = _strategy()
    start = datetime.now(timezone.utc) + timedelta(minutes=120)
    assert strategy._within_pregame_window(start) is False


def test_outside_pregame_window_already_started():
    strategy = _strategy()
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert strategy._within_pregame_window(start) is False
