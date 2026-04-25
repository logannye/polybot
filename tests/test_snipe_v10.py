"""Tests for v10 Snipe strategy — 2-tier resolution-convergence."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from polybot.strategies.snipe import (
    classify_snipe, compute_net_edge, SnipeCandidate, ResolutionSnipeStrategy,
)
from polybot.analysis.gemini_client import GeminiResult


# ---- classify_snipe -------------------------------------------------------

def test_classify_t0_yes_side():
    c = classify_snipe(yes_price=0.97, hours_remaining=6.0)
    assert c is not None
    assert c.tier == 0
    assert c.side == "YES"
    assert c.buy_price == pytest.approx(0.97)


def test_classify_t0_no_side_mirror():
    """YES price 0.03 → NO price 0.97 → T0 NO side."""
    c = classify_snipe(yes_price=0.03, hours_remaining=6.0)
    assert c is not None
    assert c.tier == 0
    assert c.side == "NO"
    assert c.buy_price == pytest.approx(0.97)


def test_classify_t1_yes():
    c = classify_snipe(yes_price=0.90, hours_remaining=4.0)
    assert c is not None
    assert c.tier == 1
    assert c.side == "YES"


def test_classify_t1_no_mirror():
    c = classify_snipe(yes_price=0.11, hours_remaining=4.0)
    assert c is not None
    assert c.tier == 1
    assert c.side == "NO"
    assert c.buy_price == pytest.approx(0.89)


def test_classify_rejects_below_0_88():
    """Price 0.85 is below T1 floor."""
    assert classify_snipe(yes_price=0.85, hours_remaining=4.0) is None


def test_classify_rejects_t1_outside_window():
    """T1-range price but outside 8h window → reject."""
    assert classify_snipe(yes_price=0.90, hours_remaining=10.0) is None


def test_classify_rejects_t0_outside_window():
    """T0 price but outside 12h window → reject."""
    assert classify_snipe(yes_price=0.97, hours_remaining=24.0) is None


def test_classify_rejects_negative_hours():
    assert classify_snipe(yes_price=0.97, hours_remaining=-1.0) is None


def test_t0_threshold_exactly_0_96():
    """Boundary: exactly 0.96 → T0."""
    c = classify_snipe(yes_price=0.96, hours_remaining=12.0)
    assert c is not None
    assert c.tier == 0


def test_t1_threshold_exactly_0_88():
    """Boundary: exactly 0.88 → T1."""
    c = classify_snipe(yes_price=0.88, hours_remaining=8.0)
    assert c is not None
    assert c.tier == 1


# ---- compute_net_edge -----------------------------------------------------

def test_net_edge_pure_maker():
    assert compute_net_edge(buy_price=0.95, fee_per_dollar=0.0) == pytest.approx(0.05)


def test_net_edge_with_fee():
    edge = compute_net_edge(buy_price=0.95, fee_per_dollar=0.02)
    assert edge == pytest.approx(0.03)


# ---- ResolutionSnipeStrategy end-to-end ----------------------------------

def _base_settings():
    s = MagicMock()
    s.snipe_enabled = True
    s.snipe_interval_seconds = 120
    s.snipe_t0_kelly_mult = 0.50
    s.snipe_t1_kelly_mult = 0.30
    s.snipe_t0_max_single_pct = 0.10
    s.snipe_t1_max_single_pct = 0.07
    s.snipe_min_book_depth = 2000.0
    s.snipe_min_book_depth_dryrun = 500.0
    s.snipe_max_concurrent = 3
    s.snipe_t1_min_confidence = 0.85
    s.snipe_min_net_edge = 0.02
    s.snipe_t0_max_hours = 12.0
    s.snipe_t1_max_hours = 8.0
    s.snipe_t0_max_hours_dryrun = 168.0
    s.snipe_t1_max_hours_dryrun = 168.0
    s.dry_run = False                   # tests assume live unless explicitly set
    s.min_trade_size = 1.0
    return s


def _ctx(markets, open_count=0):
    ctx = MagicMock()
    ctx.db = AsyncMock()
    fetchval_responses = {"count": open_count}
    async def fetchval_side_effect(sql, *args):
        sql_lower = sql.lower()
        if "count(*)" in sql_lower and "strategy = 'snipe'" in sql_lower:
            return fetchval_responses["count"]
        if "count(*)" in sql_lower:
            return 0   # dedup check
        if "insert into markets" in sql_lower:
            return 42
        if "bankroll" in sql_lower:
            return None
        return None
    ctx.db.fetchval = AsyncMock(side_effect=fetchval_side_effect)
    ctx.db.fetchrow = AsyncMock(return_value={"bankroll": 2000.0})
    ctx.scanner = AsyncMock()
    ctx.scanner.fetch_markets = AsyncMock(return_value=markets)
    ctx.portfolio_lock = asyncio.Lock()
    ctx.executor = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_snipe_t0_enters_without_llm():
    """T0 does not require Gemini — should enter directly."""
    markets = [{
        "polymarket_id": "0x" + "a" * 40,
        "question": "Will X happen?",
        "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=6),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=_base_settings(), gemini_client=None)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_called_once()
    call_kwargs = ctx.executor.place_order.call_args.kwargs
    assert call_kwargs["side"] == "YES"
    assert call_kwargs["strategy"] == "snipe"
    assert call_kwargs["kelly_inputs"]["tier"] == 0


@pytest.mark.asyncio
async def test_snipe_t1_requires_gemini_and_enters_when_verified():
    markets = [{
        "polymarket_id": "0x" + "b" * 40,
        "question": "Will Y happen?",
        "yes_price": 0.91, "no_price": 0.09,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=4),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    gemini = MagicMock()
    gemini.can_spend = MagicMock(return_value=True)
    gemini.current_spend = MagicMock(return_value=0.0)
    gemini.verify_snipe = AsyncMock(return_value=GeminiResult(
        verdict="YES_LOCKED", confidence=0.92))
    strategy = ResolutionSnipeStrategy(settings=_base_settings(), gemini_client=gemini)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    gemini.verify_snipe.assert_awaited_once()
    ctx.executor.place_order.assert_called_once()
    assert ctx.executor.place_order.call_args.kwargs["kelly_inputs"]["tier"] == 1


@pytest.mark.asyncio
async def test_snipe_t1_rejects_when_gemini_uncertain():
    markets = [{
        "polymarket_id": "0x" + "c" * 40,
        "question": "Will Z happen?",
        "yes_price": 0.91, "no_price": 0.09,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=4),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    gemini = MagicMock()
    gemini.can_spend = MagicMock(return_value=True)
    gemini.verify_snipe = AsyncMock(return_value=GeminiResult(
        verdict="UNCERTAIN", confidence=0.4))
    strategy = ResolutionSnipeStrategy(settings=_base_settings(), gemini_client=gemini)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_snipe_t1_rejects_when_gemini_cap_hit():
    markets = [{
        "polymarket_id": "0x" + "d" * 40,
        "question": "Will W happen?",
        "yes_price": 0.91, "no_price": 0.09,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=4),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    gemini = MagicMock()
    gemini.can_spend = MagicMock(return_value=False)
    gemini.current_spend = MagicMock(return_value=2.5)
    gemini.verify_snipe = AsyncMock()
    strategy = ResolutionSnipeStrategy(settings=_base_settings(), gemini_client=gemini)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    gemini.verify_snipe.assert_not_called()
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_snipe_respects_max_concurrent():
    markets = [{
        "polymarket_id": "0x" + "e" * 40,
        "question": "?", "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=3),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=_base_settings())
    ctx = _ctx(markets, open_count=3)   # at limit
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_snipe_skips_insufficient_book_depth():
    markets = [{
        "polymarket_id": "0x" + "f" * 40,
        "question": "?", "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 100.0,   # below floor
        "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=3),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=_base_settings())
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_snipe_disabled_skips():
    s = _base_settings()
    s.snipe_enabled = False
    markets = [{
        "polymarket_id": "0x1", "yes_price": 0.97,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=3),
    }]
    strategy = ResolutionSnipeStrategy(settings=s)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


# ---- v11 dry-run window relaxation ----------------------------------------

@pytest.mark.asyncio
async def test_snipe_dryrun_relaxes_t0_time_window():
    """Dry-run lifts the 12h ceiling to 168h so observation captures the
    long-tail markets that dominate current Polymarket structure."""
    s = _base_settings()
    s.dry_run = True   # <-- toggles relaxation
    markets = [{
        "polymarket_id": "0x" + "a" * 40,
        "question": "?", "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=72),  # 3d
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=s)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    # Live would skip (h=72 > 12), dry-run enters
    ctx.executor.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_snipe_live_still_enforces_12h_ceiling():
    """Live mode keeps the conservative spec ceiling — 72h market rejected."""
    s = _base_settings()
    s.dry_run = False
    markets = [{
        "polymarket_id": "0x" + "b" * 40,
        "question": "?", "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 5000.0, "volume_24h": 10000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=72),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=s)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_snipe_dryrun_relaxes_book_depth_floor():
    """Dry-run accepts \\$500 minimum book depth so observation isn't
    blocked on books below the live \\$2K floor."""
    s = _base_settings()
    s.dry_run = True
    markets = [{
        "polymarket_id": "0x" + "c" * 40,
        "question": "?", "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 800.0,  # below live $2K, above dry-run $500
        "volume_24h": 5000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=4),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=s)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_snipe_dryrun_book_depth_floor_still_applies():
    """\\$500 floor is hard — \\$200 ghost-book market still rejected in dry-run."""
    s = _base_settings()
    s.dry_run = True
    markets = [{
        "polymarket_id": "0x" + "d" * 40,
        "question": "?", "yes_price": 0.97, "no_price": 0.03,
        "book_depth": 200.0,   # below dry-run floor
        "volume_24h": 5000.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=4),
        "category": "politics",
        "yes_token_id": "tok-yes", "no_token_id": "tok-no",
    }]
    strategy = ResolutionSnipeStrategy(settings=s)
    ctx = _ctx(markets)
    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
