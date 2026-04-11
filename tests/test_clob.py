import pytest
from unittest.mock import MagicMock
from polybot.trading.clob import ClobGateway


def test_clob_gateway_constructs():
    gw = ClobGateway(
        host="https://clob.polymarket.com", chain_id=137,
        private_key="0x" + "a" * 64, api_key="test-key",
        api_secret="test-secret", api_passphrase="test-pass")
    assert gw is not None


@pytest.mark.asyncio
async def test_submit_order_returns_order_id():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_order = MagicMock()
    mock_client.create_order.return_value = mock_order
    mock_client.post_order.return_value = {"orderID": "order-123"}
    gw._client = mock_client
    result = await gw.submit_order(token_id="tok-abc", side="BUY", price=0.55, size=10.0)
    assert result == "order-123"


@pytest.mark.asyncio
async def test_cancel_order_returns_true():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.cancel.return_value = {"canceled": True}
    gw._client = mock_client
    result = await gw.cancel_order("order-123")
    assert result is True


@pytest.mark.asyncio
async def test_get_order_status():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_order.return_value = {"status": "MATCHED", "size_matched": "10.0"}
    gw._client = mock_client
    result = await gw.get_order_status("order-123")
    assert result["status"] == "matched"
    assert result["size_matched"] == 10.0


@pytest.mark.asyncio
async def test_get_balance():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {"balance": "150500000"}
    gw._client = mock_client
    result = await gw.get_balance()
    assert abs(result - 150.50) < 0.01
