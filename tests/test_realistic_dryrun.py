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


def _make_executor(db, clob, realistic=True, max_spread=0.15, fee=0.02,
                   maker_fill_tolerance=0.02):
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
    settings.dry_run_maker_fill_tolerance = maker_fill_tolerance
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


# ── v12.2 maker-fill simulation ─────────────────────────────────────────

def _make_executor_v12_2(db, clob, *, assume_maker=True, max_spread=0.15,
                         maker_fill_tolerance=0.02):
    """Helper that wires the v12.2 dry_run_assume_maker_fill flag explicitly."""
    from polybot.trading.executor import OrderExecutor
    scanner = MagicMock()
    wallet = MagicMock()
    wallet.compute_shares.side_effect = lambda usd, price: usd / price if price > 0 else 0
    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=120, clob=clob, dry_run=True)
    settings = MagicMock()
    settings.dry_run = True
    settings.dry_run_realistic = True
    settings.dry_run_max_spread = max_spread
    settings.dry_run_taker_fee_pct = 0.02
    settings.dry_run_assume_maker_fill = assume_maker
    settings.dry_run_maker_fill_tolerance = maker_fill_tolerance
    executor._settings = settings
    return executor


@pytest.mark.asyncio
async def test_maker_fill_with_tight_book_succeeds():
    """v12.5: maker fill at limit price, tight book within tolerance.
    Limit 0.93, best_ask 0.94 → gap 0.01 ≤ 0.02 tolerance → fill at limit."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.91, "best_ask": 0.94, "spread": 0.03,
    })
    executor = _make_executor_v12_2(db, clob, assume_maker=True)
    result = await executor.place_order(
        token_id="tok1", side="NO", size_usd=20.0, price=0.93,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    assert result is not None
    insert_args = db.fetchval.call_args[0]
    entry_price = insert_args[4]
    assert entry_price == 0.93    # at limit, not best_ask


@pytest.mark.asyncio
async def test_maker_fill_rejects_when_book_too_wide():
    """v12.5: real maker fills require a counter-party near our price.
    Limit 0.93, best_ask 0.99 → gap 0.06 > 0.02 tolerance → REJECT.
    This is the gate that prevents fictional fills on the LOCKED markets
    where bid 0.001 / ask 0.999 means no real seller near our limit."""
    db = AsyncMock()
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.01, "best_ask": 0.99, "spread": 0.98,
    })
    executor = _make_executor_v12_2(db, clob, assume_maker=True)
    result = await executor.place_order(
        token_id="tok1", side="NO", size_usd=20.0, price=0.93,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    assert result is None
    db.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_maker_fill_tolerance_configurable():
    """Custom tolerance changes the rejection threshold. With tolerance=0.10,
    a 0.06 gap that would normally reject now fills."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.01, "best_ask": 0.99, "spread": 0.98,
    })
    executor = _make_executor_v12_2(
        db, clob, assume_maker=True, maker_fill_tolerance=0.10)
    result = await executor.place_order(
        token_id="tok1", side="NO", size_usd=20.0, price=0.93,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    assert result is not None    # gap 0.06 < tolerance 0.10 → fills


@pytest.mark.asyncio
async def test_maker_fill_at_exact_tolerance_boundary():
    """A gap of exactly the tolerance is treated as fillable (≤, not <)."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.90, "best_ask": 0.95, "spread": 0.05,
    })
    executor = _make_executor_v12_2(
        db, clob, assume_maker=True, maker_fill_tolerance=0.02)
    result = await executor.place_order(
        token_id="tok1", side="NO", size_usd=20.0, price=0.93,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    # gap 0.95 - 0.93 = 0.02, exactly at tolerance → fills
    assert result is not None


@pytest.mark.asyncio
async def test_maker_fill_zero_fee():
    """Maker fill should NOT apply the taker fee — 0% on the size_usd field.
    Book is tight (gap 0.92→0.93 = 0.01, within tolerance) so fill happens."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.90, "best_ask": 0.93, "spread": 0.03,
    })
    executor = _make_executor_v12_2(db, clob, assume_maker=True)
    await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.92,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    insert_args = db.fetchval.call_args[0]
    # In taker mode size_usd would be 10.0 * (1 - 0.02) = 9.80. Maker keeps full $10.
    size_usd_recorded = insert_args[5]
    assert size_usd_recorded == 10.0


@pytest.mark.asyncio
async def test_maker_fill_off_falls_back_to_taker_path():
    """When dry_run_assume_maker_fill=False, the original taker-mode logic
    applies: spread cap, taker fee, fill at best_ask."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.90, "best_ask": 0.94, "spread": 0.04,
    })
    executor = _make_executor_v12_2(db, clob, assume_maker=False)
    await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.92,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    insert_args = db.fetchval.call_args[0]
    # Taker path: filled at best_ask (0.94), with 2% fee on size_usd
    entry_price = insert_args[4]
    size_usd_recorded = insert_args[5]
    assert entry_price == 0.94
    assert size_usd_recorded == pytest.approx(9.80)


@pytest.mark.asyncio
async def test_maker_fill_requires_post_only():
    """Even with assume_maker=True, the maker path is gated on post_only=True.
    A taker order (post_only=False) still goes through the taker path."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.90, "best_ask": 0.94, "spread": 0.04,
    })
    executor = _make_executor_v12_2(db, clob, assume_maker=True)
    await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.92,
        market_id=1, analysis_id=None, strategy="snipe", post_only=False)
    insert_args = db.fetchval.call_args[0]
    entry_price = insert_args[4]
    # Taker path applied
    assert entry_price == 0.94


@pytest.mark.asyncio
async def test_maker_fill_still_rejects_no_book():
    """No book at all means we have nothing to post against — reject even
    in maker mode."""
    db = AsyncMock()
    clob = AsyncMock()
    clob.get_order_book_summary = AsyncMock(return_value=None)
    executor = _make_executor_v12_2(db, clob, assume_maker=True)
    result = await executor.place_order(
        token_id="tok1", side="YES", size_usd=10.0, price=0.92,
        market_id=1, analysis_id=None, strategy="snipe", post_only=True)
    assert result is None


# ── v12.2 wider-universe economic invariants ───────────────────────────

def test_v12_3_killswitch_drawdown_under_max_drawdown_halt():
    """The right invariant after the v12.3 cap-doubling isn't 'worst single
    trade < 2%' (the v12.2 framing) — it's 'killswitch-induced drawdown <
    max_total_drawdown_pct'. Killswitch trips on 3 losses in a 50-trade
    window; at the worst tier (low: 4% × 0.92 buy = 3.68%), 3 losses gives
    ~11% drawdown — well inside the 30% halt. Verify across all tiers."""
    from polybot.strategies.snipe import select_tier
    from types import SimpleNamespace
    s = SimpleNamespace(
        snipe_tier_high_min_conf=0.99, snipe_tier_high_min_edge=0.02,
        snipe_tier_high_max_pct=0.01,
        snipe_tier_mid_min_conf=0.97, snipe_tier_mid_min_edge=0.04,
        snipe_tier_mid_max_pct=0.02,
        snipe_tier_low_min_conf=0.95, snipe_tier_low_min_edge=0.06,
        snipe_tier_low_max_pct=0.04,
    )
    max_drawdown_halt = 0.30
    losses_to_trip_killswitch = 3    # (1 - 0.97) × 50 + 1 conservative
    for conf in (1.0, 0.98, 0.96):
        t = select_tier(conf, s)
        assert t is not None
        per_trade_loss = t.max_pct * 0.92                       # buy at 0.92
        killswitch_drawdown = per_trade_loss * losses_to_trip_killswitch
        assert killswitch_drawdown < max_drawdown_halt, (
            f"tier {t.name} drawdown {killswitch_drawdown:.4f} would breach "
            f"{max_drawdown_halt} halt before killswitch fires")
