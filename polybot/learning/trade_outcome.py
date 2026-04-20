"""Trade-outcome recording per v10 spec §5 Loop 1.

On every position close, append a row to ``trade_outcome`` capturing
predicted vs realized state. All other learning loops (Kelly scaler,
edge decay, calibrator refit, weekly reflection) read from this table.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

log = structlog.get_logger()


async def record_outcome(
    db,
    strategy: str,
    market_id: int,
    market_category: str,
    entry_price: float,
    exit_price: Optional[float],
    pnl: float,
    predicted_prob: Optional[float],
    realized_outcome: Optional[int],
    exit_reason: str,
    duration_minutes: float,
    kelly_inputs: Optional[dict] = None,
    game_state_bucket: Optional[str] = None,
    tier: Optional[int] = None,
) -> int:
    """Append one trade_outcome row. Returns the inserted row id.

    Non-fatal: logs and returns -1 on error.
    """
    try:
        row_id = await db.fetchval(
            """INSERT INTO trade_outcome
                  (strategy, market_id, market_category, entry_price, exit_price,
                   pnl, predicted_prob, realized_outcome,
                   game_state_bucket, tier,
                   kelly_inputs, exit_reason, duration_minutes)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
               RETURNING id""",
            strategy, market_id, market_category, entry_price, exit_price,
            pnl, predicted_prob, realized_outcome,
            game_state_bucket, tier,
            json.dumps(kelly_inputs or {}), exit_reason, duration_minutes,
        )
        return int(row_id) if row_id is not None else -1
    except Exception as e:
        log.error("trade_outcome_record_failed", error=str(e)[:200],
                  strategy=strategy, market_id=market_id)
        return -1


async def fetch_recent(db, strategy: str, limit: int = 200) -> list[dict]:
    """Fetch most-recent trade outcomes for a strategy, oldest last."""
    rows = await db.fetch(
        """SELECT * FROM trade_outcome
           WHERE strategy = $1
           ORDER BY id DESC
           LIMIT $2""",
        strategy, limit)
    return [dict(r) for r in rows]
