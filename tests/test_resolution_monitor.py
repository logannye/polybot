import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from polybot.core.engine import Engine


def make_engine(**kwargs):
    defaults = dict(
        db=AsyncMock(), scanner=MagicMock(), researcher=MagicMock(),
        ensemble=MagicMock(), executor=MagicMock(), recorder=MagicMock(),
        risk_manager=MagicMock(),
        settings=MagicMock(
            health_check_interval=60, heartbeat_warn_seconds=600,
            heartbeat_critical_seconds=1800, balance_divergence_pct=0.05,
            strategy_kill_min_trades=50, daily_loss_limit_pct=0.15,
            fill_timeout_seconds=120, arb_fill_timeout_seconds=30,
            dry_run=False, post_breaker_cooldown_hours=24),
        email_notifier=AsyncMock(), position_manager=MagicMock(),
        clob=AsyncMock())
    defaults.update(kwargs)
    return Engine(**defaults)


@pytest.mark.asyncio
async def test_resolution_filled_trade():
    engine = make_engine()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "market_id": 10, "side": "YES", "entry_price": 0.60,
         "shares": 10.0, "position_size_usd": 6.0, "status": "filled", "strategy": "forecast"},
    ])
    engine._db.fetchrow = AsyncMock(return_value={"polymarket_id": "mkt-1", "resolution_time": past})
    engine._scanner.fetch_market_resolution = AsyncMock(return_value=1)
    engine._recorder.record_resolution = AsyncMock()
    engine._clob.get_balance = AsyncMock(return_value=106.0)
    engine._db.execute = AsyncMock()
    engine._db.fetchrow = AsyncMock(side_effect=[
        {"polymarket_id": "mkt-1", "resolution_time": past},  # market lookup
        {"pnl": 4.0},  # resolved trade pnl lookup
    ])
    await engine._resolution_monitor()
    engine._recorder.record_resolution.assert_called_once_with(1, 1)


@pytest.mark.asyncio
async def test_resolution_dry_run():
    engine = make_engine()
    engine._settings.dry_run = True
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 2, "market_id": 10, "side": "YES", "entry_price": 0.60,
         "shares": 10.0, "position_size_usd": 6.0, "status": "dry_run", "strategy": "snipe"},
    ])
    engine._db.fetchrow = AsyncMock(return_value={"polymarket_id": "mkt-1", "resolution_time": past})
    engine._scanner.fetch_market_resolution = AsyncMock(return_value=1)
    engine._db.execute = AsyncMock()
    await engine._resolution_monitor()
    calls = [str(c) for c in engine._db.execute.call_args_list]
    assert any("dry_run_resolved" in c for c in calls)


@pytest.mark.asyncio
async def test_resolution_skips_future():
    engine = make_engine()
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 3, "market_id": 10, "side": "YES", "entry_price": 0.60,
         "shares": 10.0, "position_size_usd": 6.0, "status": "filled", "strategy": "forecast"},
    ])
    engine._db.fetchrow = AsyncMock(return_value={"polymarket_id": "mkt-1", "resolution_time": future})
    engine._db.execute = AsyncMock()
    await engine._resolution_monitor()
    engine._db.execute.assert_not_called()
