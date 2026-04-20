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
    settings.use_maker_orders = True
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
    settings.forecast_yes_max_entry = 1.0  # permissive — don't filter in this test
    settings.forecast_no_min_entry = 0.0   # permissive — don't filter in this test

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


from polybot.strategies.forecast import check_forecast_blacklist
from datetime import datetime, timezone, timedelta


def test_blacklist_blocks_after_two_losses():
    """Market with 2 stop-losses in 12h should be blacklisted."""
    now = datetime.now(timezone.utc)
    blacklist = {
        "mkt-bad": [now - timedelta(hours=2), now - timedelta(hours=1)],
    }
    assert check_forecast_blacklist("mkt-bad", blacklist) is True


def test_blacklist_allows_one_loss():
    """Market with only 1 stop-loss should not be blacklisted."""
    now = datetime.now(timezone.utc)
    blacklist = {
        "mkt-ok": [now - timedelta(hours=1)],
    }
    assert check_forecast_blacklist("mkt-ok", blacklist) is False


def test_blacklist_allows_unknown_market():
    """Market not in blacklist should be allowed."""
    assert check_forecast_blacklist("mkt-new", {}) is False


from polybot.strategies.forecast import _lookup_calibration_correction


def test_calibration_lookup_exact_bin():
    """Should find correction for the nearest bin."""
    corrections = {"0.1": -0.05, "0.3": 0.02, "0.5": -0.01, "0.7": 0.03, "0.9": -0.08}
    assert _lookup_calibration_correction(0.12, corrections) == pytest.approx(-0.05)


def test_calibration_lookup_midpoint():
    """Probability 0.45 should match bin 0.5 (nearest)."""
    corrections = {"0.1": -0.05, "0.3": 0.02, "0.5": -0.01, "0.7": 0.03, "0.9": -0.08}
    assert _lookup_calibration_correction(0.45, corrections) == pytest.approx(-0.01)


def test_calibration_lookup_clamps_large_correction():
    """Corrections should be clamped to [-0.10, +0.10]."""
    corrections = {"0.9": -0.95}  # Absurdly large correction
    result = _lookup_calibration_correction(0.88, corrections)
    assert result == pytest.approx(-0.10)


def test_calibration_lookup_empty():
    """Empty corrections should return 0.0."""
    assert _lookup_calibration_correction(0.5, {}) == 0.0


def test_calibration_lookup_none():
    """None corrections should return 0.0."""
    assert _lookup_calibration_correction(0.5, None) == 0.0


@pytest.mark.asyncio
async def test_forecast_yes_entry_filter_blocks_above_threshold():
    """YES trade with current_price > forecast_yes_max_entry should be filtered out."""
    from polybot.markets.filters import MarketCandidate
    from polybot.analysis.quant import QuantSignals
    from datetime import datetime, timezone, timedelta

    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    settings.use_maker_orders = True
    settings.max_positions_per_market = 1
    settings.min_trade_size = 1.0
    settings.forecast_yes_max_entry = 0.15
    settings.forecast_no_min_entry = 0.60
    settings.forecast_category_filter_enabled = False
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

    ctx = MagicMock()
    ctx.settings = settings
    ctx.portfolio_lock = asyncio.Lock()
    ctx.db = AsyncMock()
    ctx.executor = AsyncMock()

    # candidate.current_price=0.25 is above the 0.15 YES max entry threshold
    candidate = MarketCandidate(
        polymarket_id="test-yes-filter", question="Will X happen?", category="test",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=24),
        current_price=0.25, book_depth=1000.0, no_price=0.75)

    quant = QuantSignals(0, 0, 0, 0, 0)

    # ensemble_probability=0.45 => yes_edge=0.20, kelly returns side="YES" with edge=0.20
    ensemble_result = MagicMock()
    ensemble_result.ensemble_probability = 0.45
    ensemble_result.stdev = 0.05
    est = MagicMock()
    est.model = "test"
    est.probability = 0.45
    est.confidence = 0.8
    est.reasoning = "test"
    ensemble_result.estimates = [est]  # single estimate skips consensus check
    strategy._ensemble.analyze = AsyncMock(return_value=ensemble_result)
    strategy._ensemble.challenge_estimate = AsyncMock(return_value=None)
    strategy._researcher.search = AsyncMock(return_value="research text")

    ctx.risk_manager = MagicMock()
    ctx.risk_manager.confidence_multiplier.return_value = 1.0
    ctx.risk_manager.edge_skepticism_discount.return_value = 1.0
    ctx.risk_manager.check.return_value = MagicMock(allowed=True)

    from polybot.trading.risk import PortfolioState
    portfolio = PortfolioState(
        bankroll=300.0, total_deployed=0.0, daily_pnl=0.0,
        open_count=0, category_deployed={}, circuit_breaker_until=None)

    await strategy._full_analyze_and_trade(
        candidate=candidate, quant=quant, trust_weights={},
        bankroll=300.0, kelly_mult=0.25, edge_threshold=0.05,
        portfolio=portfolio, calibration_corrections={}, ctx=ctx)

    # The YES entry filter should have blocked the trade before place_order
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_forecast_yes_entry_filter_passes_below_threshold():
    """YES trade with current_price < forecast_yes_max_entry should NOT be filtered."""
    from polybot.markets.filters import MarketCandidate
    from polybot.analysis.quant import QuantSignals
    from datetime import datetime, timezone, timedelta

    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    settings.use_maker_orders = True
    settings.max_positions_per_market = 1
    settings.min_trade_size = 1.0
    settings.forecast_yes_max_entry = 0.15
    settings.forecast_no_min_entry = 0.60
    settings.forecast_category_filter_enabled = False
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

    ctx = MagicMock()
    ctx.settings = settings
    ctx.portfolio_lock = asyncio.Lock()

    # candidate.current_price=0.10 is BELOW the 0.15 YES max entry threshold
    candidate = MarketCandidate(
        polymarket_id="test-yes-passes", question="Will Y happen?", category="test",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=24),
        current_price=0.10, book_depth=1000.0, no_price=0.90)

    quant = QuantSignals(0, 0, 0, 0, 0)

    # ensemble_probability=0.35 => yes_edge=0.25, kelly returns side="YES" with edge=0.25
    ensemble_result = MagicMock()
    ensemble_result.ensemble_probability = 0.35
    ensemble_result.stdev = 0.05
    est = MagicMock()
    est.model = "test"
    est.probability = 0.35
    est.confidence = 0.8
    est.reasoning = "test"
    ensemble_result.estimates = [est]  # single estimate skips consensus check
    strategy._ensemble.analyze = AsyncMock(return_value=ensemble_result)
    strategy._ensemble.challenge_estimate = AsyncMock(return_value=None)
    strategy._researcher.search = AsyncMock(return_value="research text")

    ctx.risk_manager = MagicMock()
    ctx.risk_manager.confidence_multiplier.return_value = 1.0
    ctx.risk_manager.edge_skepticism_discount.return_value = 1.0
    ctx.risk_manager.check.return_value = MagicMock(allowed=True)
    ctx.executor = AsyncMock()

    # DB mock: market upsert returns id=1, analyses returns 10, dedup check returns 0 (no existing)
    async def mock_fetchval(sql, *args):
        if "INSERT INTO markets" in sql:
            return 1
        if "INSERT INTO analyses" in sql:
            return 10
        if "SELECT COUNT(*) FROM trades" in sql:
            return 0  # No existing position — allow the trade
        return None

    async def mock_fetchrow(sql, *args):
        if "system_state" in sql:
            return {
                "bankroll": 300.0, "total_deployed": 0.0, "daily_pnl": 0.0,
                "circuit_breaker_until": None,
            }
        return None

    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=mock_fetchval)
    ctx.db.fetchrow = AsyncMock(side_effect=mock_fetchrow)

    ctx.email_notifier = AsyncMock()

    from polybot.trading.risk import PortfolioState
    portfolio = PortfolioState(
        bankroll=300.0, total_deployed=0.0, daily_pnl=0.0,
        open_count=0, category_deployed={}, circuit_breaker_until=None)

    await strategy._full_analyze_and_trade(
        candidate=candidate, quant=quant, trust_weights={},
        bankroll=300.0, kelly_mult=0.25, edge_threshold=0.05,
        portfolio=portfolio, calibration_corrections={}, ctx=ctx)

    # The filter should NOT block this trade — place_order should be called
    ctx.executor.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_forecast_edge_threshold_0_04_admits_moderate_disagreement():
    """With edge_threshold=0.04, an ensemble that disagrees by 10 cents raw
    should pass after 45% shrinkage (edge = 0.10 * 0.55 = 0.055 > 0.04)."""
    from polybot.analysis.ensemble import shrink_toward_market
    raw_prob = 0.60  # ensemble says 60%
    market_price = 0.50  # market says 50%
    shrunk = shrink_toward_market(raw_prob, market_price, shrinkage=0.45)
    # shrunk = 0.60 * 0.55 + 0.50 * 0.45 = 0.33 + 0.225 = 0.555
    edge = shrunk - market_price  # 0.555 - 0.50 = 0.055
    assert edge > 0.04, f"Edge {edge} should exceed 0.04 threshold"
    assert edge < 0.07, f"Edge {edge} should be below old 0.07 threshold (proving old config blocked this)"


@pytest.mark.asyncio
async def test_compute_quant_returns_none_on_wide_spread():
    """_compute_quant should return None when the order book spread exceeds
    forecast_max_spread so the strategy skips the candidate before running
    the ensemble. Without this gate, forecast spammed illiquid markets with
    signals that the executor then rejected at place_order time."""
    from polybot.markets.filters import MarketCandidate
    from datetime import datetime, timezone, timedelta

    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    settings.forecast_max_spread = 0.15

    strategy = EnsembleForecastStrategy(
        settings=settings, ensemble=MagicMock(), researcher=MagicMock())

    candidate = MarketCandidate(
        polymarket_id="illiquid", question="?", category="test",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=24),
        current_price=0.50, book_depth=1000.0, no_price=0.50,
        yes_token_id="yes-token", no_token_id="no-token")

    ctx = MagicMock()
    ctx.scanner = AsyncMock()
    ctx.scanner.fetch_price_history = AsyncMock(return_value=[0.50])
    # Best bid 0.01, best ask 0.99 — spread 0.98, like the live production data
    ctx.scanner.fetch_order_book = AsyncMock(return_value={
        "bids": [{"price": "0.01", "size": "10"}],
        "asks": [{"price": "0.99", "size": "10"}],
    })

    result = await strategy._compute_quant(candidate, ctx)
    assert result is None
    # Scanner should have been called with the YES token, not the market id
    ctx.scanner.fetch_order_book.assert_called_with("yes-token")


@pytest.mark.asyncio
async def test_compute_quant_returns_none_on_empty_book():
    """Empty order book (no liquidity) should skip the candidate."""
    from polybot.markets.filters import MarketCandidate
    from datetime import datetime, timezone, timedelta

    settings = MagicMock()
    settings.forecast_max_spread = 0.15

    strategy = EnsembleForecastStrategy(
        settings=settings, ensemble=MagicMock(), researcher=MagicMock())

    candidate = MarketCandidate(
        polymarket_id="empty", question="?", category="test",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=24),
        current_price=0.50, book_depth=1000.0, no_price=0.50,
        yes_token_id="yes-token", no_token_id="no-token")

    ctx = MagicMock()
    ctx.scanner = AsyncMock()
    ctx.scanner.fetch_price_history = AsyncMock(return_value=[0.50])
    ctx.scanner.fetch_order_book = AsyncMock(return_value={"bids": [], "asks": []})

    result = await strategy._compute_quant(candidate, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_compute_quant_allows_tight_spread():
    """Tight-spread markets should still produce QuantSignals."""
    from polybot.markets.filters import MarketCandidate
    from polybot.analysis.quant import QuantSignals
    from datetime import datetime, timezone, timedelta

    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    settings.forecast_max_spread = 0.15

    strategy = EnsembleForecastStrategy(
        settings=settings, ensemble=MagicMock(), researcher=MagicMock())

    candidate = MarketCandidate(
        polymarket_id="liquid", question="?", category="test",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=24),
        current_price=0.50, book_depth=1000.0, no_price=0.50,
        yes_token_id="yes-token", no_token_id="no-token")

    ctx = MagicMock()
    ctx.scanner = AsyncMock()
    ctx.scanner.fetch_price_history = AsyncMock(return_value=[0.50])
    ctx.scanner.fetch_order_book = AsyncMock(return_value={
        "bids": [{"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.52", "size": "100"}],
    })

    result = await strategy._compute_quant(candidate, ctx)
    assert isinstance(result, QuantSignals)
