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
