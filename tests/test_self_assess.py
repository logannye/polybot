import pytest
from polybot.learning.self_assess import suggest_kelly_adjustment, suggest_edge_threshold, check_strategy_kill_switch


class TestKellyAdjustment:
    def test_low_volatility_increase(self):
        new_mult = suggest_kelly_adjustment(current_mult=0.25, max_drawdown_pct=0.10, drawdown_tolerance=0.30)
        assert new_mult > 0.25
        assert new_mult <= 0.30

    def test_high_volatility_decrease(self):
        new_mult = suggest_kelly_adjustment(current_mult=0.25, max_drawdown_pct=0.35, drawdown_tolerance=0.30)
        assert new_mult < 0.25
        assert new_mult >= 0.20

    def test_within_tolerance_no_change(self):
        new_mult = suggest_kelly_adjustment(current_mult=0.25, max_drawdown_pct=0.25, drawdown_tolerance=0.30)
        assert abs(new_mult - 0.25) < 0.03


class TestEdgeThreshold:
    def test_marginal_trades_losing_increase(self):
        edge_buckets = {0.05: {"count": 20, "total_pnl": -30.0}, 0.10: {"count": 15, "total_pnl": 50.0}, 0.15: {"count": 10, "total_pnl": 60.0}}
        new_threshold = suggest_edge_threshold(current_threshold=0.05, edge_buckets=edge_buckets)
        assert new_threshold > 0.05

    def test_all_buckets_profitable_keep(self):
        edge_buckets = {0.05: {"count": 20, "total_pnl": 10.0}, 0.10: {"count": 15, "total_pnl": 50.0}, 0.15: {"count": 10, "total_pnl": 60.0}}
        new_threshold = suggest_edge_threshold(current_threshold=0.05, edge_buckets=edge_buckets)
        assert new_threshold <= 0.05

    def test_empty_buckets_no_change(self):
        assert suggest_edge_threshold(current_threshold=0.05, edge_buckets={}) == 0.05


class TestStrategyKillSwitch:
    def test_strategy_kill_switch_disables_losing(self):
        assert check_strategy_kill_switch(total_trades=60, total_pnl=-15.0, min_trades=50) is True

    def test_strategy_kill_switch_spares_profitable(self):
        assert check_strategy_kill_switch(total_trades=60, total_pnl=10.0, min_trades=50) is False

    def test_strategy_kill_switch_spares_insufficient_data(self):
        assert check_strategy_kill_switch(total_trades=20, total_pnl=-15.0, min_trades=50) is False
