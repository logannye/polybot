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
        mock_resp.text = AsyncMock(return_value="rate limited")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)
        researcher._session = mock_session
        result = await researcher.search("test query")
        assert result == "No recent search results found."

    @pytest.mark.asyncio
    async def test_search_retries_without_freshness(self, researcher):
        """If first call (with freshness=pd) fails, retry without freshness."""
        mock_session = AsyncMock()
        call_count = 0

        mock_fail_resp = AsyncMock()
        mock_fail_resp.status = 422
        mock_fail_resp.text = AsyncMock(return_value="invalid param")
        mock_fail_resp.__aenter__ = AsyncMock(return_value=mock_fail_resp)
        mock_fail_resp.__aexit__ = AsyncMock(return_value=False)

        mock_ok_resp = AsyncMock()
        mock_ok_resp.status = 200
        mock_ok_resp.json = AsyncMock(return_value={
            "web": {"results": [{"title": "Retry worked", "url": "https://t.co", "description": "ok"}]}
        })
        mock_ok_resp.__aenter__ = AsyncMock(return_value=mock_ok_resp)
        mock_ok_resp.__aexit__ = AsyncMock(return_value=False)

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_fail_resp
            return mock_ok_resp

        mock_session.get = side_effect
        researcher._session = mock_session
        result = await researcher.search("test query")
        assert "Retry worked" in result
        assert call_count == 2
