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


class TestSimulateFills:
    def _make_strategy(self, spread_bps=150):
        settings = _make_settings()
        settings.mm_base_spread_bps = spread_bps
        clob = AsyncMock()
        scanner = MagicMock()
        inv = InventoryTracker(max_per_market=50.0, max_total=200.0, max_skew_bps=100)
        qm = MagicMock()
        strategy = MarketMakerStrategy(
            settings=settings, clob=clob, scanner=scanner,
            dry_run=True, inventory=inv, quote_manager=qm)
        return strategy

    def _make_quote(self, side, price, size=20.0):
        from polybot.trading.quote_manager import Quote
        return Quote(
            order_id=f"dry_{side}", token_id="t1", side=side,
            price=price, size=size,
            posted_at=datetime.now(timezone.utc), status="live")

    def test_simulate_fills_detects_bid_cross(self):
        """Price drops below bid from previous cycle -> bid fill triggered."""
        strategy = self._make_strategy()
        market = ActiveMarket(
            polymarket_id="m1", yes_token_id="t1", no_token_id="t2",
            category="politics", max_incentive_spread=0.05,
            min_incentive_size=10.0, fair_value=0.50)
        strategy._active_markets = {"m1": market}

        # Previous cycle quoted at 0.50, bid at 0.4925
        strategy._prev_prices["m1"] = 0.50
        bid = self._make_quote("BUY", 0.4925, size=20.0)
        ask = self._make_quote("SELL", 0.5075, size=20.0)
        strategy._quote_mgr.get_quotes = MagicMock(return_value=(bid, ask))

        # Price dropped to 0.48 -> below bid
        strategy._scanner.get_cached_price = MagicMock(
            return_value={"yes_price": 0.48})

        strategy._simulate_fills()

        assert strategy._sim_fills == 1
        assert strategy._sim_pnl > 0  # spread_earned = 0.50 - 0.4925 = 0.0075

    def test_simulate_fills_detects_ask_cross(self):
        """Price rises above ask from previous cycle -> ask fill triggered."""
        strategy = self._make_strategy()
        market = ActiveMarket(
            polymarket_id="m1", yes_token_id="t1", no_token_id="t2",
            category="politics", max_incentive_spread=0.05,
            min_incentive_size=10.0, fair_value=0.50)
        strategy._active_markets = {"m1": market}

        strategy._prev_prices["m1"] = 0.50
        bid = self._make_quote("BUY", 0.4925, size=20.0)
        ask = self._make_quote("SELL", 0.5075, size=20.0)
        strategy._quote_mgr.get_quotes = MagicMock(return_value=(bid, ask))

        # Price rose to 0.52 -> above ask
        strategy._scanner.get_cached_price = MagicMock(
            return_value={"yes_price": 0.52})

        strategy._simulate_fills()

        assert strategy._sim_fills == 1
        assert strategy._sim_pnl > 0  # spread_earned = 0.5075 - 0.50 = 0.0075

    def test_simulate_fills_no_fill_when_price_unchanged(self):
        """Same price as quote cycle -> no fills triggered."""
        strategy = self._make_strategy()
        market = ActiveMarket(
            polymarket_id="m1", yes_token_id="t1", no_token_id="t2",
            category="politics", max_incentive_spread=0.05,
            min_incentive_size=10.0, fair_value=0.50)
        strategy._active_markets = {"m1": market}

        strategy._prev_prices["m1"] = 0.50
        bid = self._make_quote("BUY", 0.4925, size=20.0)
        ask = self._make_quote("SELL", 0.5075, size=20.0)
        strategy._quote_mgr.get_quotes = MagicMock(return_value=(bid, ask))

        # Price unchanged at 0.50 -> between bid and ask
        strategy._scanner.get_cached_price = MagicMock(
            return_value={"yes_price": 0.50})

        strategy._simulate_fills()

        assert strategy._sim_fills == 0
        assert strategy._sim_pnl == 0.0

    def test_simulate_fills_skips_without_prev_price(self):
        """First cycle for a market (no prev_price) -> graceful skip."""
        strategy = self._make_strategy()
        market = ActiveMarket(
            polymarket_id="m1", yes_token_id="t1", no_token_id="t2",
            category="politics", max_incentive_spread=0.05,
            min_incentive_size=10.0, fair_value=0.50)
        strategy._active_markets = {"m1": market}

        # No entry in _prev_prices
        bid = self._make_quote("BUY", 0.49, size=20.0)
        ask = self._make_quote("SELL", 0.51, size=20.0)
        strategy._quote_mgr.get_quotes = MagicMock(return_value=(bid, ask))
        strategy._scanner.get_cached_price = MagicMock(
            return_value={"yes_price": 0.40})  # would cross bid

        strategy._simulate_fills()

        assert strategy._sim_fills == 0  # skipped because no prev_price


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
