import pytest
from unittest.mock import MagicMock
from polybot.strategies.forecast import EnsembleForecastStrategy


def test_forecast_strategy_attrs():
    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    s = EnsembleForecastStrategy(settings=settings, ensemble=MagicMock(), researcher=MagicMock())
    assert s.name == "forecast"
    assert s.interval_seconds == 300
    assert s.kelly_multiplier == 0.25
    assert s.max_single_pct == 0.15


import asyncio
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_forecast_dedup_blocks_existing_trade():
    """If a market already has a dry_run/open trade, the forecast should skip it."""
    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    settings.polymarket_fee_rate = 0.02
    settings.max_positions_per_market = 1
    settings.min_trade_size = 1.0
    settings.quant_weights = {
        "line_movement": 0.30, "volume_spike": 0.25,
        "book_imbalance": 0.20, "spread": 0.15, "time_decay": 0.10,
    }
    settings.ensemble_stdev_low = 0.05
    settings.ensemble_stdev_high = 0.12
    settings.confidence_mult_low = 1.0
    settings.confidence_mult_mid = 0.7
    settings.confidence_mult_high = 0.4
    settings.quant_negative_mult = 0.75
    settings.post_breaker_kelly_reduction = 0.50
    settings.bankroll_survival_threshold = 50.0
    settings.bankroll_growth_threshold = 500.0

    strategy = EnsembleForecastStrategy(
        settings=settings, ensemble=MagicMock(), researcher=MagicMock())

    # Mock context
    ctx = MagicMock()
    ctx.settings = settings
    ctx.portfolio_lock = asyncio.Lock()

    # DB mock: market upsert returns id=1, dedup check returns existing=1
    async def mock_fetchval(sql, *args):
        if "INSERT INTO markets" in sql:
            return 1
        if "INSERT INTO analyses" in sql:
            return 10
        if "SELECT COUNT(*) FROM trades" in sql:
            return 1  # Already has a position
        return None

    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=mock_fetchval)

    # Build minimal candidate/quant/ensemble mocks
    from polybot.markets.filters import MarketCandidate
    from polybot.analysis.quant import QuantSignals
    from datetime import datetime, timezone, timedelta

    candidate = MarketCandidate(
        polymarket_id="test-market", question="Test?", category="test",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=24),
        current_price=0.50, book_depth=1000.0, no_price=0.50)

    quant = QuantSignals(0, 0, 0, 0, 0)

    # Provide a mock ensemble result with valid estimates
    ensemble_result = MagicMock()
    ensemble_result.ensemble_probability = 0.65
    ensemble_result.stdev = 0.05
    est = MagicMock()
    est.model = "test"
    est.probability = 0.65
    est.confidence = 0.8
    est.reasoning = "test"
    ensemble_result.estimates = [est]
    strategy._ensemble.analyze = AsyncMock(return_value=ensemble_result)
    strategy._ensemble.challenge_estimate = AsyncMock(return_value=None)
    strategy._researcher.search = AsyncMock(return_value="")

    ctx.risk_manager = MagicMock()
    ctx.risk_manager.confidence_multiplier.return_value = 1.0
    ctx.risk_manager.edge_skepticism_discount.return_value = 1.0
    ctx.risk_manager.check.return_value = MagicMock(allowed=True)
    ctx.executor = AsyncMock()

    from polybot.trading.risk import PortfolioState
    portfolio = PortfolioState(
        bankroll=300.0, total_deployed=0.0, daily_pnl=0.0,
        open_count=0, category_deployed={}, circuit_breaker_until=None)

    await strategy._full_analyze_and_trade(
        candidate=candidate, quant=quant, trust_weights={},
        bankroll=300.0, kelly_mult=0.25, edge_threshold=0.05,
        portfolio=portfolio, calibration_corrections={}, ctx=ctx)

    # place_order should NOT have been called because dedup blocked it
    ctx.executor.place_order.assert_not_called()
