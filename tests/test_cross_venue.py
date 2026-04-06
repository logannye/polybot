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
    s.cv_cooldown_hours = 12.0
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
