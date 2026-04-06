"""Tests for PoliticalStrategy — calibration-based political market trading."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from polybot.strategies.political import PoliticalStrategy
from polybot.trading.risk import RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings():
    s = MagicMock()
    # Political strategy config
    s.pol_interval_seconds = 600
    s.pol_kelly_mult = 0.40
    s.pol_max_single_pct = 0.20
    s.pol_min_edge = 0.04
    s.pol_min_liquidity = 50_000
    s.pol_max_positions = 5
    # Shared config
    s.use_maker_orders = True
    s.min_trade_size = 1.0
    s.post_breaker_kelly_reduction = 0.50
    s.bankroll_survival_threshold = 50.0
    s.bankroll_growth_threshold = 500.0
    return s


def _make_political_market(
    polymarket_id="0xpol1",
    question="Will Trump win the 2026 midterms?",
    yes_price=0.70,
    book_depth=100_000,
    hours_left=72,
    tags=None,
):
    """Return a market dict representing a liquid political market."""
    from datetime import datetime, timezone, timedelta
    if tags is None:
        tags = ["politics", "trump-presidency"]
    return {
        "polymarket_id": polymarket_id,
        "question": question,
        "yes_price": yes_price,
        "book_depth": book_depth,
        "tags": tags,
        "category": "politics",
        "volume_24h": 200_000,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=hours_left),
        "yes_token_id": "yes_tok_1",
        "no_token_id": "no_tok_1",
    }


def _make_ctx(settings, markets, open_positions=0, fetchval_override=None):
    """Build a TradingContext mock wired for PoliticalStrategy tests."""

    async def default_fetchval(query, *args):
        if "strategy_performance" in query:
            return True           # enabled
        if "COUNT" in query and "strategy" in query:
            return open_positions  # position count
        if "INSERT INTO markets" in query:
            return 1              # market_id
        if "INSERT INTO analyses" in query:
            return 1              # analysis_id
        return None

    fv = fetchval_override if fetchval_override is not None else default_fetchval

    db = AsyncMock()
    db.fetchval = AsyncMock(side_effect=fv)
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 1000.0,
        "total_deployed": 50.0,
        "daily_pnl": 0.0,
        "post_breaker_until": None,
        "circuit_breaker_until": None,
    })
    db.fetch = AsyncMock(return_value=[])  # no existing positions by default

    scanner = AsyncMock()
    scanner.fetch_markets = AsyncMock(return_value=markets)

    executor = AsyncMock()
    executor.place_order = AsyncMock(return_value={"order_id": "test_order"})

    email_notifier = AsyncMock()

    ctx = MagicMock()
    ctx.db = db
    ctx.scanner = scanner
    ctx.executor = executor
    ctx.email_notifier = email_notifier
    ctx.settings = settings
    ctx.risk_manager = RiskManager()
    ctx.portfolio_lock = asyncio.Lock()
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filters_to_political_markets_only():
    """Sports markets should be ignored; only political markets should generate trades."""
    s = _make_settings()

    sports_market = _make_political_market(
        polymarket_id="0xsport1",
        question="Will the Lakers win tonight?",
        yes_price=0.60,
        tags=["sports", "basketball"],  # NOT political
    )
    political_market = _make_political_market(
        polymarket_id="0xpol1",
        question="Will the bill pass the Senate?",
        yes_price=0.70,
        tags=["politics"],
    )

    ctx = _make_ctx(s, markets=[sports_market, political_market])
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_called_once()
    call_kwargs = ctx.executor.place_order.call_args.kwargs
    assert call_kwargs["market_id"] == 1  # only the political market was processed


@pytest.mark.asyncio
async def test_skips_low_liquidity_markets():
    """Markets below pol_min_liquidity should be skipped."""
    s = _make_settings()
    s.pol_min_liquidity = 50_000

    thin_market = _make_political_market(
        polymarket_id="0xpol_thin",
        yes_price=0.70,
        book_depth=10_000,  # below threshold
        tags=["politics"],
    )

    ctx = _make_ctx(s, markets=[thin_market])
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_respects_position_cap():
    """No new trades should be placed when already at max_positions."""
    s = _make_settings()
    s.pol_max_positions = 5

    market = _make_political_market(yes_price=0.70, tags=["politics"])

    # Open positions = 5 (at cap)
    ctx = _make_ctx(s, markets=[market], open_positions=5)
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_skips_markets_resolving_within_24h():
    """Markets resolving within 24 hours should be left to the snipe strategy."""
    s = _make_settings()

    expiring_soon = _make_political_market(
        yes_price=0.70,
        hours_left=12,  # less than 24 hours
        tags=["politics"],
    )

    ctx = _make_ctx(s, markets=[expiring_soon])
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_skips_low_edge_markets():
    """A market at 55 cents (~2.5% calibration edge) should be skipped when min_edge=0.04."""
    s = _make_settings()
    s.pol_min_edge = 0.04  # 4% minimum

    # 55-cent political market: calibration-adjusted ~0.575, edge ~0.025 < 0.04
    near_fair_market = _make_political_market(
        yes_price=0.55,
        tags=["politics"],
    )

    ctx = _make_ctx(s, markets=[near_fair_market])
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_trades_high_edge_political_market():
    """A 70-cent political market should generate a trade (edge ~11% with slope=1.31)."""
    s = _make_settings()

    market = _make_political_market(
        yes_price=0.70,
        tags=["politics"],
    )

    ctx = _make_ctx(s, markets=[market])
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_skips_when_strategy_disabled():
    """Should not scan when strategy_performance.enabled is False."""
    s = _make_settings()

    market = _make_political_market(yes_price=0.70, tags=["politics"])

    async def disabled_fetchval(query, *args):
        if "strategy_performance" in query:
            return False  # disabled
        return None

    ctx = _make_ctx(s, markets=[market], fetchval_override=disabled_fetchval)
    strategy = PoliticalStrategy(settings=s)

    await strategy.run_once(ctx)

    ctx.scanner.fetch_markets.assert_not_called()
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_deduplicates_existing_open_positions():
    """Should not double-trade a market that already has an open position."""
    s = _make_settings()

    market = _make_political_market(
        polymarket_id="0xpol_dup",
        yes_price=0.70,
        tags=["politics"],
    )

    # Existing open position for the same market
    existing_row = MagicMock()
    existing_row.__getitem__ = lambda self, key: "0xpol_dup" if key == "polymarket_id" else None

    db = AsyncMock()

    async def mock_fetchval(query, *args):
        if "strategy_performance" in query:
            return True
        if "COUNT" in query and "strategy" in query:
            return 1  # 1 open position
        if "INSERT INTO markets" in query:
            return 1
        if "INSERT INTO analyses" in query:
            return 1
        return None

    db.fetchval = AsyncMock(side_effect=mock_fetchval)
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 1000.0,
        "total_deployed": 50.0,
        "daily_pnl": 0.0,
        "post_breaker_until": None,
        "circuit_breaker_until": None,
    })
    # Return existing position for this market
    db.fetch = AsyncMock(return_value=[existing_row])

    scanner = AsyncMock()
    scanner.fetch_markets = AsyncMock(return_value=[market])

    executor = AsyncMock()
    executor.place_order = AsyncMock(return_value={"order_id": "test_order"})

    ctx = MagicMock()
    ctx.db = db
    ctx.scanner = scanner
    ctx.executor = executor
    ctx.email_notifier = AsyncMock()
    ctx.settings = s
    ctx.risk_manager = RiskManager()
    ctx.portfolio_lock = asyncio.Lock()

    strategy = PoliticalStrategy(settings=s)
    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_init_reads_settings_with_defaults():
    """__init__ should read config from settings using getattr with defaults."""
    s = MagicMock(spec=[])  # empty spec — no attributes defined
    strategy = PoliticalStrategy(settings=s)

    assert strategy.name == "political"
    assert strategy.interval_seconds == 600
    assert strategy.kelly_multiplier == 0.40
    assert strategy.max_single_pct == 0.20
    assert strategy._min_edge == 0.04
    assert strategy._min_liquidity == 50_000
    assert strategy._max_positions == 5


@pytest.mark.asyncio
async def test_llm_confirmation_blocks_disagreeing_trade():
    """LLM quick_screen disagreeing with calibration direction should block the trade."""
    s = _make_settings()
    # yes_price=0.70 gives ~5.2% edge with slope 1.31; set confirm threshold below that
    s.pol_llm_confirm_edge = 0.05
    market = _make_political_market(yes_price=0.70, tags=["politics"])

    ensemble = AsyncMock()
    # Calibration says YES (true_prob ~0.752 > yes_price 0.70).
    # LLM returns 0.60 < 0.70 → disagrees with YES direction.
    ensemble.quick_screen = AsyncMock(return_value=0.60)

    ctx = _make_ctx(s, markets=[market])
    strategy = PoliticalStrategy(settings=s, ensemble=ensemble)

    await strategy.run_once(ctx)

    ensemble.quick_screen.assert_called_once()
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_llm_confirmation_allows_agreeing_trade():
    """LLM quick_screen agreeing with calibration direction should allow the trade."""
    s = _make_settings()
    # yes_price=0.70 gives ~5.2% edge with slope 1.31; set confirm threshold below that
    s.pol_llm_confirm_edge = 0.05
    market = _make_political_market(yes_price=0.70, tags=["politics"])

    ensemble = AsyncMock()
    # Calibration says YES (true_prob ~0.752 > yes_price 0.70).
    # LLM returns 0.80 > 0.70 → agrees with YES direction.
    ensemble.quick_screen = AsyncMock(return_value=0.80)

    ctx = _make_ctx(s, markets=[market])
    strategy = PoliticalStrategy(settings=s, ensemble=ensemble)

    await strategy.run_once(ctx)

    ensemble.quick_screen.assert_called_once()
    ctx.executor.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_no_llm_when_below_confirm_threshold():
    """Market with edge below pol_llm_confirm_edge should skip LLM and still trade."""
    s = _make_settings()
    # yes_price=0.70 gives ~5.2% edge; set confirm threshold above that to skip LLM
    s.pol_llm_confirm_edge = 0.06

    ensemble = AsyncMock()
    ensemble.quick_screen = AsyncMock(return_value=0.65)

    market = _make_political_market(yes_price=0.70, tags=["politics"])
    ctx = _make_ctx(s, markets=[market])
    strategy = PoliticalStrategy(settings=s, ensemble=ensemble)

    await strategy.run_once(ctx)

    # Edge (~5.2%) is below 6% confirm threshold → no LLM call, but trade proceeds
    ensemble.quick_screen.assert_not_called()
    ctx.executor.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_no_llm_when_ensemble_is_none():
    """PoliticalStrategy with ensemble=None should trade high-edge markets without LLM check."""
    s = _make_settings()
    # yes_price=0.70 gives ~11% edge, above threshold — but no ensemble
    market = _make_political_market(yes_price=0.70, tags=["politics"])

    ctx = _make_ctx(s, markets=[market])
    strategy = PoliticalStrategy(settings=s, ensemble=None)

    await strategy.run_once(ctx)

    # No ensemble means no LLM call, trade should proceed normally
    ctx.executor.place_order.assert_called_once()
