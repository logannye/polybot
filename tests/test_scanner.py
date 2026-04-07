import aiohttp
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from polybot.markets.scanner import PolymarketScanner, parse_market_response, parse_gamma_market


class TestParseMarketResponse:
    def test_parses_valid_market(self):
        raw = {
            "condition_id": "0xabc123", "question": "Will BTC hit $100K by April?",
            "tokens": [{"token_id": "tok_yes", "outcome": "Yes", "price": 0.65}, {"token_id": "tok_no", "outcome": "No", "price": 0.35}],
            "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            "volume": 50000.0, "active": True, "closed": False, "category": "Crypto",
        }
        result = parse_market_response(raw)
        assert result is not None
        assert result["polymarket_id"] == "0xabc123"
        assert result["question"] == "Will BTC hit $100K by April?"
        assert result["yes_price"] == pytest.approx(0.65)
        assert result["category"] == "Crypto"

    def test_skips_closed_market(self):
        raw = {
            "condition_id": "0xabc123", "question": "Old question?",
            "tokens": [{"token_id": "tok_yes", "outcome": "Yes", "price": 0.65}, {"token_id": "tok_no", "outcome": "No", "price": 0.35}],
            "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            "volume": 50000.0, "active": False, "closed": True, "category": "Crypto",
        }
        assert parse_market_response(raw) is None

    def test_skips_missing_tokens(self):
        raw = {
            "condition_id": "0xabc123", "question": "Bad market?", "tokens": [],
            "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
            "volume": 50000.0, "active": True, "closed": False, "category": "Other",
        }
        assert parse_market_response(raw) is None


class TestPolymarketScanner:
    @pytest.fixture
    def scanner(self):
        return PolymarketScanner(api_key="test_key", base_url="https://clob.polymarket.com")

    @pytest.mark.asyncio
    async def test_fetch_markets_returns_parsed(self, scanner):
        gamma_response = [{
            "conditionId": "0x111", "question": "Test market?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.50", "0.50"]',
            "clobTokenIds": '["t1", "t2"]',
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "volume24hr": 10000.0, "liquidityNum": 5000.0,
            "active": True, "closed": False, "slug": "test-market",
        }]
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=gamma_response)
        mock_resp.status = 200
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        scanner._session = mock_session
        markets = await scanner.fetch_markets()
        assert len(markets) == 1
        assert markets[0]["polymarket_id"] == "0x111"

    def test_parse_gamma_market_valid(self):
        raw = {
            "conditionId": "0xabc", "question": "Will X happen?",
            "outcomes": '["Yes", "No"]', "outcomePrices": '["0.65", "0.35"]',
            "clobTokenIds": '["tok1", "tok2"]',
            "endDate": "2026-04-15T00:00:00Z", "volume24hr": 5000,
            "liquidityNum": 3000, "active": True, "closed": False,
            "slug": "will-x-happen",
        }
        result = parse_gamma_market(raw)
        assert result is not None
        assert result["polymarket_id"] == "0xabc"
        assert result["yes_price"] == pytest.approx(0.65)
        assert result["book_depth"] == pytest.approx(3000)

    def test_parse_gamma_market_rejects_closed(self):
        raw = {"conditionId": "0x1", "outcomes": '["Y","N"]',
               "outcomePrices": '["0.5","0.5"]', "clobTokenIds": '["a","b"]',
               "endDate": "2026-05-01T00:00:00Z", "active": True, "closed": True}
        assert parse_gamma_market(raw) is None

    @pytest.mark.asyncio
    async def test_fetch_market_resolution_resolved_yes(self, scanner):
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"resolved": True, "outcome": "Yes"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)
        scanner._session = mock_session
        result = await scanner.fetch_market_resolution("test-id")
        assert result == 1

    @pytest.mark.asyncio
    async def test_fetch_market_resolution_unresolved(self, scanner):
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"resolved": False})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)
        scanner._session = mock_session
        result = await scanner.fetch_market_resolution("test-id")
        assert result is None

    def test_fetch_grouped_markets_valid_exhaustive(self):
        """Valid exhaustive group: same slug, similar resolution, common question prefix, sum ~1.0."""
        res = datetime(2026, 5, 1, tzinfo=timezone.utc)
        markets = [
            {"polymarket_id": "a", "group_slug": "who-wins-election",
             "yes_price": 0.40, "question": "Who will win the 2026 election? — Alice",
             "resolution_time": res},
            {"polymarket_id": "b", "group_slug": "who-wins-election",
             "yes_price": 0.35, "question": "Who will win the 2026 election? — Bob",
             "resolution_time": res},
            {"polymarket_id": "c", "group_slug": "who-wins-election",
             "yes_price": 0.25, "question": "Who will win the 2026 election? — Carol",
             "resolution_time": res},
            {"polymarket_id": "d", "group_slug": None, "yes_price": 0.50,
             "question": "Unrelated?", "resolution_time": res},
        ]
        groups = PolymarketScanner.fetch_grouped_markets(markets)
        assert "who-wins-election" in groups
        assert len(groups["who-wins-election"]) == 3

    def test_fetch_grouped_markets_rejects_bad_sum(self):
        """Group with yes_sum=1.5 is NOT exhaustive -> rejected."""
        res = datetime(2026, 5, 1, tzinfo=timezone.utc)
        markets = [
            {"polymarket_id": "a", "group_slug": "bad-group",
             "yes_price": 0.80, "question": "Will X happen by June?",
             "resolution_time": res},
            {"polymarket_id": "b", "group_slug": "bad-group",
             "yes_price": 0.70, "question": "Will Y happen by June?",
             "resolution_time": res},
        ]
        groups = PolymarketScanner.fetch_grouped_markets(markets)
        assert "bad-group" not in groups

    def test_fetch_grouped_markets_rejects_different_resolution(self):
        """Group with different resolution times -> rejected."""
        markets = [
            {"polymarket_id": "a", "group_slug": "split-res",
             "yes_price": 0.50, "question": "Will Alice win the race?",
             "resolution_time": datetime(2026, 5, 1, tzinfo=timezone.utc)},
            {"polymarket_id": "b", "group_slug": "split-res",
             "yes_price": 0.50, "question": "Will Bob win the race?",
             "resolution_time": datetime(2026, 6, 1, tzinfo=timezone.utc)},
        ]
        groups = PolymarketScanner.fetch_grouped_markets(markets)
        assert "split-res" not in groups

    def test_fetch_grouped_markets_rejects_unrelated_questions(self):
        """Group with no common question prefix -> rejected."""
        res = datetime(2026, 5, 1, tzinfo=timezone.utc)
        markets = [
            {"polymarket_id": "a", "group_slug": "cosmetic-group",
             "yes_price": 0.50, "question": "Will Bitcoin hit $100K?",
             "resolution_time": res},
            {"polymarket_id": "b", "group_slug": "cosmetic-group",
             "yes_price": 0.50, "question": "Will the Lakers win the championship?",
             "resolution_time": res},
        ]
        groups = PolymarketScanner.fetch_grouped_markets(markets)
        assert "cosmetic-group" not in groups

    def test_validate_exhaustive_group_single_market(self):
        """A single market cannot be an exhaustive group."""
        assert PolymarketScanner.validate_exhaustive_group([
            {"yes_price": 0.50, "question": "Test?",
             "resolution_time": datetime(2026, 5, 1, tzinfo=timezone.utc)}
        ]) is False

    def test_parse_market_response_preserves_group_slug(self):
        raw = {
            "active": True, "closed": False,
            "condition_id": "test-cond",
            "question": "Test?",
            "category": "politics",
            "end_date_iso": "2026-04-01T00:00:00Z",
            "tokens": [
                {"outcome": "Yes", "price": "0.55", "token_id": "tok_yes"},
                {"outcome": "No", "price": "0.45", "token_id": "tok_no"},
            ],
            "volume": "1000",
            "group_slug": "my-group",
        }
        result = parse_market_response(raw)
        assert result is not None
        assert result["group_slug"] == "my-group"

    @pytest.mark.asyncio
    async def test_fetch_markets_returns_empty_on_client_connector_error(self, scanner):
        """DNS failure (ClientConnectorError) should return [] not propagate."""
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("DNS resolution failed")))
        scanner._session = mock_session
        markets = await scanner.fetch_markets()
        assert markets == []

    @pytest.mark.asyncio
    async def test_fetch_markets_recreates_closed_session(self, scanner):
        """If session is closed, fetch_markets should recreate it before fetching."""
        gamma_response = [{
            "conditionId": "0x222", "question": "Recreated session market?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.60", "0.40"]',
            "clobTokenIds": '["t3", "t4"]',
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "volume24hr": 8000.0, "liquidityNum": 4000.0,
            "active": True, "closed": False, "slug": "recreated-session-market",
        }]
        # Set up a closed session that should be replaced
        closed_session = MagicMock()
        closed_session.closed = True
        scanner._session = closed_session

        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=gamma_response)
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        new_mock_session = MagicMock()
        new_mock_session.closed = False
        new_mock_session.get = MagicMock(return_value=mock_resp)

        with patch("polybot.markets.scanner.aiohttp.ClientSession", return_value=new_mock_session):
            markets = await scanner.fetch_markets()

        # Session should have been recreated (old closed session replaced)
        assert scanner._session is new_mock_session
        assert len(markets) == 1
        assert markets[0]["polymarket_id"] == "0x222"
