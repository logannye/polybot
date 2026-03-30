import pytest
from polybot.trading.executor import OrderExecutor, compute_limit_price
from polybot.trading.wallet import WalletManager
from unittest.mock import AsyncMock, MagicMock


class TestComputeLimitPrice:
    def test_buy_yes_places_above_best_bid(self):
        price = compute_limit_price(side="YES", best_bid=0.49, best_ask=0.51)
        assert 0.49 <= price <= 0.51

    def test_buy_no_places_above_best_bid(self):
        price = compute_limit_price(side="NO", best_bid=0.30, best_ask=0.35)
        assert 0.30 <= price <= 0.35

    def test_exit_crosses_spread(self):
        price = compute_limit_price(side="YES", best_bid=0.60, best_ask=0.65, is_exit=True)
        assert price <= 0.61


class TestWalletManager:
    def test_compute_shares_from_usd(self):
        wm = WalletManager.__new__(WalletManager)
        assert wm.compute_shares(usd_amount=20.0, price=0.50) == pytest.approx(40.0)

    def test_compute_shares_at_high_price(self):
        wm = WalletManager.__new__(WalletManager)
        assert wm.compute_shares(usd_amount=20.0, price=0.80) == pytest.approx(25.0)


class TestOrderExecutor:
    @pytest.fixture
    def executor(self):
        ex = OrderExecutor.__new__(OrderExecutor)
        ex._fill_timeout_seconds = 120
        return ex

    def test_should_cancel_stale_order(self, executor):
        assert executor.should_cancel_order(elapsed_seconds=130) is True

    def test_should_not_cancel_fresh_order(self, executor):
        assert executor.should_cancel_order(elapsed_seconds=60) is False


def test_compute_limit_price_cross_spread():
    price = compute_limit_price("YES", best_bid=0.40, best_ask=0.42, cross_spread=True)
    assert price == 0.42


def test_compute_limit_price_normal():
    price = compute_limit_price("YES", best_bid=0.40, best_ask=0.42, cross_spread=False)
    assert 0.40 < price <= 0.42


def test_compute_limit_price_exit_unchanged():
    price = compute_limit_price("YES", best_bid=0.40, best_ask=0.42, is_exit=True)
    assert price == 0.40


@pytest.mark.asyncio
async def test_place_order_records_strategy():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1, strategy="snipe")
    assert result is not None
    call_args = db.fetchval.call_args
    assert "strategy" in call_args[0][0]


@pytest.mark.asyncio
async def test_place_multi_leg_order():
    db = AsyncMock()
    db.fetchval = AsyncMock(side_effect=[1, 2])
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    legs = [
        {"token_id": "tok_a", "side": "YES", "price": 0.45, "size_usd": 5.0, "market_id": 1, "analysis_id": None},
        {"token_id": "tok_b", "side": "YES", "price": 0.35, "size_usd": 5.0, "market_id": 2, "analysis_id": None},
    ]
    results = await executor.place_multi_leg_order(legs, strategy="arbitrage")
    assert len(results) == 2
    assert all(r is not None for r in results)


@pytest.mark.asyncio
async def test_place_order_dry_run_skips_clob():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    clob = AsyncMock()
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db,
                              fill_timeout_seconds=120, clob=clob, dry_run=True)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1)
    assert result is not None
    assert result["order_id"] is None
    clob.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_live_calls_clob():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    clob = AsyncMock()
    clob.submit_order = AsyncMock(return_value="order-abc")
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db,
                              fill_timeout_seconds=120, clob=clob, dry_run=False)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1)
    assert result is not None
    assert result["order_id"] == "order-abc"
    clob.submit_order.assert_called_once()


@pytest.mark.asyncio
async def test_place_order_clob_failure_cancels():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    clob = AsyncMock()
    clob.submit_order = AsyncMock(side_effect=Exception("CLOB down"))
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db,
                              fill_timeout_seconds=120, clob=clob, dry_run=False)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1)
    assert result is None


@pytest.mark.asyncio
async def test_place_order_serializes_kelly_as_json_string():
    """kelly_inputs must be JSON-serialized to a string for asyncpg JSONB columns."""
    import json
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    kelly = {"edge": 0.05, "kelly_fraction": 0.12, "ensemble_prob": 0.65}
    await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1, kelly_inputs=kelly)
    args = db.fetchval.call_args[0]
    kelly_arg = args[7]  # $7 = kelly_inputs (args[0] is the SQL, args[1-9] are params)
    assert isinstance(kelly_arg, str), f"Expected str, got {type(kelly_arg)}: {kelly_arg}"
    parsed = json.loads(kelly_arg)
    assert parsed["edge"] == 0.05
    assert parsed["ensemble_prob"] == 0.65


@pytest.mark.asyncio
async def test_exit_position_computes_pnl_yes():
    """exit_position should compute correct PnL for YES trades."""
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "id": 1, "status": "dry_run", "side": "YES", "entry_price": 0.50,
        "shares": 20.0, "position_size_usd": 10.0, "strategy": "forecast",
        "market_id": 1,
    })
    db.execute = AsyncMock()
    wallet = MagicMock()
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    pnl = await executor.exit_position(trade_id=1, exit_price=0.60, exit_reason="take_profit")
    assert pnl == pytest.approx(2.0)  # 20 shares * (0.60 - 0.50)


@pytest.mark.asyncio
async def test_exit_position_computes_pnl_no():
    """exit_position should compute correct PnL for NO trades."""
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "id": 2, "status": "dry_run", "side": "NO", "entry_price": 0.40,
        "shares": 25.0, "position_size_usd": 10.0, "strategy": "snipe",
        "market_id": 1,
    })
    db.execute = AsyncMock()
    wallet = MagicMock()
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    # NO entry at 0.40, now NO is worth 0.50 → PnL = 25 * (0.50 - 0.40) = 2.50
    pnl = await executor.exit_position(trade_id=2, exit_price=0.50, exit_reason="take_profit")
    assert pnl == pytest.approx(2.50)


@pytest.mark.asyncio
async def test_exit_position_frees_deployed_capital():
    """exit_position should reduce total_deployed in system_state."""
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "id": 3, "status": "dry_run", "side": "YES", "entry_price": 0.50,
        "shares": 20.0, "position_size_usd": 10.0, "strategy": "forecast",
        "market_id": 1,
    })
    db.execute = AsyncMock()
    wallet = MagicMock()
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    await executor.exit_position(trade_id=3, exit_price=0.55, exit_reason="take_profit")

    # Should have 3 execute calls: update trade, update system_state, update strategy_performance
    assert db.execute.call_count == 3
    # The system_state update should subtract position_size
    system_call = db.execute.call_args_list[1]
    assert "total_deployed = total_deployed - $2" in system_call[0][0]


@pytest.mark.asyncio
async def test_exit_position_prevents_double_exit():
    """exit_position should return None if trade is already closed."""
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "id": 4, "status": "closed", "side": "YES", "entry_price": 0.50,
        "shares": 20.0, "position_size_usd": 10.0, "strategy": "forecast",
        "market_id": 1,
    })
    wallet = MagicMock()
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    pnl = await executor.exit_position(trade_id=4, exit_price=0.60, exit_reason="take_profit")
    assert pnl is None


@pytest.mark.asyncio
async def test_place_order_passes_empty_json_for_none_kelly():
    """When kelly_inputs is None, an empty JSON object string should be passed."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db, fill_timeout_seconds=120)
    await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1, kelly_inputs=None)
    args = db.fetchval.call_args[0]
    kelly_arg = args[7]
    assert isinstance(kelly_arg, str), f"Expected str, got {type(kelly_arg)}: {kelly_arg}"
    assert kelly_arg == "{}"
