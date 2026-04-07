import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.strategies.cross_venue import CrossVenueStrategy
from polybot.trading.risk import RiskManager


def _make_settings():
    s = MagicMock()
    s.cv_interval_seconds = 300.0
    s.cv_kelly_mult = 0.25
    s.cv_max_single_pct = 0.15
    s.cv_min_divergence = 0.03
    s.cv_min_implied_prob = 0.10
    s.cv_cooldown_hours = 12.0
    s.cv_max_days_to_resolution = 7.0
    s.use_maker_orders = True
    s.min_trade_size = 1.0
    s.post_breaker_kelly_reduction = 0.5
    s.bankroll_survival_threshold = 50.0
    s.bankroll_growth_threshold = 500.0
    s.conviction_stack_enabled = False
    s.conviction_stack_per_signal = 0.5
    s.conviction_stack_max = 3.0
    return s


class TestCrossVenueInit:
    def test_reads_settings(self):
        s = _make_settings()
        odds_client = MagicMock()
        strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)
        assert strategy.name == "cross_venue"
        assert strategy.interval_seconds == 300.0
        assert strategy._min_divergence == 0.03


@pytest.mark.asyncio
async def test_run_once_skips_when_no_divergences():
    """Should not place trades when odds client returns no events."""
    s = _make_settings()
    odds_client = MagicMock()
    odds_client.fetch_all_sports = AsyncMock(return_value=[])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {}

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_skips_when_disabled():
    """Should not scan when strategy is disabled."""
    s = _make_settings()
    odds_client = MagicMock()
    odds_client.fetch_all_sports = AsyncMock()

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=False)  # disabled

    await strategy.run_once(ctx)
    odds_client.fetch_all_sports.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_respects_max_concurrent_positions():
    """Should reject trades when open_count >= max_concurrent."""
    s = _make_settings()
    odds_client = MagicMock()
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt1", "sport_key": "basketball_nba",
         "home_team": "Lakers", "away_team": "Celtics",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": -200},
                 {"name": "Boston Celtics", "price": +170}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": -110},
                 {"name": "Boston Celtics", "price": -110}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=[
        True,    # enabled check
        1,       # market upsert (we may not reach this)
    ])
    ctx.db.fetchrow = AsyncMock(return_value={
        "bankroll": 500.0, "total_deployed": 0.0, "daily_pnl": 0.0,
        "post_breaker_until": None, "circuit_breaker_until": None,
    })
    # Return 12 open trades (max_concurrent default)
    ctx.db.fetch = AsyncMock(return_value=[{"position_size_usd": 10, "category": "sports"}] * 12)
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xabc", "question": "Will the Los Angeles Lakers win?",
               "yes_price": 0.45, "category": "sports", "book_depth": 5000,
               "resolution_time": "2026-04-10T00:00:00Z", "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.risk_manager = RiskManager()
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_run_once_skips_long_dated_market():
    """Should skip markets resolving more than 7 days out."""
    s = _make_settings()
    s.cv_max_days_to_resolution = 7.0
    odds_client = MagicMock()
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt1", "sport_key": "basketball_nba",
         "home_team": "Lakers", "away_team": "Celtics",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": -200},
                 {"name": "Boston Celtics", "price": +170}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": -110},
                 {"name": "Boston Celtics", "price": -110}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)  # enabled
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    # Market resolves 90 days from now — should be skipped
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xabc", "question": "Will the Los Angeles Lakers win?",
               "yes_price": 0.45, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=90),
               "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_skips_penny_odds():
    """Should skip divergences where buy_price < cv_min_implied_prob (default 0.10)."""
    s = _make_settings()
    s.cv_min_implied_prob = 0.10  # 10% floor
    odds_client = MagicMock()
    # Consensus says 5%, Polymarket says 1.5% — 3.5% divergence but penny odds
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt1", "sport_key": "basketball_nba",
         "home_team": "Lakers", "away_team": "Celtics",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": 1900},
                 {"name": "Boston Celtics", "price": -5000}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": 5566},
                 {"name": "Boston Celtics", "price": -10000}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)  # enabled
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xabc", "question": "Will the Los Angeles Lakers win?",
               "yes_price": 0.015, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=3),
               "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_skips_penny_odds_no_side():
    """Should skip NO-side divergences where 1 - yes_price < cv_min_implied_prob."""
    s = _make_settings()
    s.cv_min_implied_prob = 0.10
    odds_client = MagicMock()
    # FanDuel: Team A -2000 (~95%), Team B +1200 (~8%) — after devig Team A ~92%, Team B ~8%
    # Polymarket: Team A -10000 (~99%), Team B +5566 (~1.77%)
    # For Team A: consensus(~92%) < poly(~99%) → divergence < 0 → side = "NO"
    # buy_price = 1 - yes_price = 1 - 0.99 = 0.01, which is < cv_min_implied_prob (0.10) → skip
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt1", "sport_key": "basketball_nba",
         "home_team": "Team A", "away_team": "Team B",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Team A", "price": -2000},
                 {"name": "Team B", "price": 1200}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Team A", "price": -10000},
                 {"name": "Team B", "price": 5566}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        # yes_price=0.99 means NO buy_price = 1 - 0.99 = 0.01 (penny odds)
        "m1": {"polymarket_id": "0xabc", "question": "Will Team A win?",
               "yes_price": 0.99, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=3),
               "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_trades_mid_range_divergence():
    """Should place a trade when divergence is real and buy_price >= cv_min_implied_prob."""
    s = _make_settings()
    s.cv_min_implied_prob = 0.10
    odds_client = MagicMock()
    # FanDuel ~54% Nuggets, Polymarket ~47% Nuggets — ~7% divergence, mid-range price
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt2", "sport_key": "basketball_nba",
         "home_team": "Denver Nuggets", "away_team": "Phoenix Suns",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Denver Nuggets", "price": -130},
                 {"name": "Phoenix Suns", "price": 110}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Denver Nuggets", "price": 110},
                 {"name": "Phoenix Suns", "price": -130}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=[
        True,   # enabled check
        1,      # market upsert RETURNING id
        1,      # analysis insert RETURNING id
    ])
    ctx.db.fetchrow = AsyncMock(return_value={
        "bankroll": 500.0, "total_deployed": 50.0, "daily_pnl": 0.0,
        "post_breaker_until": None, "circuit_breaker_until": None,
    })
    ctx.db.fetch = AsyncMock(return_value=[
        {"position_size_usd": 10, "category": "sports"},
    ])
    ctx.executor = AsyncMock()
    ctx.executor.place_order = AsyncMock(return_value={"order_id": "test123"})
    ctx.settings = s
    ctx.risk_manager = RiskManager()
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xdef", "question": "Will the Denver Nuggets win?",
               "yes_price": 0.45, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=2),
               "volume_24h": 50000,
               "yes_token_id": "tok3", "no_token_id": "tok4"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_called_once()
