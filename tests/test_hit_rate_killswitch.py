"""Tests for the rolling-50 hit-rate killswitch."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.learning import hit_rate_killswitch as ks


def _state_row(*, stage="micro_test", tripped_at=None):
    return {"killswitch_tripped_at": tripped_at,
            "live_deployment_stage": stage}


@pytest.mark.asyncio
async def test_hit_rate_full_house_does_not_trip():
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[{"realized_outcome": 1}] * 50)
    db.fetchrow = AsyncMock(return_value=_state_row())
    db.execute = AsyncMock()
    rate, n, tripped = await ks.update_and_check(db)
    assert rate == 1.0
    assert n == 50
    assert tripped is False


@pytest.mark.asyncio
async def test_hit_rate_below_floor_trips_at_min_n():
    rows = [{"realized_outcome": 1}] * 47 + [{"realized_outcome": 0}] * 3
    db = MagicMock()
    db.fetch = AsyncMock(return_value=rows)
    db.fetchrow = AsyncMock(return_value=_state_row(stage="micro_test"))
    db.execute = AsyncMock()
    rate, n, tripped = await ks.update_and_check(
        db, window=50, min_hit_rate=0.97, min_n=50)
    assert rate == pytest.approx(0.94)
    assert n == 50
    assert tripped is True
    # Verify trip + demote was written.
    update_calls = [c for c in db.execute.await_args_list
                    if "killswitch_tripped_at" in c.args[0]]
    assert len(update_calls) == 1
    new_stage = update_calls[0].args[3]
    assert new_stage == "preflight"     # demoted from micro_test


@pytest.mark.asyncio
async def test_hit_rate_below_floor_under_min_n_does_not_trip():
    rows = [{"realized_outcome": 0}] * 10
    db = MagicMock()
    db.fetch = AsyncMock(return_value=rows)
    db.fetchrow = AsyncMock(return_value=_state_row())
    db.execute = AsyncMock()
    _, n, tripped = await ks.update_and_check(
        db, window=50, min_hit_rate=0.97, min_n=50)
    assert n == 10
    assert tripped is False


@pytest.mark.asyncio
async def test_already_tripped_does_not_double_trip():
    from datetime import datetime, timezone
    rows = [{"realized_outcome": 0}] * 50
    db = MagicMock()
    db.fetch = AsyncMock(return_value=rows)
    db.fetchrow = AsyncMock(return_value=_state_row(
        tripped_at=datetime(2026, 4, 1, tzinfo=timezone.utc)))
    db.execute = AsyncMock()
    _, _, tripped_now = await ks.update_and_check(db)
    assert tripped_now is False    # already tripped, don't re-trip


@pytest.mark.asyncio
async def test_demote_floors_at_dry_run():
    rows = [{"realized_outcome": 0}] * 50
    db = MagicMock()
    db.fetch = AsyncMock(return_value=rows)
    db.fetchrow = AsyncMock(return_value=_state_row(stage="dry_run"))
    db.execute = AsyncMock()
    await ks.update_and_check(db)
    update_calls = [c for c in db.execute.await_args_list
                    if "killswitch_tripped_at" in c.args[0]]
    assert update_calls[0].args[3] == "dry_run"


@pytest.mark.asyncio
async def test_is_tripped_reflects_state():
    from datetime import datetime, timezone
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={
        "killswitch_tripped_at": datetime(2026, 4, 1, tzinfo=timezone.utc)})
    assert await ks.is_tripped(db) is True
    db.fetchrow = AsyncMock(return_value={"killswitch_tripped_at": None})
    assert await ks.is_tripped(db) is False


@pytest.mark.asyncio
async def test_reset_killswitch_clears_trip():
    from datetime import datetime, timezone
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={
        "killswitch_tripped_at": datetime(2026, 4, 1, tzinfo=timezone.utc)})
    db.execute = AsyncMock()
    cleared = await ks.reset_killswitch(db, operator_note="manual")
    assert cleared is True
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_killswitch_noop_when_not_tripped():
    db = MagicMock()
    db.fetchrow = AsyncMock(return_value={"killswitch_tripped_at": None})
    db.execute = AsyncMock()
    cleared = await ks.reset_killswitch(db)
    assert cleared is False
    db.execute.assert_not_awaited()
