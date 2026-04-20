"""Tests for polybot.strategies.live_sports — v10 Live Sports engine."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from polybot.strategies.live_sports import (
    LiveSportsStrategy, espn_game_to_live_game, espn_game_to_game_state,
    _parse_clock,
)
from polybot.sports.win_prob import GameState


# ---- ESPN game conversion -----------------------------------------------

def test_espn_to_live_game_happy():
    espn = {
        "sport": "nba", "espn_id": "401",
        "home_team": "Thunder", "away_team": "Jazz",
        "home_score": 50, "away_score": 45,
        "status": "in_progress", "period": 3, "clock": "5:30",
    }
    result = espn_game_to_live_game(espn)
    assert result is not None
    assert result.sport == "nba"
    assert result.home_team == "Thunder"
    assert result.score_home == 50


def test_espn_to_live_game_skip_scheduled():
    espn = {
        "sport": "nba", "espn_id": "401",
        "home_team": "Thunder", "away_team": "Jazz",
        "status": "scheduled",
    }
    assert espn_game_to_live_game(espn) is None


def test_espn_to_live_game_skip_unsupported_sport():
    espn = {
        "sport": "cricket", "status": "in_progress",
        "home_team": "A", "away_team": "B",
    }
    assert espn_game_to_live_game(espn) is None


def test_espn_to_game_state_parses_clock():
    espn = {
        "sport": "nba", "period": 4,
        "home_score": 100, "away_score": 95, "clock": "2:30",
        "status": "in_progress",
    }
    state = espn_game_to_game_state(espn)
    assert state is not None
    assert state.sport == "nba"
    assert state.period == 4
    assert state.clock_seconds == 150.0


def test_parse_clock_various_formats():
    assert _parse_clock("2:30") == 150.0
    assert _parse_clock("0:05") == 5.0
    assert _parse_clock("45.5") == 45.5
    assert _parse_clock("bogus") == 0.0
    assert _parse_clock("") == 0.0


# ---- Entry gate -----------------------------------------------------------

def _base_settings():
    s = MagicMock()
    s.lg_interval_seconds = 30.0
    s.lg_kelly_mult = 0.50
    s.lg_max_single_pct = 0.20
    s.lg_min_edge = 0.04
    s.lg_min_win_prob = 0.85
    s.lg_min_book_depth = 10000.0
    s.lg_matcher_min_confidence = 0.95
    s.lg_max_staleness_s = 60.0
    s.lg_max_hold_hours = 6.0
    s.lg_take_profit_price = 0.97
    s.lg_emergency_exit_wp = 0.70
    s.min_trade_size = 1.0
    return s


def _strategy_with_mocked_espn(live_games):
    espn = MagicMock()
    espn.fetch_all_live_games = AsyncMock(return_value=live_games)
    return LiveSportsStrategy(settings=_base_settings(), espn_client=espn)


def _mock_ctx(scanner_markets=None, existing_trades=0):
    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=lambda *args, **kwargs: existing_trades
                                  if "SELECT COUNT(*)" in str(args[0]) else 1)
    ctx.db.fetchrow = AsyncMock(return_value={"bankroll": 2000.0, "total_deployed": 0.0})
    ctx.db.fetch = AsyncMock(return_value=[])   # no open trades
    ctx.scanner = AsyncMock()
    ctx.scanner.fetch_sports_markets = AsyncMock(return_value=scanner_markets or [])
    ctx.executor = AsyncMock()
    ctx.portfolio_lock = asyncio.Lock()
    ctx.settings = _base_settings()
    return ctx


@pytest.mark.asyncio
async def test_entry_gate_blocks_low_edge():
    """Edge below lg_min_edge (0.04) should skip."""
    espn_games = [{
        "sport": "nba", "espn_id": "1", "home_team": "thunder", "away_team": "jazz",
        "home_score": 100, "away_score": 98, "status": "in_progress",
        "period": 4, "clock": "1:00",
    }]
    market = {
        "polymarket_id": "0x" + "a" * 40,
        "question": "thunder vs jazz 2026-04-05",
        "slug": "thunder-vs-jazz", "resolution_time": datetime.now(timezone.utc),
        "yes_price": 0.90, "no_price": 0.10, "book_depth": 20000.0,
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }
    strategy = _strategy_with_mocked_espn(espn_games)
    ctx = _mock_ctx(scanner_markets=[market])
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_entry_gate_blocks_insufficient_depth():
    """Book depth < lg_min_book_depth should skip."""
    espn_games = [{
        "sport": "nba", "espn_id": "1", "home_team": "thunder", "away_team": "jazz",
        "home_score": 120, "away_score": 100, "status": "in_progress",
        "period": 4, "clock": "1:00",
    }]
    market = {
        "polymarket_id": "0x" + "b" * 40,
        "question": "thunder vs jazz 2026-04-05",
        "slug": "thunder-vs-jazz", "resolution_time": datetime.now(timezone.utc),
        "yes_price": 0.60, "no_price": 0.40,
        "book_depth": 100.0,   # way below $10K
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }
    strategy = _strategy_with_mocked_espn(espn_games)
    ctx = _mock_ctx(scanner_markets=[market])
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_entry_gate_blocks_existing_position():
    espn_games = [{
        "sport": "nba", "espn_id": "1", "home_team": "thunder", "away_team": "jazz",
        "home_score": 120, "away_score": 100, "status": "in_progress",
        "period": 4, "clock": "1:00",
    }]
    market = {
        "polymarket_id": "0x" + "c" * 40,
        "question": "thunder vs jazz 2026-04-05",
        "slug": "thunder-vs-jazz", "resolution_time": datetime.now(timezone.utc),
        "yes_price": 0.60, "no_price": 0.40, "book_depth": 20000.0,
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }
    strategy = _strategy_with_mocked_espn(espn_games)
    ctx = _mock_ctx(scanner_markets=[market], existing_trades=1)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_entry_gate_passes_when_all_conditions_met():
    """Thunder up big in Q4, matching market, fresh data — should place order."""
    espn_games = [{
        "sport": "nba", "espn_id": "1", "home_team": "thunder", "away_team": "jazz",
        "home_score": 120, "away_score": 100, "status": "in_progress",
        "period": 4, "clock": "1:00",
    }]
    market = {
        "polymarket_id": "0x" + "d" * 40,
        "question": "thunder vs jazz 2026-04-05",
        "slug": "thunder-vs-jazz", "resolution_time": datetime.now(timezone.utc),
        "yes_price": 0.70, "no_price": 0.30,   # 0.97+ calibrated WP − 0.70 market = big edge
        "book_depth": 20000.0,
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }
    strategy = _strategy_with_mocked_espn(espn_games)
    ctx = _mock_ctx(scanner_markets=[market])
    # fetchval needs to return 0 for dedup check, market_id for insert
    fetchval_calls = {"count": 0}
    async def fetchval_side_effect(sql, *args):
        if "SELECT COUNT(*)" in sql:
            return 0
        if "INSERT INTO markets" in sql:
            return 42
        return None
    ctx.db.fetchval = AsyncMock(side_effect=fetchval_side_effect)

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_called_once()
    call_kwargs = ctx.executor.place_order.call_args.kwargs
    assert call_kwargs["side"] in ("YES", "NO")
    assert call_kwargs["post_only"] is True   # maker-first
    assert call_kwargs["strategy"] == "live_sports"


@pytest.mark.asyncio
async def test_no_action_when_espn_returns_empty():
    strategy = _strategy_with_mocked_espn([])
    ctx = _mock_ctx(scanner_markets=[])
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_exits_trigger_time_stop_after_six_hours():
    """A trade opened 7h ago should be force-closed."""
    strategy = _strategy_with_mocked_espn([])
    ctx = _mock_ctx(scanner_markets=[])
    seven_h_ago = datetime.now(timezone.utc) - timedelta(hours=7)
    ctx.db.fetch = AsyncMock(return_value=[{
        "id": 1, "strategy": "live_sports", "status": "dry_run",
        "polymarket_id": "0x" + "e" * 40, "question": "t vs j",
        "opened_at": seven_h_ago, "yes_token_id": "tok", "no_token_id": "",
    }])
    await strategy.run_once(ctx)
    ctx.executor.exit_position.assert_called_once()
    call_kwargs = ctx.executor.exit_position.call_args.kwargs
    assert call_kwargs["exit_reason"] == "time_stop"


@pytest.mark.asyncio
async def test_exits_trigger_take_profit_at_0_97():
    """A trade where the market price hit 0.97 should TP."""
    strategy = _strategy_with_mocked_espn([])
    ctx = _mock_ctx(scanner_markets=[])
    ctx.db.fetch = AsyncMock(return_value=[{
        "id": 1, "strategy": "live_sports", "status": "dry_run",
        "polymarket_id": "0x" + "f" * 40, "question": "t vs j",
        "opened_at": datetime.now(timezone.utc) - timedelta(minutes=30),
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }])
    ctx.scanner.fetch_order_book = AsyncMock(return_value={
        "bids": [{"price": "0.97", "size": "100"}],
        "asks": [{"price": "0.98", "size": "100"}],
    })
    await strategy.run_once(ctx)
    ctx.executor.exit_position.assert_called_once()
    call_kwargs = ctx.executor.exit_position.call_args.kwargs
    assert call_kwargs["exit_reason"] == "take_profit"


@pytest.mark.asyncio
async def test_no_exit_when_within_hold_and_price_below_tp():
    """Young trade with price below TP should not exit."""
    strategy = _strategy_with_mocked_espn([])
    ctx = _mock_ctx(scanner_markets=[])
    ctx.db.fetch = AsyncMock(return_value=[{
        "id": 1, "strategy": "live_sports", "status": "dry_run",
        "polymarket_id": "0x" + "a" * 40, "question": "t vs j",
        "opened_at": datetime.now(timezone.utc) - timedelta(minutes=30),
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }])
    ctx.scanner.fetch_order_book = AsyncMock(return_value={
        "bids": [{"price": "0.80", "size": "100"}],
        "asks": [{"price": "0.85", "size": "100"}],
    })
    await strategy.run_once(ctx)
    ctx.executor.exit_position.assert_not_called()
