import pytest
from polybot.markets.websocket import PositionTracker, should_early_exit, should_stop_loss


class TestShouldEarlyExit:
    def test_exit_when_edge_evaporated(self):
        assert should_early_exit(
            entry_price=0.50,
            current_price=0.58,
            side="YES",
            ensemble_prob=0.60,
            early_exit_edge=0.02,
        ) is True

    def test_no_exit_with_edge(self):
        assert should_early_exit(
            entry_price=0.50,
            current_price=0.52,
            side="YES",
            ensemble_prob=0.60,
            early_exit_edge=0.02,
        ) is False


class TestShouldStopLoss:
    def test_stop_when_price_moved_against(self):
        assert should_stop_loss(
            entry_price=0.50,
            current_price=0.35,
            side="YES",
        ) is True

    def test_no_stop_in_profit(self):
        assert should_stop_loss(
            entry_price=0.50,
            current_price=0.55,
            side="YES",
        ) is False

    def test_stop_no_side(self):
        assert should_stop_loss(
            entry_price=0.50,
            current_price=0.65,
            side="NO",
        ) is True
