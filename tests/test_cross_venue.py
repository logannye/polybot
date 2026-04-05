import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.strategies.cross_venue import CrossVenueStrategy


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
