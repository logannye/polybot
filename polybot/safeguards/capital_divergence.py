"""Capital divergence monitor — halts on CLOB vs DB mismatch > threshold.

Self-heals after 3 consecutive OK checks. Extracted from
polybot.core.engine.Engine._check_capital_divergence.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()


class CapitalDivergenceMonitor:
    def __init__(self, db, clob, settings, email_notifier=None,
                 ok_streak_to_recover: int = 3):
        self._db = db
        self._clob = clob
        self._settings = settings
        self._email = email_notifier
        self._ok_streak_target = ok_streak_to_recover
        self._halted = False
        self._ok_streak = 0

    @property
    def is_halted(self) -> bool:
        return self._halted

    async def check(self) -> None:
        """Run one check cycle; updates internal halt state. No return value."""
        if not self._clob or self._settings.dry_run:
            return
        try:
            state = await self._db.fetchrow(
                "SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
            clob_balance = await self._clob.get_balance()
            expected_cash = float(state["bankroll"]) - float(state["total_deployed"])
            if expected_cash <= 0:
                return
            divergence = abs(clob_balance - expected_cash) / expected_cash
            max_div = getattr(self._settings, "max_capital_divergence_pct", 0.10)
            if divergence > max_div:
                self._halted = True
                self._ok_streak = 0
                log.critical("CAPITAL_DIVERGENCE_HALT", clob=clob_balance,
                             expected=expected_cash, divergence_pct=round(divergence * 100, 1))
                if self._email:
                    try:
                        await self._email.send(
                            "[POLYBOT CRITICAL] Capital divergence halt",
                            f"<p>CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}, "
                            f"Divergence: {divergence*100:.1f}%</p>")
                    except Exception:
                        pass
            elif self._halted:
                self._ok_streak += 1
                if self._ok_streak >= self._ok_streak_target:
                    self._halted = False
                    self._ok_streak = 0
                    log.info("CAPITAL_DIVERGENCE_RECOVERED",
                             clob=clob_balance, expected=expected_cash)
                    if self._email:
                        try:
                            await self._email.send(
                                "[POLYBOT INFO] Capital divergence recovered",
                                f"<p>CLOB balance back in sync after "
                                f"{self._ok_streak_target} consecutive OK checks. "
                                f"CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}</p>")
                        except Exception:
                            pass
        except Exception as e:
            log.error("capital_divergence_check_error", error=str(e))
