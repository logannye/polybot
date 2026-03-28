import pytest
from unittest.mock import AsyncMock
from polybot.analysis.research import BraveResearcher, format_search_results


class TestFormatSearchResults:
    def test_formats_results(self):
        raw = [{"title": "BTC hits 95K", "url": "https://example.com/1", "description": "Bitcoin surged today."},
               {"title": "Market analysis", "url": "https://example.com/2", "description": "Analysts predict..."}]
        formatted = format_search_results(raw)
        assert "BTC hits 95K" in formatted
        assert "https://example.com/1" in formatted

    def test_empty_results(self):
        assert format_search_results([]) == "No recent search results found."

    def test_truncates_to_max(self):
        raw = [{"title": f"Result {i}", "url": f"https://example.com/{i}", "description": f"Desc {i}"} for i in range(10)]
        formatted = format_search_results(raw, max_results=5)
        assert formatted.count("Result ") == 5


class TestBraveResearcher:
    @pytest.fixture
    def researcher(self):
        return BraveResearcher(api_key="test_key")

    @pytest.mark.asyncio
    async def test_search_returns_formatted(self, researcher):
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"web": {"results": [{"title": "Test", "url": "https://t.co", "description": "Test desc"}]}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)
        researcher._session = mock_session
        result = await researcher.search("Will BTC hit 100K?")
        assert "Test" in result

    @pytest.mark.asyncio
    async def test_search_handles_api_error(self, researcher):
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)
        researcher._session = mock_session
        result = await researcher.search("test query")
        assert result == "No recent search results found."
