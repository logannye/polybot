import pytest
from polybot.analysis.odds_client import (
    american_to_prob, devig, compute_consensus,
    find_polymarket_prices, find_divergences,
)


class TestAmericanToProb:
    def test_negative_odds(self):
        assert american_to_prob(-150) == pytest.approx(0.60, abs=0.001)

    def test_positive_odds(self):
        assert american_to_prob(200) == pytest.approx(0.333, abs=0.001)

    def test_even_odds(self):
        assert american_to_prob(100) == pytest.approx(0.50, abs=0.001)

    def test_heavy_favorite(self):
        assert american_to_prob(-500) == pytest.approx(0.833, abs=0.001)

    def test_heavy_underdog(self):
        assert american_to_prob(500) == pytest.approx(0.167, abs=0.001)


class TestDevig:
    def test_removes_standard_vig(self):
        p_a = american_to_prob(-110)
        p_b = american_to_prob(-110)
        fair_a, fair_b = devig(p_a, p_b)
        assert fair_a == pytest.approx(0.50, abs=0.01)
        assert fair_b == pytest.approx(0.50, abs=0.01)
        assert fair_a + fair_b == pytest.approx(1.0, abs=0.001)

    def test_asymmetric_vig(self):
        p_a = american_to_prob(-200)
        p_b = american_to_prob(170)
        fair_a, fair_b = devig(p_a, p_b)
        assert fair_a + fair_b == pytest.approx(1.0, abs=0.001)
        assert fair_a > 0.60


class TestComputeConsensus:
    def test_averages_across_books(self):
        bookmakers = [
            {
                "key": "fanduel",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -150},
                    {"name": "Team B", "price": 130},
                ]}],
            },
            {
                "key": "draftkings",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -160},
                    {"name": "Team B", "price": 140},
                ]}],
            },
        ]
        result = compute_consensus(bookmakers)
        assert result is not None
        assert "Team A" in result
        assert "Team B" in result
        assert result["Team A"] + result["Team B"] == pytest.approx(1.0, abs=0.02)
        assert result["Team A"] > 0.55

    def test_ignores_non_consensus_books(self):
        bookmakers = [
            {
                "key": "polymarket",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -200},
                    {"name": "Team B", "price": 170},
                ]}],
            },
        ]
        result = compute_consensus(bookmakers)
        assert result is None

    def test_returns_none_with_no_data(self):
        assert compute_consensus([]) is None


class TestFindDivergences:
    def test_detects_underpriced_polymarket(self):
        events = [{
            "id": "evt1", "sport_key": "basketball_nba",
            "home_team": "Warriors", "away_team": "Lakers",
            "commence_time": "2026-04-05T01:00:00Z",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Warriors", "price": -200},
                        {"name": "Lakers", "price": 170},
                    ]}],
                },
                {
                    "key": "draftkings",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Warriors", "price": -190},
                        {"name": "Lakers", "price": 165},
                    ]}],
                },
                {
                    "key": "polymarket",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Warriors", "price": -130},
                        {"name": "Lakers", "price": 110},
                    ]}],
                },
            ],
        }]
        divs = find_divergences(events, min_divergence=0.03)
        assert len(divs) >= 1
        warriors_div = next(d for d in divs if d["outcome_name"] == "Warriors")
        assert warriors_div["side"] == "YES"
        assert warriors_div["divergence"] > 0.03

    def test_ignores_small_divergence(self):
        events = [{
            "id": "evt2", "sport_key": "basketball_nba",
            "home_team": "A", "away_team": "B",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": -150},
                        {"name": "B", "price": 130},
                    ]}],
                },
                {
                    "key": "polymarket",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": -145},
                        {"name": "B", "price": 125},
                    ]}],
                },
            ],
        }]
        divs = find_divergences(events, min_divergence=0.03)
        assert len(divs) == 0

    def test_skips_events_without_polymarket(self):
        events = [{
            "id": "evt3", "sport_key": "basketball_nba",
            "home_team": "A", "away_team": "B",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": -200},
                        {"name": "B", "price": 170},
                    ]}],
                },
            ],
        }]
        divs = find_divergences(events, min_divergence=0.03)
        assert len(divs) == 0
