import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_book(best_bid: str, best_ask: str):
    """Helper to create a mock order book."""
    book = MagicMock()
    ask = MagicMock()
    ask.price = best_ask
    bid = MagicMock()
    bid.price = best_bid
    book.asks = [ask]
    book.bids = [bid]
    return book


def _make_executor(db, clob, realistic=True, max_spread=0.15, fee=0.02):
    """Helper to create an executor with realistic dry-run settings."""
    from polybot.trading.executor import OrderExecutor
    scanner = MagicMock()
    wallet = MagicMock()
    wallet.compute_shares.side_effect = lambda usd, price: usd / price if price > 0 else 0

    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=120, clob=clob, dry_run=True)

    settings = MagicMock()
    settings.dry_run = True
    settings.dry_run_realistic = realistic
    settings.dry_run_max_spread = max_spread
    settings.dry_run_taker_fee_pct = fee
    executor._settings = settings
    return executor


@pytest.mark.asyncio
async def test_realistic_dryrun_rejects_wide_spread():
    """Dry-run should reject orders on markets with spread > threshold."""
    db = AsyncMock()
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.25, "best_ask": 0.75, "spread": 0.50,
    })

    executor = _make_executor(db, clob, max_spread=0.15)
    result = await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.50,
        market_id=1, analysis_id=1, strategy="mean_reversion")

    assert result is None


@pytest.mark.asyncio
async def test_realistic_dryrun_fills_at_best_ask():
    """Dry-run should fill at best ask price, not model price."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.48, "best_ask": 0.52, "spread": 0.04,
    })

    executor = _make_executor(db, clob)
    result = await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.50,
        market_id=1, analysis_id=1, strategy="mean_reversion")

    assert result is not None
    # Check the trade was inserted with best ask (0.52), not model (0.50)
    insert_args = db.fetchval.call_args[0]
    entry_price = insert_args[4]  # $4 = entry_price (index 4: sql, market_id, analysis_id, side, price)
    assert entry_price == 0.52


@pytest.mark.asyncio
async def test_non_realistic_dryrun_uses_model_price():
    """When dry_run_realistic=False, fill at model price (old behavior)."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = MagicMock()

    executor = _make_executor(db, clob, realistic=False)
    result = await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.50,
        market_id=1, analysis_id=1, strategy="mean_reversion")

    assert result is not None
    insert_args = db.fetchval.call_args[0]
    entry_price = insert_args[4]  # $4 = entry_price (index 4: sql, market_id, analysis_id, side, price)
    assert entry_price == 0.50
