import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

from polybot.strategies.market_maker import MarketMakerStrategy
from polybot.trading.inventory import InventoryTracker
from polybot.trading.quote_manager import QuoteManager, ActiveMarket


def _make_settings():
    s = MagicMock()
    s.mm_cycle_seconds = 5.0
    s.mm_selection_interval_seconds = 300.0
    s.mm_kelly_mult = 0.15
    s.mm_max_single_pct = 0.10
    s.mm_max_total_pct = 0.30
    s.mm_max_markets = 3
    s.mm_base_spread_bps = 200
    s.mm_min_spread_bps = 50
    s.mm_max_spread_bps = 500
    s.mm_quote_size_usd = 10.0
    s.mm_max_inventory_per_market = 50.0
    s.mm_max_total_inventory = 200.0
    s.mm_max_skew_bps = 100
    s.mm_requote_threshold = 0.005
    s.mm_min_volume_24h = 5000.0
    s.mm_min_resolution_hours = 168.0
    s.mm_emergency_vol_threshold = 0.15
    s.mm_volatility_pullback_mult = 2.0
    s.mm_min_book_depth = 1000.0
    return s


def _make_market(polymarket_id="m1", price=0.50, volume=10000.0, depth=5000.0, hours=500):
    return {
        "polymarket_id": polymarket_id,
        "question": "Test market?",
        "category": "politics",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=hours),
        "yes_price": price,
        "no_price": 1 - price,
        "yes_token_id": f"{polymarket_id}_yes",
        "no_token_id": f"{polymarket_id}_no",
        "volume_24h": volume,
        "book_depth": depth,
    }


class TestMarketSelection:
    @pytest.mark.asyncio
    async def test_selects_markets_by_score(self):
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", price=0.50, volume=20000, depth=8000),
            _make_market("m2", price=0.50, volume=10000, depth=5000),
            _make_market("m3", price=0.90, volume=30000, depth=10000),  # rejected: price > 0.90
            _make_market("m4", price=0.50, volume=1000, depth=5000),    # rejected: volume < 5000
        ])

        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner)
        ctx = MagicMock()

        await strategy._select_markets(ctx)

        assert len(strategy._active_markets) == 2
        assert "m1" in strategy._active_markets
        assert "m2" in strategy._active_markets
        assert "m3" not in strategy._active_markets
        assert "m4" not in strategy._active_markets

    @pytest.mark.asyncio
    async def test_filters_short_resolution(self):
        settings = _make_settings()
        clob = AsyncMock()
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", hours=24),  # rejected: < 168h min
        ])
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner)
        ctx = MagicMock()
        await strategy._select_markets(ctx)
        assert len(strategy._active_markets) == 0


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_sent(self):
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb2")
        scanner = MagicMock()
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner)

        await strategy._send_heartbeat()
        clob.send_heartbeat.assert_awaited_once()
        assert strategy._last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_heartbeat_failure_marks_stale(self):
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(side_effect=Exception("network"))
        scanner = MagicMock()
        qm = MagicMock()
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       quote_manager=qm)

        await strategy._send_heartbeat()
        qm.mark_all_stale.assert_called_once()


class TestQuoteManagement:
    @pytest.mark.asyncio
    async def test_manage_quotes_places_two_sided(self):
        settings = _make_settings()
        clob = AsyncMock()
        clob.submit_order = AsyncMock(return_value="ord1")
        scanner = MagicMock()
        scanner.get_cached_price = MagicMock(return_value={
            "yes_price": 0.50, "no_price": 0.50,
        })
        qm = AsyncMock()
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       quote_manager=qm)

        market = ActiveMarket(
            polymarket_id="m1", yes_token_id="t1", no_token_id="t2",
            category="politics", max_incentive_spread=0.05,
            min_incentive_size=10.0, fair_value=0.50)

        ctx = MagicMock()
        ctx.db = AsyncMock()
        await strategy._manage_quotes(market, ctx)
        qm.requote.assert_awaited_once()


class TestDryRunNoSimulation:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_simulate_fills(self):
        """Dry-run run_once should NOT call _simulate_fills (method removed)."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[])
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       dry_run=True)
        assert not hasattr(strategy, '_simulate_fills')
        assert not hasattr(strategy, '_sim_pnl')
        assert not hasattr(strategy, '_sim_fills')
        assert not hasattr(strategy, '_prev_prices')

    @pytest.mark.asyncio
    async def test_dry_run_logs_quote_summary(self):
        """Dry-run run_once should log active market count."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", price=0.50, volume=20000, depth=8000),
        ])
        scanner.get_cached_price = MagicMock(return_value={
            "yes_price": 0.50, "no_price": 0.50,
        })
        qm = AsyncMock()
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       dry_run=True, quote_manager=qm)
        ctx = MagicMock()
        ctx.db = AsyncMock()
        await strategy.run_once(ctx)
        # Should not crash; quoting still runs in dry-run


class TestEmergencyWithdraw:
    @pytest.mark.asyncio
    async def test_emergency_withdraw_cancels_all(self):
        settings = _make_settings()
        clob = AsyncMock()
        scanner = MagicMock()
        qm = AsyncMock()
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       quote_manager=qm)
        strategy._active_markets = {"m1": MagicMock()}

        await strategy.emergency_withdraw()
        qm.cancel_all_quotes.assert_awaited_once()
        assert len(strategy._active_markets) == 0


class TestInventoryReconciliation:
    @pytest.mark.asyncio
    async def test_reconcile_inventory_seeds_from_db(self):
        """First run_once should seed inventory from filled MM trades in DB."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[])
        inv = InventoryTracker(max_per_market=50.0, max_total=200.0, max_skew_bps=100)
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       inventory=inv)

        ctx = MagicMock()
        ctx.db = AsyncMock()
        # YES fills -> BUY (adds to yes_shares), NO fills -> SELL (subtracts from yes_shares)
        ctx.db.fetch = AsyncMock(return_value=[
            {"polymarket_id": "m1", "side": "YES", "total_shares": 25.0},
            {"polymarket_id": "m1", "side": "NO", "total_shares": 10.0},
        ])

        assert strategy._inventory_reconciled is False
        await strategy.run_once(ctx)
        assert strategy._inventory_reconciled is True

        m1_inv = inv.get_inventory("m1")
        assert m1_inv is not None
        # record_fill("BUY", 0.50, 25) -> yes_shares=25, then
        # record_fill("SELL", 0.50, 10) -> yes_shares=15 (net long 15 YES)
        assert m1_inv.yes_shares == 15.0

    @pytest.mark.asyncio
    async def test_reconcile_inventory_runs_once(self):
        """Inventory reconciliation should only run on first run_once."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[])
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner)

        ctx = MagicMock()
        ctx.db = AsyncMock()
        ctx.db.fetch = AsyncMock(return_value=[])

        await strategy.run_once(ctx)
        await strategy.run_once(ctx)
        # fetch should be called once for reconciliation, not twice
        assert ctx.db.fetch.call_count == 1
