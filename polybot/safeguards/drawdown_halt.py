"""Total drawdown halt — permanent stop at N% loss from high-water mark.

Extracted from polybot.core.engine.Engine._check_drawdown_halt. Behavior
preserved bit-for-bit including the 30s result cache.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger()


class DrawdownHalt:
    def __init__(self, db, settings, email_notifier=None, cache_ttl_seconds: float = 30.0):
        self._db = db
        self._settings = settings
        self._email = email_notifier
        self._cache_ttl = cache_ttl_seconds
        self._cache: tuple[bool, float] | None = None  # (result, monotonic_timestamp)

    async def check(self) -> bool:
        """Return True if trading should be halted due to drawdown."""
        if self._cache is not None:
            cached_result, cached_at = self._cache
            if time.monotonic() - cached_at < self._cache_ttl:
                return cached_result

        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            self._cache = (False, time.monotonic())
            return False

        bankroll = float(state["bankroll"])
        high_water = float(state.get("high_water_bankroll", bankroll) or bankroll)
        halt_until = state.get("drawdown_halt_until")

        if halt_until and halt_until > datetime.now(timezone.utc):
            self._cache = (True, time.monotonic())
            return True

        if bankroll > high_water:
            await self._db.execute(
                "UPDATE system_state SET high_water_bankroll = $1 WHERE id = 1", bankroll)
            self._cache = (False, time.monotonic())
            return False

        if high_water > 0:
            drawdown = 1.0 - (bankroll / high_water)
            max_drawdown = getattr(self._settings, "max_total_drawdown_pct", 0.30)
            if drawdown >= max_drawdown:
                halt_time = datetime.now(timezone.utc) + timedelta(days=365)
                await self._db.execute(
                    "UPDATE system_state SET drawdown_halt_until = $1 WHERE id = 1",
                    halt_time)
                log.critical("DRAWDOWN_HALT", bankroll=bankroll, high_water=high_water,
                             drawdown_pct=round(drawdown * 100, 1))
                if self._email:
                    try:
                        await self._email.send(
                            "[POLYBOT CRITICAL] DRAWDOWN HALT — ALL TRADING STOPPED",
                            f"<p>Bankroll ${bankroll:.2f} is {drawdown*100:.1f}% below "
                            f"high-water ${high_water:.2f}. Threshold: {max_drawdown*100:.0f}%.</p>"
                            f"<p>All trading halted. Manual DB reset required to resume.</p>")
                    except Exception:
                        pass
                self._cache = (True, time.monotonic())
                return True

        self._cache = (False, time.monotonic())
        return False
