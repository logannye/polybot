import aiohttp
import structlog

log = structlog.get_logger()
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


def format_search_results(results: list[dict], max_results: int = 5) -> str:
    if not results:
        return "No recent search results found."
    lines = []
    for r in results[:max_results]:
        lines.append(f"- {r.get('title', '')}\n  URL: {r.get('url', '')}\n  {r.get('description', '')}")
    return "\n\n".join(lines)


class BraveResearcher:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession(headers={"X-Subscription-Token": self._api_key})

    async def close(self):
        if self._session:
            await self._session.close()

    async def _do_search(self, params: dict) -> list[dict] | None:
        """Execute a single Brave search request. Returns results list or None on failure."""
        cm = self._session.get(BRAVE_API_URL, params=params)
        if hasattr(cm, "__aenter__"):
            async with cm as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("brave_search_error", status=resp.status, body=body[:500])
                    return None
                data = await resp.json()
        else:
            resp = await cm
            async with resp as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("brave_search_error", status=r.status, body=body[:500])
                    return None
                data = await r.json()
        return data.get("web", {}).get("results", [])

    async def search(self, query: str, max_results: int = 5) -> str:
        # Try with freshness filter first, then fall back to no filter
        for params in [
            {"q": query, "count": max_results, "freshness": "pd"},
            {"q": query, "count": max_results},
        ]:
            try:
                results = await self._do_search(params)
                if results is not None:
                    return format_search_results(results, max_results)
            except Exception as e:
                log.error("brave_search_exception", error=str(e))
        return "No recent search results found."
