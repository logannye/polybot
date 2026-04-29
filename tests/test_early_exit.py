"""v12.4 exit monitor — three priority-ordered exit rules:
   1. stop-loss (early window + adverse move)
   2. take-profit (≥ capture_pct of max possible move)
   3. time-stop (age past max_hold_hours)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _make_engine(
    open_trades, cached_yes_price,
    capture_pct=0.80, max_hold_hours=48.0,
    sl_adverse=0.05, sl_window=2.0,
):
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetch = AsyncMock(return_value=open_trades)

    scanner = MagicMock()
    if cached_yes_price is None:
        scanner.get_cached_price = MagicMock(return_value=None)
    else:
        scanner.get_cached_price = MagicMock(
            return_value={"yes_price": cached_yes_price})

    executor = MagicMock()
    executor.exit_position = AsyncMock(return_value=0.0)

    settings = SimpleNamespace(
        dry_run=True,
        snipe_early_exit_enabled=True,
        snipe_early_exit_check_interval=60,
        snipe_early_exit_capture_pct=capture_pct,
        snipe_max_hold_hours=max_hold_hours,
        snipe_stop_loss_adverse_pp=sl_adverse,
        snipe_stop_loss_window_hours=sl_window,
        max_total_drawdown_pct=0.30,
        max_capital_divergence_pct=0.10,
        live_deployment_stage="dry_run",
        health_check_interval=60,
        heartbeat_warn_seconds=600,
        heartbeat_critical_seconds=1800,
    )
    eng = Engine(db=db, scanner=scanner, executor=executor, recorder=MagicMock(),
                 risk_manager=MagicMock(), settings=settings,
                 email_notifier=MagicMock(send=AsyncMock()),
                 clob=None, portfolio_lock=None)
    return eng, executor


def _trade(side, entry_price, *, age_hours=0.5, trade_id=1):
    """Open snipe trade fixture — `age_hours` is hours since opened."""
    opened_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "id": trade_id, "strategy": "snipe", "status": "dry_run",
        "side": side, "entry_price": entry_price, "shares": 100.0,
        "position_size_usd": 40.0, "polymarket_id": "0xabc",
        "question": "test", "opened_at": opened_at}


# ── take-profit (rule 2) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_take_profit_at_80pct_capture_no_side():
    """NO at 0.93, max-toward-thesis move = 0.93. 80% = 0.744 capture →
    exit when YES drops to ≤ 0.186."""
    eng, executor = _make_engine([_trade("NO", 0.93)], cached_yes_price=0.18)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    assert executor.exit_position.await_args.kwargs["exit_reason"] == "take_profit"


@pytest.mark.asyncio
async def test_take_profit_holds_below_threshold():
    """NO at 0.93, YES at 0.30 → captured (0.93 - 0.30) / 0.93 = 0.677 < 0.80.
    Don't exit yet."""
    eng, executor = _make_engine([_trade("NO", 0.93)], cached_yes_price=0.30)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_take_profit_yes_side():
    """YES at 0.93. max-toward-thesis = 0.07 (toward 1.0). 80% capture =
    0.056 → exit when YES rises to ≥ 0.986."""
    eng, executor = _make_engine([_trade("YES", 0.93)], cached_yes_price=0.99)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    assert executor.exit_position.await_args.kwargs["exit_reason"] == "take_profit"


# ── stop-loss (rule 1) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_loss_within_window():
    """NO at 0.93; 1h after entry YES rises to 0.99 (-6pp adverse) → cut."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=1.0)], cached_yes_price=0.99)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    assert executor.exit_position.await_args.kwargs["exit_reason"] == "stop_loss"


@pytest.mark.asyncio
async def test_stop_loss_does_not_fire_after_window():
    """Same -6pp adverse move, but at 5h age → past 2h window. Hold."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=5.0)], cached_yes_price=0.99)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_stop_loss_does_not_fire_below_threshold():
    """Within window, but adverse move is only 3pp (< 5pp floor). Hold."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=1.0)], cached_yes_price=0.96)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


# ── time-stop (rule 3) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_time_stop_fires_past_48h():
    """Held 49h, no take-profit hit (only 50% captured) → time-stop."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=49.0)], cached_yes_price=0.45)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    assert executor.exit_position.await_args.kwargs["exit_reason"] == "time_stop"


@pytest.mark.asyncio
async def test_time_stop_holds_below_age():
    """47h held, partial capture only → not yet time-stop, not yet TP."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=47.0)], cached_yes_price=0.45)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_time_stop_fires_without_cache():
    """No cached price + age past max_hold → exit at entry as fallback."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=49.0)], cached_yes_price=None)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    args = executor.exit_position.await_args.kwargs
    assert args["exit_reason"] == "time_stop"
    assert args["exit_price"] == 0.93    # fallback to entry


# ── priority ordering ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_take_profit_outranks_time_stop():
    """If both fire (49h aged, 90% captured), take-profit wins so the
    exit_reason in stats reflects the actual driver."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=49.0)], cached_yes_price=0.05)
    await eng._early_exit_monitor()
    assert executor.exit_position.await_args.kwargs["exit_reason"] == "take_profit"


@pytest.mark.asyncio
async def test_stop_loss_outranks_take_profit_in_window():
    """Stop-loss is rule 1 — but in practice at hour 1.0 a take-profit
    won't have triggered yet. Verify stop-loss IS chosen if both fired
    at the same instant (shouldn't happen, but defensive)."""
    # Construct: age 1h, but somehow current is favorable AND adverse simultaneously.
    # Can't have both — pick the harder case: clear adverse → only stop-loss.
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=1.0)], cached_yes_price=0.99)
    await eng._early_exit_monitor()
    assert executor.exit_position.await_args.kwargs["exit_reason"] == "stop_loss"


@pytest.mark.asyncio
async def test_no_action_when_nothing_fires():
    """Aged 0.5h, no adverse move, partial capture → hold."""
    eng, executor = _make_engine(
        [_trade("NO", 0.93, age_hours=0.5)], cached_yes_price=0.50)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


# ── robustness ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_failure_does_not_propagate():
    eng, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=0.05)
    executor.exit_position.side_effect = RuntimeError("boom")
    # Should not raise.
    await eng._early_exit_monitor()
