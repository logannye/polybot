"""v12.3 early-exit monitor — recycles concurrency slots when a position's
mark-to-market has moved ≥ threshold toward our thesis."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _make_engine(open_trades, cached_yes_price, threshold=0.03):
    """Wire just enough of Engine to drive `_early_exit_monitor`."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetch = AsyncMock(return_value=open_trades)

    scanner = MagicMock()
    scanner.get_cached_price = MagicMock(
        return_value={"yes_price": cached_yes_price})

    executor = MagicMock()
    executor.exit_position = AsyncMock(return_value=0.0)

    settings = SimpleNamespace(
        dry_run=True,
        snipe_early_exit_enabled=True,
        snipe_early_exit_threshold=threshold,
        snipe_early_exit_check_interval=60,
        max_total_drawdown_pct=0.30,
        max_capital_divergence_pct=0.10,
        live_deployment_stage="dry_run",
        health_check_interval=60,
        heartbeat_warn_seconds=600,
        heartbeat_critical_seconds=1800,
    )
    email = MagicMock()
    email.send = AsyncMock()
    risk = MagicMock()
    eng = Engine(db=db, scanner=scanner, executor=executor, recorder=MagicMock(),
                 risk_manager=risk, settings=settings, email_notifier=email,
                 clob=None, portfolio_lock=None)
    return eng, db, scanner, executor


def _trade(side: str, entry_price: float, trade_id: int = 1, polymarket_id: str = "0xabc"):
    return {
        "id": trade_id, "strategy": "snipe", "status": "dry_run",
        "side": side, "entry_price": entry_price, "shares": 100.0,
        "position_size_usd": 40.0,
        "polymarket_id": polymarket_id, "question": "test"}


@pytest.mark.asyncio
async def test_no_position_exits_when_yes_drops_3pp():
    """NO at 0.93; YES drifts to 0.90 (-3pp) → exit."""
    eng, db, scanner, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=0.90)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    args = executor.exit_position.await_args
    assert args.kwargs["exit_price"] == 0.90
    assert args.kwargs["exit_reason"] == "early_exit"


@pytest.mark.asyncio
async def test_no_position_holds_below_threshold():
    """NO at 0.93; YES at 0.91 (-2pp) → don't exit."""
    eng, db, scanner, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=0.91)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_no_position_holds_when_yes_rises():
    """NO at 0.93; YES at 0.96 (against us) → don't exit (we're losing)."""
    eng, db, scanner, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=0.96)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_yes_position_exits_when_yes_rises_3pp():
    """YES at 0.93; YES drifts to 0.97 (+4pp toward thesis) → exit.
    (Use 4pp not 3pp to avoid float-precision tie at threshold.)"""
    eng, db, scanner, executor = _make_engine(
        [_trade("YES", 0.93)], cached_yes_price=0.97)
    await eng._early_exit_monitor()
    executor.exit_position.assert_awaited_once()
    assert executor.exit_position.await_args.kwargs["exit_price"] == 0.97


@pytest.mark.asyncio
async def test_skips_when_no_cached_price():
    eng, db, scanner, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=None)
    scanner.get_cached_price.return_value = None
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_custom_threshold():
    """5pp threshold; 3pp move should NOT trigger exit."""
    eng, db, scanner, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=0.90, threshold=0.05)
    await eng._early_exit_monitor()
    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_exit_failure_does_not_propagate():
    """If exit_position raises, the monitor logs and moves on — one failure
    must not block the others (e.g. when iterating multiple open trades)."""
    eng, db, scanner, executor = _make_engine(
        [_trade("NO", 0.93)], cached_yes_price=0.88)
    executor.exit_position.side_effect = RuntimeError("boom")
    # Should not raise.
    await eng._early_exit_monitor()
