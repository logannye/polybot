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
async def test_fill_monitor_matched():
    engine = make_engine()
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "clob_order_id": "order-1", "strategy": "forecast",
         "position_size_usd": 5.0, "opened_at": datetime.now(timezone.utc)},
    ])
    engine._clob.get_order_status = AsyncMock(return_value={"status": "matched", "size_matched": 10.0})
    engine._clob.get_balance = AsyncMock(return_value=95.0)
    engine._db.execute = AsyncMock()
    await engine._fill_monitor()
    calls = [str(c) for c in engine._db.execute.call_args_list]
    assert any("filled" in c for c in calls)
    assert any("bankroll" in c for c in calls)


@pytest.mark.asyncio
async def test_fill_monitor_timeout():
    engine = make_engine()
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "clob_order_id": "order-1", "strategy": "forecast",
         "position_size_usd": 5.0,
         "opened_at": datetime.now(timezone.utc) - timedelta(seconds=200)},
    ])
    engine._clob.get_order_status = AsyncMock(return_value={"status": "live", "size_matched": 0})
    engine._clob.cancel_order = AsyncMock(return_value=True)
    engine._db.execute = AsyncMock()
    await engine._fill_monitor()
    engine._clob.cancel_order.assert_called_once_with("order-1")


@pytest.mark.asyncio
async def test_fill_monitor_arb_timeout():
    engine = make_engine()
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "clob_order_id": "order-1", "strategy": "arbitrage",
         "position_size_usd": 5.0,
         "opened_at": datetime.now(timezone.utc) - timedelta(seconds=35)},
    ])
    engine._clob.get_order_status = AsyncMock(return_value={"status": "live", "size_matched": 0})
    engine._clob.cancel_order = AsyncMock(return_value=True)
    engine._db.execute = AsyncMock()
    await engine._fill_monitor()
    engine._clob.cancel_order.assert_called_once()
