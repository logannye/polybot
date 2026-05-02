"""Rolling-50 hit-rate killswitch.

The single adaptive component in v12. Snipe T1 needs ≥97% hit rate to be
EV-positive at 0.96+ entry prices. This module:

  1. Recomputes the rolling-50 hit rate on every resolution event.
  2. Trips the killswitch if the rate drops below `min_hit_rate` (default
     0.97) AND we have at least `min_n` resolved trades (default 50).
  3. On trip: writes `killswitch_tripped_at` to system_state, halts entries,
     and demotes `live_deployment_stage` by one tier.

The killswitch is the hard backstop, not a tuning knob. It is one-shot per
trip — once tripped, only a manual operator action (`reset_killswitch`)
clears it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

import structlog

log = structlog.get_logger()


# Stage demotion ladder (matches deployment_stage.py order).
_STAGE_LADDER = ["dry_run", "preflight", "micro_test", "ramp", "full"]


def _demote(stage: str) -> str:
    if stage not in _STAGE_LADDER:
        return "dry_run"
    idx = _STAGE_LADDER.index(stage)
    if idx == 0:
        return stage    # already at the floor
    return _STAGE_LADDER[idx - 1]


async def update_and_check(
    db,
    *,
    window: int = 50,
    min_hit_rate: float = 0.97,
    min_n: int = 50,
    email_notifier=None,
) -> Tuple[float, int, bool]:
    """Recompute the rolling hit rate and trip the killswitch if breached.

    Returns ``(hit_rate, n, tripped_now)``. ``hit_rate`` is None-safe (returns
    1.0 when there are no resolved trades — no evidence of failure).

    Idempotent — if already tripped, this re-checks the gauge but does not
    re-emit alerts.
    """
    rows = await db.fetch(
        """SELECT realized_outcome FROM trade_outcome
           WHERE strategy = 'snipe' AND realized_outcome IS NOT NULL
           ORDER BY closed_at DESC LIMIT $1""",
        window)
    n = len(rows)
    if n == 0:
        hit_rate = 1.0
    else:
        wins = sum(1 for r in rows if r["realized_outcome"] == 1)
        hit_rate = wins / n

    state = await db.fetchrow(
        "SELECT killswitch_tripped_at, live_deployment_stage FROM system_state WHERE id = 1")
    already_tripped = bool(state and state["killswitch_tripped_at"])

    await db.execute(
        """UPDATE system_state
           SET rolling_hit_rate = $1, rolling_hit_rate_n = $2
           WHERE id = 1""",
        round(hit_rate, 4), n)

    tripped_now = False
    if not already_tripped and n >= min_n and hit_rate < min_hit_rate:
        # Trip.
        now = datetime.now(timezone.utc)
        prior_stage = state["live_deployment_stage"] if state else "dry_run"
        new_stage = _demote(prior_stage)
        reason = (f"rolling_{window}_hit_rate={hit_rate:.4f} < {min_hit_rate} "
                  f"over n={n} closed snipe trades")
        await db.execute(
            """UPDATE system_state
               SET killswitch_tripped_at = $1,
                   killswitch_reason = $2,
                   live_deployment_stage = $3
               WHERE id = 1""",
            now, reason, new_stage)
        tripped_now = True
        log.critical("KILLSWITCH_TRIPPED", hit_rate=round(hit_rate, 4),
                     n=n, prior_stage=prior_stage, new_stage=new_stage,
                     reason=reason)
        if email_notifier:
            try:
                await email_notifier.send(
                    "[POLYBOT CRITICAL] Hit-rate killswitch tripped",
                    f"<p>Rolling-{window} hit rate <b>{hit_rate:.2%}</b> over "
                    f"{n} closed snipe trades — below {min_hit_rate:.0%} floor.</p>"
                    f"<p>All entries halted. Deployment stage demoted: "
                    f"<code>{prior_stage}</code> → <code>{new_stage}</code>.</p>"
                    f"<p>Reason: {reason}</p>"
                    f"<p>Manual reset required (<code>reset_killswitch</code>).</p>")
            except Exception as e:
                log.error("killswitch_email_failed", error=str(e))

    return hit_rate, n, tripped_now


async def is_tripped(db) -> bool:
    """Cheap check used by the strategy loop to gate entries."""
    row = await db.fetchrow(
        "SELECT killswitch_tripped_at FROM system_state WHERE id = 1")
    return bool(row and row["killswitch_tripped_at"])


async def reset_killswitch(db, *, operator_note: str = "") -> bool:
    """Manual unhalt. Returns True if a trip was cleared."""
    row = await db.fetchrow(
        "SELECT killswitch_tripped_at FROM system_state WHERE id = 1")
    if not row or not row["killswitch_tripped_at"]:
        return False
    await db.execute(
        """UPDATE system_state
           SET killswitch_tripped_at = NULL, killswitch_reason = NULL
           WHERE id = 1""")
    log.warning("killswitch_reset", note=operator_note)
    return True
