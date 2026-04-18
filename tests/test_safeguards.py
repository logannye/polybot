import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_drawdown_halt_stops_strategy():
    """Strategy should not run when drawdown halt is active."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 300, "high_water_bankroll": 500,
        "drawdown_halt_until": datetime.now(timezone.utc) + timedelta(hours=24),
        "total_deployed": 0, "daily_pnl": 0, "circuit_breaker_until": None,
        "post_breaker_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._context = MagicMock()
    engine._drawdown_cache = None

    result = await engine._check_drawdown_halt()
    assert result is True


@pytest.mark.asyncio
async def test_drawdown_triggers_when_below_threshold():
    """Should trigger halt when bankroll drops 30%+ below high-water."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 300, "high_water_bankroll": 500,
        "drawdown_halt_until": None,
        "total_deployed": 0, "daily_pnl": 0, "circuit_breaker_until": None,
        "post_breaker_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()
    engine._drawdown_cache = None

    result = await engine._check_drawdown_halt()
    assert result is True
    db.execute.assert_called()


@pytest.mark.asyncio
async def test_no_halt_when_within_threshold():
    """Should not trigger halt when drawdown is within limits."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 400, "high_water_bankroll": 500,
        "drawdown_halt_until": None,
        "total_deployed": 0, "daily_pnl": 0, "circuit_breaker_until": None,
        "post_breaker_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._drawdown_cache = None

    result = await engine._check_drawdown_halt()
    assert result is False


@pytest.mark.asyncio
async def test_capital_divergence_triggers_halt():
    """Should halt when CLOB balance diverges > 10% from DB bankroll."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 500, "total_deployed": 0,
    })

    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=100.0)

    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._clob = clob
    engine._settings = settings
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()
    engine._capital_divergence_halted = False

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True


@pytest.mark.asyncio
async def test_capital_divergence_ok_when_close():
    """Should not halt when CLOB balance is close to DB bankroll."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 500, "total_deployed": 50,
    })

    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=445.0)

    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._clob = clob
    engine._settings = settings
    engine._capital_divergence_halted = False

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is False


@pytest.mark.asyncio
async def test_capital_divergence_self_heals_after_3_ok_checks():
    """Should clear halt after 3 consecutive checks within threshold."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 500, "total_deployed": 50,
    })

    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=445.0)  # 445 vs 450 expected = 1%

    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._clob = clob
    engine._settings = settings
    engine._capital_divergence_halted = True  # previously halted
    engine._capital_divergence_ok_count = 0
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()

    # First two OK checks: still halted
    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True
    assert engine._capital_divergence_ok_count == 1

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True
    assert engine._capital_divergence_ok_count == 2

    # Third OK check: healed
    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is False
    assert engine._capital_divergence_ok_count == 0


@pytest.mark.asyncio
async def test_capital_divergence_resets_ok_count_on_new_divergence():
    """A new divergence during recovery should reset the OK counter."""
    from polybot.core.engine import Engine

    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10

    engine = Engine.__new__(Engine)
    engine._settings = settings
    engine._capital_divergence_halted = True
    engine._capital_divergence_ok_count = 2  # almost healed
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()

    # Divergent check: CLOB balance way off
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={"bankroll": 500, "total_deployed": 0})
    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=100.0)  # 80% divergence

    engine._db = db
    engine._clob = clob

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True
    assert engine._capital_divergence_ok_count == 0  # reset


@pytest.mark.asyncio
async def test_drawdown_check_uses_cache_within_30s():
    """Should return cached result without querying DB within 30s."""
    import time
    from polybot.core.engine import Engine

    db = AsyncMock()
    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._drawdown_cache = (False, time.monotonic())  # cached 'not halted' just now

    result = await engine._check_drawdown_halt()
    assert result is False
    db.fetchrow.assert_not_called()  # should NOT have queried DB


@pytest.mark.asyncio
async def test_drawdown_check_queries_db_after_cache_expires():
    """Should query DB when cache is older than 30s."""
    import time
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 400, "high_water_bankroll": 500,
        "drawdown_halt_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._drawdown_cache = (False, time.monotonic() - 31)  # expired

    result = await engine._check_drawdown_halt()
    assert result is False
    db.fetchrow.assert_called_once()  # SHOULD have queried DB
