import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.strategies.base import Strategy, TradingContext


class DummyStrategy(Strategy):
    name = "dummy"
    interval_seconds = 1.0
    kelly_multiplier = 0.25
    max_single_pct = 0.15

    def __init__(self):
        self.call_count = 0

    async def run_once(self, ctx: TradingContext) -> None:
        self.call_count += 1


def make_context():
    return TradingContext(
        db=MagicMock(), scanner=MagicMock(), risk_manager=MagicMock(),
        portfolio_lock=asyncio.Lock(), executor=MagicMock(),
        email_notifier=MagicMock(), settings=MagicMock())


def test_strategy_abc_requires_run_once():
    with pytest.raises(TypeError):
        Strategy()


def test_dummy_strategy_has_required_attrs():
    s = DummyStrategy()
    assert s.name == "dummy"
    assert s.interval_seconds == 1.0
    assert s.kelly_multiplier == 0.25
    assert s.max_single_pct == 0.15


@pytest.mark.asyncio
async def test_dummy_strategy_run_once():
    s = DummyStrategy()
    ctx = make_context()
    await s.run_once(ctx)
    assert s.call_count == 1


def test_trading_context_fields():
    ctx = make_context()
    assert ctx.db is not None
    assert ctx.scanner is not None
    assert isinstance(ctx.portfolio_lock, asyncio.Lock)
