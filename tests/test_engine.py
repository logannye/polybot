import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.core.engine import Engine


def make_engine():
    return Engine(
        db=AsyncMock(), scanner=MagicMock(), researcher=MagicMock(),
        ensemble=MagicMock(), executor=MagicMock(), recorder=MagicMock(),
        risk_manager=MagicMock(),
        settings=MagicMock(health_check_interval=60, heartbeat_warn_seconds=600,
                           heartbeat_critical_seconds=1800, balance_divergence_pct=0.05,
                           strategy_kill_min_trades=50, daily_loss_limit_pct=0.15),
        email_notifier=AsyncMock(), position_manager=MagicMock())


def test_engine_constructs():
    engine = make_engine()
    assert engine is not None


def test_add_strategy():
    engine = make_engine()
    strategy = MagicMock()
    strategy.name = "test"
    engine.add_strategy(strategy)
    assert len(engine._strategies) == 1


@pytest.mark.asyncio
async def test_run_strategy_calls_run_once():
    engine = make_engine()
    strategy = MagicMock()
    strategy.name = "test"
    strategy.interval_seconds = 0.001
    call_count = 0
    async def fake_run_once(ctx):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise asyncio.CancelledError
    strategy.run_once = fake_run_once
    await engine._run_strategy(strategy)
    assert call_count == 3


@pytest.mark.asyncio
async def test_cleanup_stale_arbs():
    """Arb positions older than max_hold_days should be closed."""
    engine = make_engine()
    engine._settings.arb_max_hold_days = 7.0
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 100, "position_size_usd": 10.0, "status": "dry_run"},
        {"id": 101, "position_size_usd": 10.0, "status": "dry_run"},
    ])
    engine._executor = AsyncMock()
    engine._executor.exit_position = AsyncMock(return_value=0.0)

    await engine._cleanup_stale_arbs()

    assert engine._executor.exit_position.call_count == 2
    engine._executor.exit_position.assert_any_call(
        trade_id=100, exit_price=0.0, exit_reason="arb_ttl_expired")
    engine._executor.exit_position.assert_any_call(
        trade_id=101, exit_price=0.0, exit_reason="arb_ttl_expired")


@pytest.mark.asyncio
async def test_cleanup_stale_arbs_no_stale():
    """No stale arbs means no exits triggered."""
    engine = make_engine()
    engine._settings.arb_max_hold_days = 7.0
    engine._db.fetch = AsyncMock(return_value=[])
    engine._executor = AsyncMock()

    await engine._cleanup_stale_arbs()
    engine._executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_run_strategy_disables_after_consecutive_errors(monkeypatch):
    # Patch asyncio.sleep to be instant during backoff
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    engine = make_engine()
    strategy = MagicMock()
    strategy.name = "failing"
    strategy.interval_seconds = 0.001
    call_count = 0
    async def always_fail(ctx):
        nonlocal call_count
        call_count += 1
        raise ValueError("boom")
    strategy.run_once = always_fail
    await engine._run_strategy(strategy)
    # Should disable after 30 consecutive errors (with exp backoff)
    assert call_count == 30
    assert engine._context.email_notifier.send.called
