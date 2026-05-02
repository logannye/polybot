"""Shadow signal log — record every snipe candidate that surfaces, regardless
of whether it filled, was filter-rejected, or was verifier-rejected.

The point of this module is to make snipe edge measurable independently of
the executor. Every signal lands a row; later, when the underlying market
resolves, we backfill `resolved_outcome` and `hypothetical_pnl` so we can
answer questions like:
  - What would Brier score look like if we'd filled at mid?
  - Is the verifier accurate on rejected reasons too?
  - Which reject reasons cost us the most expected value?

Pure function `signal_id = await record_signal(...)` is the only entry point
during a scan. Resolution backfill happens in a separate path keyed by
polymarket_id (see `backfill_resolution`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class SignalRecord:
    polymarket_id: str
    yes_price: float
    hours_remaining: float
    side: str               # "YES" or "NO"
    buy_price: float
    verifier_verdict: Optional[str] = None
    verifier_confidence: Optional[float] = None
    verifier_reason: Optional[str] = None
    passed_filter: bool = False
    fill_attempted: bool = False
    filled: bool = False
    reject_reason: Optional[str] = None
    hypothetical_pnl: Optional[float] = None


async def record_signal(db, rec: SignalRecord) -> int:
    """Insert a shadow_signal row. Returns the row id.

    Idempotency is the caller's responsibility — typically the snipe loop
    dedups by `polymarket_id` against the past 5 minutes before recording.
    """
    return await db.fetchval(
        """INSERT INTO shadow_signal (
               polymarket_id, yes_price, hours_remaining, side, buy_price,
               verifier_verdict, verifier_confidence, verifier_reason,
               passed_filter, fill_attempted, filled, reject_reason,
               hypothetical_pnl)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
           RETURNING id""",
        rec.polymarket_id, rec.yes_price, rec.hours_remaining, rec.side,
        rec.buy_price, rec.verifier_verdict, rec.verifier_confidence,
        rec.verifier_reason, rec.passed_filter, rec.fill_attempted,
        rec.filled, rec.reject_reason, rec.hypothetical_pnl,
    )


async def backfill_resolution(db, polymarket_id: str, outcome: int) -> int:
    """Backfill `resolved_outcome` and `hypothetical_pnl` for every shadow
    signal on this market that hasn't been resolved yet.

    `hypothetical_pnl` is computed at the entry price (i.e. the answer to
    "if we'd been able to fill at our intended price, what would the trade
    have made?"), so it's directly comparable to realized P&L.

    Returns the number of rows backfilled.
    """
    from datetime import datetime, timezone
    if outcome not in (0, 1):
        log.warning("shadow_backfill_invalid_outcome",
                    polymarket_id=polymarket_id, outcome=outcome)
        return 0
    rows = await db.fetch(
        """SELECT id, side, buy_price FROM shadow_signal
           WHERE polymarket_id = $1 AND resolved_at IS NULL""",
        polymarket_id)
    n = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        side = r["side"]
        buy_price = float(r["buy_price"])
        won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
        # Per-dollar P&L. Multiplied by intended size when consumed.
        per_dollar_pnl = (1.0 / buy_price) - 1.0 if won else -1.0
        await db.execute(
            """UPDATE shadow_signal
               SET resolved_outcome = $1,
                   hypothetical_pnl = $2,
                   resolved_at = $3
               WHERE id = $4""",
            outcome, per_dollar_pnl, now, r["id"])
        n += 1
    if n:
        log.info("shadow_signals_backfilled",
                 polymarket_id=polymarket_id, n=n, outcome=outcome)
    return n
