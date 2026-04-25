"""v11.0c — hourly learning cycle orchestrator.

Wires the v10 spec § 5 Loop 2 deferred items:
- Per-strategy Beta-Binomial Kelly scaler refit (writes strategy_performance.kelly_scaler)
- Per-(strategy, category) edge decay evaluation (writes decay_disabled_until)
- Per-strategy sport calibrator refit (delegated to strategy.refit_calibrator if present)

All three are independent. Errors in one don't block the others; each is
wrapped in try/except and logged. Per-strategy progress is returned so the
engine can surface metrics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import structlog

from polybot.learning.edge_decay import evaluate_decay, LONG_WINDOW
from polybot.learning.kelly_scaler import compute_from_outcomes

log = structlog.get_logger()

# Default category list for edge-decay evaluation. Strategies can extend
# this if they trade additional categories.
DEFAULT_CATEGORIES = ["moneyline", "spread", "total"]

# How far back to pull outcomes for the kelly + edge-decay analysis.
KELLY_WINDOW_DAYS = 30
DECAY_DISABLE_HOURS = 48


async def refit_kelly_scalers(
    db, strategies: Iterable[str],
    *, window_days: int = KELLY_WINDOW_DAYS,
    cold_start_n: int = 20,
) -> dict[str, tuple[float, Optional[float]]]:
    """Compute Beta-Binomial Kelly scaler per strategy and persist.

    Each strategy is queried independently — a failure on one doesn't
    cascade. Returns ``{strategy: (scaler, avg_predicted_prob)}``.
    """
    results: dict[str, tuple[float, Optional[float]]] = {}
    for strategy in strategies:
        try:
            outcomes = await db.fetch(
                """SELECT pnl, predicted_prob FROM trade_outcome
                   WHERE strategy = $1 AND closed_at > NOW() - $2::interval""",
                strategy, f"{window_days} days",
            )
            scaler, avg_pred = compute_from_outcomes(
                outcomes, cold_start_n=cold_start_n)
            results[strategy] = (scaler, avg_pred)

            # UPSERT so a never-traded strategy still gets a row at scaler=1.0
            await db.execute(
                """INSERT INTO strategy_performance (strategy, kelly_scaler, last_updated)
                   VALUES ($1, $2, NOW())
                   ON CONFLICT (strategy)
                   DO UPDATE SET kelly_scaler = EXCLUDED.kelly_scaler,
                                 last_updated = EXCLUDED.last_updated""",
                strategy, scaler,
            )
            log.info("kelly_scaler_refit",
                     strategy=strategy, scaler=round(scaler, 3),
                     avg_predicted=round(avg_pred, 4) if avg_pred is not None else None,
                     n_outcomes=len(outcomes))
        except Exception as e:
            log.error("kelly_scaler_refit_failed",
                      strategy=strategy, error=str(e))
            results[strategy] = (1.0, None)
    return results


async def evaluate_edge_decay(
    db, strategies: Iterable[str], categories: Iterable[str] = DEFAULT_CATEGORIES,
    *, disable_hours: int = DECAY_DISABLE_HOURS,
) -> list[tuple[str, str]]:
    """Evaluate per-(strategy, category) edge decay, disable if triggered.

    Returns the list of disabled (strategy, category) pairs.
    """
    disabled: list[tuple[str, str]] = []
    until = datetime.now(timezone.utc) + timedelta(hours=disable_hours)

    for strategy in strategies:
        for category in categories:
            try:
                outcomes = await db.fetch(
                    """SELECT id, pnl FROM trade_outcome
                       WHERE strategy = $1 AND market_category = $2
                       ORDER BY id ASC
                       LIMIT $3""",
                    strategy, category, LONG_WINDOW,
                )
                # Build dict-shaped iterable; rows already have id+pnl.
                rows = [{"id": r["id"], "pnl": r["pnl"]} for r in outcomes]
                verdict = evaluate_decay(rows)
                if verdict.should_disable:
                    disabled.append((strategy, category))
                    # Persist on strategy_performance — categories share the
                    # same row, so we use a JSONB column to avoid per-category
                    # rows. For now keep it simple: only writes the first
                    # category's disable; in v12 split into a per-category
                    # table if needed. Multiple disabled categories on the
                    # same strategy still all log; the table just records the
                    # most recent timestamp, which is fine for monitoring.
                    await db.execute(
                        """UPDATE strategy_performance
                           SET decay_disabled_until = $1, last_updated = NOW()
                           WHERE strategy = $2""",
                        until, strategy,
                    )
                    log.warning("edge_decay_disabled",
                                strategy=strategy, category=category,
                                short_mean=round(verdict.short_mean, 4)
                                if verdict.short_mean is not None else None,
                                long_mean=round(verdict.long_mean, 4)
                                if verdict.long_mean is not None else None,
                                until=until.isoformat())
            except Exception as e:
                log.error("edge_decay_failed",
                          strategy=strategy, category=category, error=str(e))
    return disabled


async def refit_sport_calibrators(db, strategies: list) -> list[str]:
    """Trigger each strategy's calibrator refit if it exposes one.

    Strategies that don't have ``refit_calibrator`` (e.g., Snipe) are
    silently skipped. Failures don't block other strategies.
    """
    refitted: list[str] = []
    for strategy in strategies:
        method = getattr(strategy, "refit_calibrator", None)
        if method is None:
            continue
        try:
            await method(db)
            refitted.append(strategy.name)
            log.info("calibrator_refit_complete", strategy=strategy.name)
        except Exception as e:
            log.error("calibrator_refit_failed",
                      strategy=strategy.name, error=str(e))
    return refitted


async def run_hourly_cycle(db, strategy_objs: list, *,
                             kelly_window_days: int = KELLY_WINDOW_DAYS) -> dict:
    """Single-call orchestrator — runs the three subroutines in sequence.

    Returns a metrics dict for monitoring/logging. The engine's
    ``_hourly_learning`` invokes this; tests cover the subroutines
    individually.
    """
    strategy_names = [s.name for s in strategy_objs]
    metrics: dict = {}

    metrics["kelly"] = await refit_kelly_scalers(
        db, strategy_names, window_days=kelly_window_days)
    metrics["decay_disabled"] = await evaluate_edge_decay(db, strategy_names)
    metrics["calibrators_refit"] = await refit_sport_calibrators(db, strategy_objs)

    log.info("hourly_learning_cycle_complete",
             strategies=strategy_names,
             n_disabled=len(metrics["decay_disabled"]),
             n_calibrators=len(metrics["calibrators_refit"]))
    return metrics
