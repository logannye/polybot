import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from polybot.learning.trade_learning import TradeLearner


@pytest.fixture
def settings():
    s = MagicMock()
    s.enable_proxy_trust_learning = True
    s.proxy_brier_alpha_tp = 0.05
    s.proxy_brier_alpha_sl = 0.08
    s.proxy_brier_alpha_weak = 0.03
    s.enable_adaptive_thresholds = True
    s.adaptive_threshold_min_trades = 10
    s.enable_snipe_learning = True
    s.take_profit_threshold = 0.30
    s.stop_loss_threshold = 0.25
    s.snipe_min_net_edge = 0.02
    return s


@pytest.fixture
def db():
    return AsyncMock()


@pytest.mark.asyncio
async def test_on_trade_closed_updates_exit_stats(db, settings):
    """on_trade_closed should update exit_reason stats in learned_params."""
    now = datetime.now(timezone.utc)
    db.fetchrow = AsyncMock(side_effect=[
        # Trade lookup
        {"id": 1, "analysis_id": 10, "pnl": 2.50, "strategy": "snipe",
         "exit_reason": "take_profit", "market_id": 5,
         "opened_at": now - timedelta(minutes=30), "closed_at": now},
        # Analysis lookup
        {"edge": 0.04, "model_estimates": "[]"},
        # Market lookup (for category_scores)
        {"category": "crypto"},
        # system_state lookup (for category_scores)
        {"category_scores": "{}"},
        # strategy_performance lookup (for avg_edge)
        {"avg_edge": 0.03, "total_trades": 10},
    ])
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(1)

    # Check that learned_params was updated with exit_stats
    calls = [c for c in db.execute.call_args_list if "learned_params" in str(c)]
    assert len(calls) >= 1
    params_json = calls[0].args[0] if not "UPDATE" in str(calls[0].args[0]) else calls[0].args[1]
    # The first positional arg to execute after the SQL should be the JSON
    for call in db.execute.call_args_list:
        sql = call.args[0]
        if "learned_params" in sql:
            params = json.loads(call.args[1])
            assert "exit_stats" in params
            assert "take_profit" in params["exit_stats"]
            assert params["exit_stats"]["take_profit"]["count"] == 1
            assert params["exit_stats"]["take_profit"]["total_pnl"] == 2.50
            break


@pytest.mark.asyncio
async def test_on_trade_closed_updates_category_scores(db, settings):
    """on_trade_closed should update category_scores in system_state."""
    now = datetime.now(timezone.utc)
    db.fetchrow = AsyncMock(side_effect=[
        # Trade
        {"id": 2, "analysis_id": 11, "pnl": -1.00, "strategy": "forecast",
         "exit_reason": "stop_loss", "market_id": 6,
         "opened_at": now - timedelta(hours=2), "closed_at": now},
        # Analysis
        {"edge": 0.05, "model_estimates": "[]"},
        # Market
        {"category": "politics"},
        # system_state
        {"category_scores": '{"politics": {"trades": 5, "pnl": 3.0, "wins": 3}}'},
        # strategy_performance
        {"avg_edge": 0.04, "total_trades": 20},
    ])
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(2)

    # Check category_scores update
    for call in db.execute.call_args_list:
        sql = call.args[0]
        if "category_scores" in sql:
            scores = json.loads(call.args[1])
            assert scores["politics"]["trades"] == 6
            assert scores["politics"]["pnl"] == 2.0  # 3.0 + (-1.0)
            assert scores["politics"]["wins"] == 3  # no new win
            break


@pytest.mark.asyncio
async def test_on_trade_closed_updates_avg_edge(db, settings):
    """on_trade_closed should update strategy_performance.avg_edge."""
    now = datetime.now(timezone.utc)
    db.fetchrow = AsyncMock(side_effect=[
        # Trade
        {"id": 3, "analysis_id": 12, "pnl": 1.00, "strategy": "forecast",
         "exit_reason": "take_profit", "market_id": 7,
         "opened_at": now - timedelta(minutes=10), "closed_at": now},
        # Analysis
        {"edge": 0.08, "model_estimates": "[]"},
        # Market
        {"category": "sports"},
        # system_state
        {"category_scores": "{}"},
        # strategy_performance for avg_edge
        {"avg_edge": 0.04, "total_trades": 10},
    ])
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(3)

    # Check avg_edge update
    for call in db.execute.call_args_list:
        sql = call.args[0]
        if "avg_edge" in sql:
            new_avg = call.args[1]
            # Running avg: (0.04 * 9 + 0.08) / 10 = 0.044
            assert abs(new_avg - 0.044) < 0.001
            break


@pytest.mark.asyncio
async def test_on_trade_closed_handles_missing_trade(db, settings):
    """Should return gracefully if trade not found."""
    db.fetchrow = AsyncMock(return_value=None)
    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(999)
    db.execute.assert_not_called()
