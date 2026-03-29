import aiohttp
import structlog
from datetime import datetime, timezone
from typing import Any

log = structlog.get_logger()
CLOB_BASE_URL = "https://clob.polymarket.com"


def parse_market_response(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not raw.get("active") or raw.get("closed"):
        return None
    tokens = raw.get("tokens", [])
    if len(tokens) < 2:
        return None
    yes_token = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), None)
    no_token = next((t for t in tokens if t.get("outcome", "").lower() == "no"), None)
    if not yes_token or not no_token:
        return None
    try:
        end_date = datetime.fromisoformat(raw["end_date_iso"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return {
        "polymarket_id": raw["condition_id"], "question": raw.get("question", ""),
        "category": raw.get("category", "unknown"), "resolution_time": end_date,
        "yes_price": float(yes_token["price"]), "no_price": float(no_token["price"]),
        "yes_token_id": yes_token["token_id"], "no_token_id": no_token["token_id"],
        "volume_24h": float(raw.get("volume", 0)),
        "group_slug": raw.get("group_slug"),
    }


class PolymarketScanner:
    def __init__(self, api_key: str, base_url: str = CLOB_BASE_URL):
        self._api_key = api_key
        self._base_url = base_url
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self._api_key}"})

    async def close(self):
        if self._session:
            await self._session.close()

    async def _get(self, url: str, params: dict[str, Any]) -> tuple[int, Any]:
        cm = self._session.get(url, params=params)
        # Support both direct context managers (real aiohttp) and
        # AsyncMock patterns where .get() is awaitable and returns a CM.
        if hasattr(cm, "__aenter__"):
            async with cm as resp:
                status = resp.status
                data = await resp.json() if status == 200 else None
                return status, data
        else:
            resp = await cm
            async with resp as r:
                status = r.status
                data = await r.json() if status == 200 else None
                return status, data

    async def fetch_markets(self) -> list[dict[str, Any]]:
        markets = []
        next_cursor = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if next_cursor:
                params["next_cursor"] = next_cursor
            status, data = await self._get(f"{self._base_url}/markets", params)
            if status != 200:
                log.error("scanner_api_error", status=status)
                break
            for raw_market in data.get("data", []):
                parsed = parse_market_response(raw_market)
                if parsed:
                    markets.append(parsed)
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
        log.info("scanner_fetched", count=len(markets))
        return markets

    async def fetch_order_book(self, token_id: str) -> dict[str, Any]:
        status, data = await self._get(f"{self._base_url}/book", {"token_id": token_id})
        if status != 200:
            return {"bids": [], "asks": []}
        return data

    async def fetch_price_history(self, token_id: str, interval: str = "1h") -> list[float]:
        status, data = await self._get(
            f"{self._base_url}/prices-history",
            {"token_id": token_id, "interval": interval, "fidelity": 60},
        )
        if status != 200:
            return []
        return [float(p.get("p", 0)) for p in data.get("history", [])]

    async def fetch_market_resolution(self, condition_id: str) -> int | None:
        status, data = await self._get(f"{self._base_url}/markets/{condition_id}", {})
        if status != 200 or not data:
            return None
        if not data.get("resolved", False):
            return None
        outcome = data.get("outcome", "").lower()
        if outcome in ("yes", "true", "1"):
            return 1
        elif outcome in ("no", "false", "0"):
            return 0
        return None

    @staticmethod
    def fetch_grouped_markets(markets: list[dict]) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for m in markets:
            slug = m.get("group_slug")
            if slug:
                groups.setdefault(slug, []).append(m)
        return {k: v for k, v in groups.items() if len(v) >= 2}
