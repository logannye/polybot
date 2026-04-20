"""Deployment stage gate — caps deployed capital per v10 ladder per spec §6.

Stages: 0=dry_run (70% cap), 1=preflight, 2=micro_test (5% cap),
3=ramp (25% cap), 4=full (70% cap). Read-only check — returns max $
allowed for NEW trades given current deployed.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()

STAGE_CAPS = {
    "dry_run": 0.70,
    "preflight": 0.0,
    "micro_test": 0.05,
    "ramp": 0.25,
    "full": 0.70,
}


class DeploymentStageGate:
    def __init__(self, db, settings):
        self._db = db
        self._settings = settings

    async def available_capital(self) -> float:
        """Return $ amount that may still be deployed for new trades."""
        stage = getattr(self._settings, "live_deployment_stage", "dry_run")
        cap_pct = STAGE_CAPS.get(stage, 0.70)
        state = await self._db.fetchrow(
            "SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
        if not state:
            return 0.0
        bankroll = float(state["bankroll"])
        deployed = float(state["total_deployed"])
        allowed = bankroll * cap_pct
        return max(0.0, allowed - deployed)
