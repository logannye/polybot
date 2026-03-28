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

    async def search(self, query: str, max_results: int = 5) -> str:
        try:
            cm = self._session.get(BRAVE_API_URL, params={"q": query, "count": max_results, "freshness": "pd"})
            if hasattr(cm, "__aenter__"):
                async with cm as resp:
                    if resp.status != 200:
                        log.warning("brave_search_error", status=resp.status)
                        return "No recent search results found."
                    data = await resp.json()
            else:
                resp = await cm
                async with resp as r:
                    if r.status != 200:
                        log.warning("brave_search_error", status=r.status)
                        return "No recent search results found."
                    data = await r.json()
            return format_search_results(data.get("web", {}).get("results", []), max_results)
        except Exception as e:
            log.error("brave_search_exception", error=str(e))
            return "No recent search results found."
