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


@pytest.mark.asyncio
async def test_proxy_trust_updates_on_take_profit(db, settings):
    """Take-profit should update model Brier scores with proxy outcome aligned to bet side."""
    now = datetime.now(timezone.utc)
    db.fetchrow = AsyncMock(side_effect=[
        # Trade lookup
        {"id": 5, "analysis_id": 15, "pnl": 3.00, "strategy": "forecast",
         "exit_reason": "take_profit", "market_id": 8, "side": "YES",
         "opened_at": now - timedelta(minutes=20), "closed_at": now},
        # Analysis lookup
        {"edge": 0.06, "model_estimates": json.dumps([
            {"model": "claude-sonnet-4.6", "probability": 0.70, "confidence": "high", "reasoning": "test"},
            {"model": "gpt-4o", "probability": 0.60, "confidence": "medium", "reasoning": "test"},
        ])},
        # Market lookup
        {"category": "crypto"},
        # system_state
        {"category_scores": "{}"},
        # strategy_performance for avg_edge
        {"avg_edge": 0.04, "total_trades": 10},
        # model_performance for claude (proxy trust)
        {"model_name": "claude-sonnet-4.6", "brier_score_ema": 0.25, "resolved_count": 0},
        # model_performance for gpt (proxy trust)
        {"model_name": "gpt-4o", "brier_score_ema": 0.25, "resolved_count": 0},
        # model_performance for rebalance (fetch all)
    ])
    db.fetchval = AsyncMock(return_value="{}")
    db.fetch = AsyncMock(return_value=[
        {"model_name": "claude-sonnet-4.6", "brier_score_ema": 0.24, "trust_weight": 0.333},
        {"model_name": "gpt-4o", "brier_score_ema": 0.26, "trust_weight": 0.333},
        {"model_name": "gemini-2.5-flash", "brier_score_ema": 0.25, "trust_weight": 0.333},
    ])
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(5)

    # Verify model_performance was updated (brier_score_ema + resolved_count)
    brier_updates = [c for c in db.execute.call_args_list if "brier_score_ema" in str(c)]
    assert len(brier_updates) >= 2  # One per model


@pytest.mark.asyncio
async def test_proxy_trust_skips_resolution(db, settings):
    """Resolution trades should not trigger proxy trust updates (handled by TradeRecorder)."""
    now = datetime.now(timezone.utc)
    db.fetchrow = AsyncMock(side_effect=[
        # Trade lookup
        {"id": 6, "analysis_id": 16, "pnl": 5.00, "strategy": "forecast",
         "exit_reason": "resolution", "market_id": 9, "side": "YES",
         "opened_at": now - timedelta(hours=24), "closed_at": now},
        # Analysis lookup
        {"edge": 0.05, "model_estimates": json.dumps([
            {"model": "claude-sonnet-4.6", "probability": 0.80, "confidence": "high", "reasoning": "test"},
        ])},
        # Market lookup
        {"category": "politics"},
        # system_state
        {"category_scores": "{}"},
        # strategy_performance for avg_edge
        {"avg_edge": 0.05, "total_trades": 5},
    ])
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(6)

    # Should NOT see brier_score_ema updates (resolution is skipped for proxy)
    brier_updates = [c for c in db.execute.call_args_list if "brier_score_ema" in str(c)]
    assert len(brier_updates) == 0


@pytest.mark.asyncio
async def test_proxy_trust_skips_snipe_no_estimates(db, settings):
    """Snipe trades with no model estimates should skip proxy trust entirely."""
    now = datetime.now(timezone.utc)
    db.fetchrow = AsyncMock(side_effect=[
        # Trade lookup
        {"id": 7, "analysis_id": 17, "pnl": 0.50, "strategy": "snipe",
         "exit_reason": "early_exit", "market_id": 10, "side": "YES",
         "opened_at": now - timedelta(minutes=5), "closed_at": now},
        # Analysis lookup — empty model_estimates (snipe trades)
        {"edge": 0.03, "model_estimates": "[]"},
        # Market lookup
        {"category": "sports"},
        # system_state
        {"category_scores": "{}"},
        # strategy_performance for avg_edge
        {"avg_edge": 0.02, "total_trades": 50},
    ])
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.on_trade_closed(7)

    # Should NOT see brier_score_ema updates (no model estimates)
    brier_updates = [c for c in db.execute.call_args_list if "brier_score_ema" in str(c)]
    assert len(brier_updates) == 0


@pytest.mark.asyncio
async def test_compute_optimal_thresholds_stores_results(db, settings):
    """compute_optimal_thresholds should store learned TP/SL in learned_params."""
    # Create enough mock trades to trigger learning
    trades = []
    now = datetime.now(timezone.utc)
    for i in range(15):
        pnl = 2.0 if i % 3 == 0 else -1.0
        trades.append({
            "exit_reason": "take_profit" if pnl > 0 else "stop_loss",
            "pnl": pnl,
            "entry_price": 0.50,
            "exit_price": 0.65 if pnl > 0 else 0.38,
            "side": "YES",
            "opened_at": now - timedelta(hours=i),
            "closed_at": now - timedelta(hours=i) + timedelta(minutes=30),
        })

    db.fetch = AsyncMock(return_value=trades)
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.compute_optimal_thresholds()

    # Check that learned_params was updated for at least one strategy
    params_updates = [c for c in db.execute.call_args_list if "learned_params" in str(c)]
    assert len(params_updates) >= 1
    for call in params_updates:
        params = json.loads(call.args[1])
        assert "take_profit_threshold" in params
        assert "stop_loss_threshold" in params
        assert 0.10 <= params["take_profit_threshold"] <= 0.50
        assert 0.10 <= params["stop_loss_threshold"] <= 0.40
        assert params["threshold_sample_size"] == 15


@pytest.mark.asyncio
async def test_compute_optimal_thresholds_skips_insufficient_data(db, settings):
    """Should skip strategies with fewer than min_trades."""
    db.fetch = AsyncMock(return_value=[
        {"exit_reason": "take_profit", "pnl": 1.0, "entry_price": 0.50,
         "exit_price": 0.65, "side": "YES",
         "opened_at": datetime.now(timezone.utc), "closed_at": datetime.now(timezone.utc)},
    ])  # Only 1 trade, below threshold of 10
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.compute_optimal_thresholds()

    # No learned_params updates should occur
    params_updates = [c for c in db.execute.call_args_list if "learned_params" in str(c)]
    assert len(params_updates) == 0


@pytest.mark.asyncio
async def test_compute_snipe_params_stores_results(db, settings):
    """compute_snipe_params should store optimal_min_edge in learned_params."""
    now = datetime.now(timezone.utc)
    trades = []
    for i in range(10):
        edge = 0.03 + (i * 0.005)
        pnl = 0.50 if edge >= 0.04 else -0.20
        trades.append({
            "pnl": pnl, "entry_price": 0.95, "exit_reason": "early_exit",
            "side": "YES", "edge": edge,
            "opened_at": now - timedelta(hours=i),
            "closed_at": now - timedelta(hours=i) + timedelta(minutes=5),
        })

    db.fetch = AsyncMock(return_value=trades)
    db.fetchval = AsyncMock(return_value="{}")
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.compute_snipe_params()

    # Check that learned_params was updated
    params_updates = [c for c in db.execute.call_args_list
                      if "learned_params" in str(c) and "snipe" in str(c)]
    assert len(params_updates) >= 1
    params = json.loads(params_updates[0].args[1])
    assert "optimal_min_edge" in params
    assert 0.01 <= params["optimal_min_edge"] <= 0.10
    assert params["snipe_sample_size"] == 10


@pytest.mark.asyncio
async def test_compute_snipe_params_skips_insufficient_data(db, settings):
    """Should skip if fewer than 5 trades."""
    db.fetch = AsyncMock(return_value=[
        {"pnl": 0.5, "entry_price": 0.95, "exit_reason": "early_exit",
         "side": "YES", "edge": 0.03,
         "opened_at": datetime.now(timezone.utc),
         "closed_at": datetime.now(timezone.utc)},
    ])
    db.execute = AsyncMock()

    learner = TradeLearner(db=db, settings=settings)
    await learner.compute_snipe_params()

    params_updates = [c for c in db.execute.call_args_list if "learned_params" in str(c)]
    assert len(params_updates) == 0
