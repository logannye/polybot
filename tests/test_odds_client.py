import asyncio
import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.analysis.odds_client import (
    OddsClient,
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


class TestOddsClientCreditGuard:
    @pytest.mark.asyncio
    async def test_fetch_odds_stops_when_credits_exhausted(self):
        """When credits_remaining == 0, fetch_odds returns [] without HTTP call."""
        client = OddsClient(api_key="test-key")
        mock_session = MagicMock()
        mock_session.closed = False  # session is open so recreation is skipped
        client._session = mock_session
        client._credits_remaining = 0

        result = await client.fetch_odds("basketball_nba")

        assert result == []
        # Verify no HTTP request was made
        mock_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_odds_stops_below_reserve(self):
        """When credits_remaining (5) is below credit_reserve (10), returns [] without HTTP call."""
        client = OddsClient(api_key="test-key", credit_reserve=10)
        mock_session = MagicMock()
        mock_session.closed = False  # session is open so recreation is skipped
        client._session = mock_session
        client._credits_remaining = 5

        result = await client.fetch_odds("basketball_nba")

        assert result == []
        mock_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_all_sports_short_circuits_on_zero_credits(self):
        """When credits are already 0, fetch_all_sports should return [] immediately."""
        client = OddsClient(api_key="test-key", sports=["nba", "nhl", "epl"])
        client._session = MagicMock()
        client._credits_remaining = 0

        with patch.object(client, 'fetch_odds', new_callable=AsyncMock) as mock_fetch:
            result = await client.fetch_all_sports()

        assert result == []
        mock_fetch.assert_not_called()  # should short-circuit before calling fetch_odds


class TestOddsClientSessionRecovery:
    @pytest.mark.asyncio
    async def test_odds_client_recreates_closed_session(self):
        """When the session is closed, fetch_odds should recreate it and use the new one."""
        client = OddsClient(api_key="test-key")
        old_session = MagicMock()
        old_session.closed = True
        client._session = old_session

        new_session = MagicMock()
        # Make the new session's get() return a valid async context manager
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {}
        mock_resp.json = AsyncMock(return_value=[])
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        new_session.get = MagicMock(return_value=mock_resp)

        with patch("aiohttp.ClientSession", return_value=new_session):
            await client.fetch_odds("basketball_nba")

        # The old closed session must not be used
        old_session.get.assert_not_called()
        # The new session must have been used
        new_session.get.assert_called_once()
        assert client._session is new_session


class TestOddsClientFetchAllTimeout:
    @pytest.mark.asyncio
    async def test_fetch_all_sports_has_overall_timeout(self):
        """fetch_all_sports must complete within ~31s even if individual fetches hang."""
        client = OddsClient(api_key="test-key", sports=["sport_a", "sport_b", "sport_c"],
                            fetch_timeout=0.1)
        client._session = MagicMock()
        client._session.closed = False

        async def slow_fetch(sport_key: str) -> list:
            await asyncio.sleep(60)  # would hang forever without overall timeout
            return []

        with patch.object(client, "fetch_odds", side_effect=slow_fetch):
            result = await client.fetch_all_sports()

        # Should return whatever was collected before the timeout (empty here)
        assert isinstance(result, list)


class TestOddsClientCreditsExhausted:
    def test_exhausted_when_zero(self):
        client = OddsClient(api_key="test", sports=[])
        client._credits_remaining = 0
        assert client.credits_exhausted is True

    def test_exhausted_when_at_reserve(self):
        client = OddsClient(api_key="test", sports=[], credit_reserve=10)
        client._credits_remaining = 10
        assert client.credits_exhausted is True

    def test_not_exhausted_when_above_reserve(self):
        client = OddsClient(api_key="test", sports=[], credit_reserve=10)
        client._credits_remaining = 50
        assert client.credits_exhausted is False

    def test_not_exhausted_when_unknown(self):
        """Before any API call, credits_remaining is None — should not be treated as exhausted."""
        client = OddsClient(api_key="test", sports=[])
        assert client.credits_exhausted is False
