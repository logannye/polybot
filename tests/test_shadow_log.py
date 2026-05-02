"""Tests for the shadow_signal recorder + resolution backfill."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.learning import shadow_log


@pytest.mark.asyncio
async def test_record_signal_inserts_row():
    db = MagicMock()
    db.fetchval = AsyncMock(return_value=42)
    rec = shadow_log.SignalRecord(
        polymarket_id="abc", yes_price=0.97, hours_remaining=2.0,
        side="YES", buy_price=0.97,
        verifier_verdict="YES_LOCKED", verifier_confidence=0.99,
        verifier_reason="AP called the race at 8:14pm ET",
        passed_filter=True)
    rid = await shadow_log.record_signal(db, rec)
    assert rid == 42
    db.fetchval.assert_awaited_once()
    args = db.fetchval.await_args.args
    assert "INSERT INTO shadow_signal" in args[0]
    assert args[1] == "abc"
    assert args[4] == "YES"
    assert args[6] == "YES_LOCKED"


@pytest.mark.asyncio
async def test_backfill_marks_winners_and_losers():
    db = MagicMock()
    # YES side wins on outcome=1 (per dollar +0.0309 at buy=0.97)
    # NO side loses on outcome=1 (per dollar -1.0)
    db.fetch = AsyncMock(return_value=[
        {"id": 1, "side": "YES", "buy_price": 0.97},
        {"id": 2, "side": "NO",  "buy_price": 0.96},
    ])
    db.execute = AsyncMock()
    n = await shadow_log.backfill_resolution(db, "abc", outcome=1)
    assert n == 2
    assert db.execute.await_count == 2
    # Verify per-dollar PnL roughly: YES win = (1/0.97) - 1 ≈ 0.0309
    yes_pnl = db.execute.await_args_list[0].args[2]
    no_pnl = db.execute.await_args_list[1].args[2]
    assert yes_pnl == pytest.approx(1.0 / 0.97 - 1.0, rel=1e-3)
    assert no_pnl == -1.0


@pytest.mark.asyncio
async def test_backfill_invalid_outcome_returns_zero():
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[{"id": 1, "side": "YES", "buy_price": 0.97}])
    n = await shadow_log.backfill_resolution(db, "abc", outcome=2)
    assert n == 0


@pytest.mark.asyncio
async def test_backfill_no_unresolved_rows():
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])
    n = await shadow_log.backfill_resolution(db, "abc", outcome=1)
    assert n == 0
