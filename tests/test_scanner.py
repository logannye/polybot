import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone, timedelta
from polybot.markets.scanner import PolymarketScanner, parse_market_response


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
        mock_response = {
            "data": [{
                "condition_id": "0x111", "question": "Test market?",
                "tokens": [{"token_id": "t1", "outcome": "Yes", "price": 0.50}, {"token_id": "t2", "outcome": "No", "price": 0.50}],
                "end_date_iso": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "volume": 10000.0, "active": True, "closed": False, "category": "Politics",
            }],
            "next_cursor": None,
        }
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value=mock_response)
        mock_resp.status = 200
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        scanner._session = mock_session
        markets = await scanner.fetch_markets()
        assert len(markets) == 1
        assert markets[0]["polymarket_id"] == "0x111"
