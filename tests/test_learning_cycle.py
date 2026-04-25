"""Tests for the v11.0c hourly learning cycle orchestrator.

Validates the orchestration layer that combines existing primitives:
- OnlineCalibrator refit from sport_calibration
- Beta-Binomial Kelly scaler refit from trade_outcome
- Edge-decay disable evaluation per (strategy, category)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from polybot.learning.learning_cycle import (
    refit_kelly_scalers, evaluate_edge_decay, refit_sport_calibrators,
)


def _outcome_row(strategy="live_sports", category="moneyline",
                  pnl=1.0, predicted_prob=0.85, row_id=1):
    return {
        "id": row_id, "strategy": strategy, "market_category": category,
        "pnl": pnl, "predicted_prob": predicted_prob,
    }


# ---- refit_kelly_scalers ---------------------------------------------------

@pytest.mark.asyncio
async def test_refit_kelly_scalers_updates_strategy_performance():
    """End-to-end: outcomes are aggregated by strategy and the scaler row
    in strategy_performance is upserted."""
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[
        _outcome_row(strategy="live_sports", pnl=1.0, predicted_prob=0.85, row_id=i)
        for i in range(25)
    ])
    db.execute = AsyncMock()

    result = await refit_kelly_scalers(db, strategies=["live_sports"])

    assert "live_sports" in result
    scaler, avg_pred = result["live_sports"]
    # 25 wins, 0 losses, avg_pred=0.85 → posterior heavily above predicted
    assert scaler > 1.0
    assert scaler <= 2.0   # clamped
    # The DB UPSERT should have been called with strategy + scaler
    assert db.execute.await_count >= 1
    args = db.execute.await_args.args
    assert "strategy_performance" in args[0]


@pytest.mark.asyncio
async def test_refit_kelly_scalers_skips_unknown_strategy():
    """Strategy not in our list shouldn't appear in result."""
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()

    result = await refit_kelly_scalers(db, strategies=["live_sports", "snipe"])
    # Both strategies queried even with no outcomes — produces 1.0 default
    assert "live_sports" in result and "snipe" in result
    for strategy, (scaler, _avg) in result.items():
        # Cold-start (no outcomes) keeps scaler at 1.0
        assert scaler == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_refit_kelly_scalers_handles_db_errors_gracefully():
    """If the per-strategy fetch fails, that strategy is skipped — others continue."""
    db = MagicMock()
    db.fetch = AsyncMock(side_effect=Exception("boom"))
    db.execute = AsyncMock()
    result = await refit_kelly_scalers(db, strategies=["live_sports"])
    # On error we keep the scaler conservative (1.0) and don't write
    assert result["live_sports"] == (1.0, None)


# ---- evaluate_edge_decay --------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_edge_decay_disables_when_short_negative_long_positive():
    """50 recent losing trades + 200 mostly winning earlier trades → disable."""
    db = MagicMock()
    # Build outcomes ordered by id ascending: 200 wins (long_mean+) then 50 losses (short_mean-).
    rows = []
    for i in range(200):
        rows.append({"id": i, "strategy": "live_sports", "market_category": "moneyline",
                      "pnl": 0.5})
    for i in range(200, 250):
        rows.append({"id": i, "strategy": "live_sports", "market_category": "moneyline",
                      "pnl": -1.0})
    db.fetch = AsyncMock(return_value=rows)
    db.execute = AsyncMock()

    disabled = await evaluate_edge_decay(
        db, strategies=["live_sports"], categories=["moneyline"])

    assert disabled == [("live_sports", "moneyline")]
    # decay_disabled_until written
    assert db.execute.await_count >= 1
    args = db.execute.await_args.args
    assert "decay_disabled_until" in args[0]


@pytest.mark.asyncio
async def test_evaluate_edge_decay_no_disable_when_short_positive():
    """All winning recent trades → no disable."""
    db = MagicMock()
    rows = [{"id": i, "strategy": "live_sports", "market_category": "moneyline",
             "pnl": 0.5} for i in range(60)]
    db.fetch = AsyncMock(return_value=rows)
    db.execute = AsyncMock()

    disabled = await evaluate_edge_decay(
        db, strategies=["live_sports"], categories=["moneyline"])

    assert disabled == []


# ---- refit_sport_calibrators ---------------------------------------------

@pytest.mark.asyncio
async def test_refit_sport_calibrators_invokes_strategy_refit():
    """Calls live_sports.refit_calibrator(db) when present on the strategy."""
    db = MagicMock()
    strategy = MagicMock()
    strategy.name = "live_sports"
    strategy.refit_calibrator = AsyncMock()
    refitted = await refit_sport_calibrators(db, [strategy])
    strategy.refit_calibrator.assert_awaited_once()
    assert refitted == ["live_sports"]


@pytest.mark.asyncio
async def test_refit_sport_calibrators_skips_strategy_without_method():
    """Strategy without refit_calibrator is silently skipped (e.g., snipe)."""
    db = MagicMock()
    strategy = MagicMock(spec=["name"])
    strategy.name = "snipe"
    refitted = await refit_sport_calibrators(db, [strategy])
    assert refitted == []


@pytest.mark.asyncio
async def test_refit_sport_calibrators_handles_strategy_error():
    """An exception in one strategy's refit doesn't block others."""
    db = MagicMock()
    bad = MagicMock()
    bad.name = "live_sports"
    bad.refit_calibrator = AsyncMock(side_effect=Exception("boom"))
    good = MagicMock()
    good.name = "pregame_sharp"
    good.refit_calibrator = AsyncMock()
    refitted = await refit_sport_calibrators(db, [bad, good])
    assert refitted == ["pregame_sharp"]
    good.refit_calibrator.assert_awaited_once()
