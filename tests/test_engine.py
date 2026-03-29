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
            raise KeyboardInterrupt
    strategy.run_once = fake_run_once
    with pytest.raises(KeyboardInterrupt):
        await engine._run_strategy(strategy)
    assert call_count == 3


@pytest.mark.asyncio
async def test_run_strategy_disables_after_5_errors():
    engine = make_engine()
    strategy = MagicMock()
    strategy.name = "failing"
    strategy.interval_seconds = 0.001
    async def always_fail(ctx):
        raise ValueError("boom")
    strategy.run_once = always_fail
    await engine._run_strategy(strategy)
    assert engine._context.email_notifier.send.called
