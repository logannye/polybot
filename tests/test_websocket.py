import pytest
import asyncio
from unittest.mock import AsyncMock
from polybot.markets.websocket import PositionTracker, PriceStreamHub, should_early_exit, should_stop_loss


class TestShouldEarlyExit:
    def test_exit_when_edge_evaporated(self):
        assert should_early_exit(
            entry_price=0.50,
            current_price=0.58,
            side="YES",
            ensemble_prob=0.60,
            early_exit_edge=0.02,
        ) is True

    def test_no_exit_with_edge(self):
        assert should_early_exit(
            entry_price=0.50,
            current_price=0.52,
            side="YES",
            ensemble_prob=0.60,
            early_exit_edge=0.02,
        ) is False


class TestShouldStopLoss:
    def test_stop_when_price_moved_against(self):
        assert should_stop_loss(
            entry_price=0.50,
            current_price=0.35,
            side="YES",
        ) is True

    def test_no_stop_in_profit(self):
        assert should_stop_loss(
            entry_price=0.50,
            current_price=0.55,
            side="YES",
        ) is False

    def test_stop_no_side(self):
        assert should_stop_loss(
            entry_price=0.50,
            current_price=0.65,
            side="NO",
        ) is True


class TestPriceStreamHub:
    def test_subscribe_and_get_price(self):
        hub = PriceStreamHub()
        cb = AsyncMock()
        hub.subscribe("token_a", cb)
        assert hub.get_price("token_a") is None
        hub._price_cache["token_a"] = 0.55
        assert hub.get_price("token_a") == 0.55

    def test_unsubscribe_all(self):
        hub = PriceStreamHub()
        cb = AsyncMock()
        hub.subscribe("token_a", cb)
        hub.unsubscribe("token_a")
        assert "token_a" not in hub._subscribers

    def test_unsubscribe_specific_callback(self):
        hub = PriceStreamHub()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        hub.subscribe("token_a", cb1)
        hub.subscribe("token_a", cb2)
        hub.unsubscribe("token_a", cb1)
        assert cb2 in hub._subscribers["token_a"]
        assert cb1 not in hub._subscribers["token_a"]

    @pytest.mark.asyncio
    async def test_dispatch_calls_subscribers(self):
        hub = PriceStreamHub()
        cb = AsyncMock()
        hub.subscribe("token_a", cb)
        await hub._dispatch({"token_id": "token_a", "price": "0.65"})
        cb.assert_awaited_once_with("token_a", 0.65)
        assert hub.get_price("token_a") == 0.65

    @pytest.mark.asyncio
    async def test_dispatch_ignores_unknown_token(self):
        hub = PriceStreamHub()
        cb = AsyncMock()
        hub.subscribe("token_a", cb)
        await hub._dispatch({"token_id": "token_b", "price": "0.50"})
        cb.assert_not_awaited()
        # But price cache is still updated
        assert hub.get_price("token_b") == 0.50

    @pytest.mark.asyncio
    async def test_dispatch_handles_missing_fields(self):
        hub = PriceStreamHub()
        cb = AsyncMock()
        hub.subscribe("token_a", cb)
        await hub._dispatch({"token_id": "token_a"})  # no price
        cb.assert_not_awaited()
        await hub._dispatch({})  # no fields
        cb.assert_not_awaited()
