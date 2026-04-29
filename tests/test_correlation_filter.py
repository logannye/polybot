"""v12.4 correlation filter — markets sharing a `group_slug` are bracket
markets on the same underlying news event. Opening multiple positions on
them creates phantom diversification, so the filter blocks the second
entry within the same group.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from polybot.strategies.snipe import ResolutionSnipeStrategy
from polybot.strategies.base import TradingContext


def _settings(correlation_enabled=True):
    return SimpleNamespace(
        snipe_enabled=True,
        snipe_interval_seconds=60,
        snipe_kelly_mult=0.25,
        snipe_max_single_pct=0.04,
        snipe_max_concurrent=10,
        snipe_min_verifier_confidence=0.95,
        snipe_min_book_depth_dryrun=500.0,
        snipe_max_hours_dryrun=72.0,
        snipe_max_hours=12.0,
        snipe_min_book_depth=1000.0,
        snipe_min_price=0.92,
        dry_run=True,
        snipe_max_total_deployed_pct=0.30,
        snipe_correlation_filter_enabled=correlation_enabled,
        snipe_tier_high_min_conf=0.99, snipe_tier_high_min_edge=0.02,
        snipe_tier_high_max_pct=0.01,
        snipe_tier_mid_min_conf=0.97, snipe_tier_mid_min_edge=0.04,
        snipe_tier_mid_max_pct=0.02,
        snipe_tier_low_min_conf=0.95, snipe_tier_low_min_edge=0.06,
        snipe_tier_low_max_pct=0.04,
        min_trade_size=1.0,
    )


def _market(polymarket_id, group_slug, *, yes_price=0.07,
            resolution_hours=24.0):
    """yes_price defaults to 0.07 so classify_snipe mirrors to side='NO'
    with buy_price=0.93. The mocked verifier returns NO_LOCKED so the
    candidate matches and reaches the entry path."""
    return {
        "polymarket_id": polymarket_id,
        "question": f"Test question for {polymarket_id}",
        "category": "politics",
        "yes_price": yes_price,
        "no_price": 1 - yes_price,
        "yes_token_id": f"y_{polymarket_id}",
        "no_token_id": f"n_{polymarket_id}",
        "volume_24h": 5000.0,
        "book_depth": 1500.0,
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=resolution_hours),
        "group_slug": group_slug,
    }


def _make_ctx(scanner_markets, held_polymarket_ids, scanner_cache):
    """Wire a TradingContext + db that dispatches by SQL string so each
    call site (killswitch/concurrency/deployed/correlation/Kelly/insert)
    gets the row shape it expects."""
    db = AsyncMock()

    # ── fetchrow dispatcher ────────────────────────────────────────
    async def _fetchrow(sql, *args):
        if "killswitch_tripped_at" in sql:
            return {"killswitch_tripped_at": None}
        # bankroll + total_deployed gate, also bankroll-only in _enter
        return {"bankroll": 2000.0, "total_deployed": 0.0}
    db.fetchrow = AsyncMock(side_effect=_fetchrow)

    # ── fetchval dispatcher ────────────────────────────────────────
    market_id_counter = {"n": 99}
    async def _fetchval(sql, *args):
        if "FROM trades" in sql and "polymarket_id" not in sql.lower():
            # Concurrency gate (snipe open count).
            return 0
        if "FROM trades" in sql and "polymarket_id" in sql.lower():
            return 0    # per-candidate "existing position?" check
        if "INSERT INTO markets" in sql:
            market_id_counter["n"] += 1
            return market_id_counter["n"]
        return 0
    db.fetchval = AsyncMock(side_effect=_fetchval)

    db.fetch = AsyncMock(return_value=[
        {"polymarket_id": pid} for pid in held_polymarket_ids])
    db.execute = AsyncMock()

    scanner = MagicMock()
    scanner.fetch_markets = AsyncMock(return_value=scanner_markets)
    scanner.get_cached_price = MagicMock(side_effect=lambda pid: scanner_cache.get(pid))

    portfolio_lock = MagicMock()
    portfolio_lock.__aenter__ = AsyncMock()
    portfolio_lock.__aexit__ = AsyncMock()

    executor = MagicMock()
    executor.place_order = AsyncMock(return_value={"trade_id": 1, "shares": 50})

    ctx = TradingContext(
        db=db, scanner=scanner, risk_manager=MagicMock(),
        portfolio_lock=portfolio_lock, executor=executor,
        email_notifier=MagicMock(), settings=None, clob=None)
    return ctx, db, scanner, executor


@pytest.mark.asyncio
async def test_correlation_filter_blocks_second_event_market(monkeypatch):
    """Two GDP bracket markets, same group_slug. We hold one already →
    the second must be rejected with reason `event_group_already_held`."""
    held_pid = "0xgdp_a"
    new_pid = "0xgdp_b"
    scanner_cache = {
        held_pid: _market(held_pid, "us-gdp-q1-2026"),
        new_pid: _market(new_pid, "us-gdp-q1-2026"),
    }
    ctx, db, scanner, executor = _make_ctx(
        scanner_markets=[scanner_cache[new_pid]],
        held_polymarket_ids=[held_pid],
        scanner_cache=scanner_cache)

    settings = _settings()
    ctx = ctx._replace(settings=settings) if hasattr(ctx, "_replace") else ctx
    ctx.settings = settings

    strat = ResolutionSnipeStrategy(settings=settings, gemini_client=None)
    # Force verifier path to NOT block — we want to isolate the correlation
    # gate. Mock to return the correct LOCKED + high confidence.
    from polybot.analysis.gemini_client import GeminiResult

    async def fake_verify(cand, market):
        return GeminiResult(verdict="NO_LOCKED", confidence=1.0,
                            reason="test " * 20)
    monkeypatch.setattr(strat, "_verify", fake_verify)

    # Patch shadow_log.record_signal to a no-op (asserts about the row
    # exist in test_snipe_v12; here we care about entry behavior).
    from polybot.learning import shadow_log
    monkeypatch.setattr(shadow_log, "record_signal",
                        AsyncMock(return_value=None))

    await strat.run_once(ctx)

    # The second market must NOT have been entered.
    executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_correlation_filter_disabled_allows_double_entry(monkeypatch):
    """When the flag is off, both bracket markets pass. Demonstrates the
    gate is the only thing changing behavior."""
    held_pid = "0xgdp_a"
    new_pid = "0xgdp_b"
    scanner_cache = {
        held_pid: _market(held_pid, "us-gdp-q1-2026"),
        new_pid: _market(new_pid, "us-gdp-q1-2026"),
    }
    ctx, db, scanner, executor = _make_ctx(
        scanner_markets=[scanner_cache[new_pid]],
        held_polymarket_ids=[held_pid],
        scanner_cache=scanner_cache)

    settings = _settings(correlation_enabled=False)
    ctx.settings = settings

    strat = ResolutionSnipeStrategy(settings=settings, gemini_client=None)
    from polybot.analysis.gemini_client import GeminiResult

    async def fake_verify(cand, market):
        return GeminiResult(verdict="NO_LOCKED", confidence=1.0,
                            reason="test " * 20)
    monkeypatch.setattr(strat, "_verify", fake_verify)
    from polybot.learning import shadow_log
    monkeypatch.setattr(shadow_log, "record_signal",
                        AsyncMock(return_value=None))

    await strat.run_once(ctx)

    executor.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_correlation_filter_allows_unrelated_groups(monkeypatch):
    """Held: GDP. New candidate: Senate race. Different group_slugs → enter."""
    held_pid = "0xgdp_a"
    new_pid = "0xohio_senate"
    scanner_cache = {
        held_pid: _market(held_pid, "us-gdp-q1-2026"),
        new_pid: _market(new_pid, "ohio-senate-2026"),
    }
    ctx, db, scanner, executor = _make_ctx(
        scanner_markets=[scanner_cache[new_pid]],
        held_polymarket_ids=[held_pid],
        scanner_cache=scanner_cache)

    settings = _settings()
    ctx.settings = settings

    strat = ResolutionSnipeStrategy(settings=settings, gemini_client=None)
    from polybot.analysis.gemini_client import GeminiResult

    async def fake_verify(cand, market):
        return GeminiResult(verdict="NO_LOCKED", confidence=1.0,
                            reason="test " * 20)
    monkeypatch.setattr(strat, "_verify", fake_verify)
    from polybot.learning import shadow_log
    monkeypatch.setattr(shadow_log, "record_signal",
                        AsyncMock(return_value=None))

    await strat.run_once(ctx)
    executor.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_correlation_filter_in_cycle_blocks_double_entry(monkeypatch):
    """Within a single scan cycle, two bracket markets appear and neither
    is held yet. After the first enters, the second must be skipped."""
    pid_a, pid_b = "0xgdp_a", "0xgdp_b"
    scanner_cache = {
        pid_a: _market(pid_a, "us-gdp-q1-2026"),
        pid_b: _market(pid_b, "us-gdp-q1-2026"),
    }
    ctx, db, scanner, executor = _make_ctx(
        scanner_markets=[scanner_cache[pid_a], scanner_cache[pid_b]],
        held_polymarket_ids=[],
        scanner_cache=scanner_cache)

    settings = _settings()
    ctx.settings = settings

    strat = ResolutionSnipeStrategy(settings=settings, gemini_client=None)
    from polybot.analysis.gemini_client import GeminiResult

    async def fake_verify(cand, market):
        return GeminiResult(verdict="NO_LOCKED", confidence=1.0,
                            reason="test " * 20)
    monkeypatch.setattr(strat, "_verify", fake_verify)
    from polybot.learning import shadow_log
    monkeypatch.setattr(shadow_log, "record_signal",
                        AsyncMock(return_value=None))

    await strat.run_once(ctx)

    # Exactly one entry, not two.
    assert executor.place_order.await_count == 1
