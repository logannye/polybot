import pytest
from polybot.analysis.win_probability import compute_win_probability


class TestNBAWinProbability:
    def test_blowout_q4(self):
        """Up 20 in Q4 -> near-certain win."""
        wp = compute_win_probability(sport="nba", lead=20, period=4, total_periods=4)
        assert wp >= 0.97

    def test_close_game_q4(self):
        """Up 2 in Q4 -> slight favorite."""
        wp = compute_win_probability(sport="nba", lead=2, period=4, total_periods=4)
        assert 0.55 <= wp <= 0.75

    def test_tied_game(self):
        wp = compute_win_probability(sport="nba", lead=0, period=2, total_periods=4)
        assert 0.45 <= wp <= 0.55

    def test_trailing(self):
        wp = compute_win_probability(sport="nba", lead=-10, period=3, total_periods=4)
        assert wp < 0.35

    def test_halftime_big_lead(self):
        wp = compute_win_probability(sport="nba", lead=15, period=2, total_periods=4)
        assert 0.80 <= wp <= 0.95


class TestMLBWinProbability:
    def test_blowout_late(self):
        wp = compute_win_probability(sport="mlb", lead=5, period=8, total_periods=9)
        assert wp >= 0.96

    def test_one_run_lead_9th(self):
        wp = compute_win_probability(sport="mlb", lead=1, period=9, total_periods=9)
        assert 0.80 <= wp <= 0.95

    def test_early_game(self):
        wp = compute_win_probability(sport="mlb", lead=3, period=3, total_periods=9)
        assert 0.60 <= wp <= 0.80


class TestNHLWinProbability:
    def test_two_goal_lead_3rd(self):
        wp = compute_win_probability(sport="nhl", lead=2, period=3, total_periods=3)
        assert wp >= 0.90

    def test_one_goal_lead_3rd(self):
        wp = compute_win_probability(sport="nhl", lead=1, period=3, total_periods=3)
        assert 0.75 <= wp <= 0.92


class TestEdgeCases:
    def test_completed_game_winner(self):
        wp = compute_win_probability(sport="nba", lead=10, period=4, total_periods=4, completed=True)
        assert wp == 1.0

    def test_completed_game_loser(self):
        wp = compute_win_probability(sport="nba", lead=-5, period=4, total_periods=4, completed=True)
        assert wp == 0.0

    def test_unknown_sport_returns_none(self):
        wp = compute_win_probability(sport="curling", lead=3, period=5, total_periods=10)
        assert wp is None
