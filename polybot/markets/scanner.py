import aiohttp
import json
import structlog
from datetime import datetime, timezone
from typing import Any

log = structlog.get_logger()
CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# Recognized category tags (order matters: first match wins)
CATEGORY_TAGS = {"politics", "geopolitics", "crypto", "sports", "finance",
                 "business", "tech", "culture", "weather", "world"}


def parse_gamma_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a market from the Gamma API (active-only, server-side filtered)."""
    if not raw.get("active") or raw.get("closed"):
        return None

    outcomes_raw = raw.get("outcomes", "[]")
    prices_raw = raw.get("outcomePrices", "[]")
    token_ids_raw = raw.get("clobTokenIds", "[]")

    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    except (json.JSONDecodeError, TypeError):
        return None

    if len(outcomes) != 2 or len(prices) != 2 or len(token_ids) != 2:
        return None

    p0, p1 = float(prices[0]), float(prices[1])
    if p0 == 0 and p1 == 0:
        return None

    # Parse end date — Gamma uses endDate or endDateIso
    end_str = raw.get("endDate") or raw.get("endDateIso")
    if not end_str:
        return None
    try:
        if "T" in end_str:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            end_date = datetime.fromisoformat(end_str + "T23:59:59+00:00")
    except (ValueError, TypeError):
        return None

    # Extract event tags (deduplicated, lowercase slugs)
    tags: list[str] = []
    seen_tags: set[str] = set()
    for event in raw.get("events", []):
        for tag in event.get("tags", []):
            slug = tag.get("slug", "").lower().strip()
            if slug and slug not in seen_tags:
                tags.append(slug)
                seen_tags.add(slug)

    # Derive category from tags (first recognized tag wins)
    derived_category = "unknown"
    for t in tags:
        if t in CATEGORY_TAGS:
            derived_category = t
            break

    return {
        "polymarket_id": raw.get("conditionId", ""),
        "question": raw.get("question", ""),
        "category": derived_category if derived_category != "unknown" else (raw.get("category") or raw.get("slug", "unknown") or "unknown"),
        "tags": tags,
        "resolution_time": end_date,
        "yes_price": p0,
        "no_price": p1,
        "yes_token_id": token_ids[0],
        "no_token_id": token_ids[1],
        "outcomes": outcomes,
        "volume_24h": float(raw.get("volume24hr", 0) or 0),
        "book_depth": float(raw.get("liquidityNum", 0) or 0),
        "group_slug": raw.get("groupItemTitle") or raw.get("slug"),
    }


def parse_market_response(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a market from the CLOB API (legacy, used for order books + resolution)."""
    if not raw.get("active") or raw.get("closed"):
        return None
    tokens = raw.get("tokens", [])
    if len(tokens) < 2:
        return None
    # Accept any 2-outcome market (YES/NO, team names, Over/Under)
    t0, t1 = tokens[0], tokens[1]
    p0, p1 = float(t0.get("price", 0)), float(t1.get("price", 0))
    if p0 == 0 and p1 == 0:
        return None
    try:
        end_date_str = raw.get("end_date_iso")
        if not end_date_str:
            return None
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return {
        "polymarket_id": raw["condition_id"], "question": raw.get("question", ""),
        "category": raw.get("category", "unknown"), "resolution_time": end_date,
        "yes_price": p0, "no_price": p1,
        "yes_token_id": t0["token_id"], "no_token_id": t1["token_id"],
        "volume_24h": float(raw.get("volume", 0)),
        "group_slug": raw.get("group_slug"),
    }


class PolymarketScanner:
    def __init__(self, api_key: str, base_url: str = CLOB_BASE_URL):
        self._api_key = api_key
        self._base_url = base_url
        self._session: aiohttp.ClientSession | None = None
        self._price_cache: dict[str, dict] = {}

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._api_key}",
                     "User-Agent": "polybot/2.1"})

    async def close(self):
        if self._session:
            await self._session.close()

    async def _get(self, url: str, params: dict[str, Any]) -> tuple[int, Any]:
        cm = self._session.get(url, params=params)
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
        """Fetch active markets via Gamma API (server-side filtering, ~3000 markets)."""
        if self._session is None or self._session.closed:
            log.warning("scanner_session_recreated")
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    pass
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._api_key}",
                         "User-Agent": "polybot/2.1"})
        markets = []
        offset = 0
        try:
            while offset < 5000:  # safety cap
                status, data = await self._get(
                    f"{GAMMA_BASE_URL}/markets",
                    {"limit": 100, "offset": offset, "active": "true", "closed": "false"})
                if status != 200 or not data:
                    if status != 200:
                        log.error("gamma_api_error", status=status)
                    break
                for raw in data:
                    parsed = parse_gamma_market(raw)
                    if parsed:
                        markets.append(parsed)
                if len(data) < 100:
                    break
                offset += 100
        except aiohttp.ClientError as e:
            log.error("scanner_client_error", error=str(e))
            return []
        self._price_cache = {m["polymarket_id"]: m for m in markets}

        # Enrich markets with event tags (the /markets endpoint doesn't include them)
        await self._enrich_event_tags(markets)

        log.info("scanner_fetched", count=len(markets), source="gamma")
        return markets

    async def _enrich_event_tags(self, markets: list[dict[str, Any]]) -> None:
        """Fetch events and attach their tags to markets by conditionId.

        The Gamma /markets endpoint returns events without tags.
        The /events endpoint includes tags. We fetch events in bulk
        and build a conditionId→tags lookup to enrich the market data.
        """
        cid_to_tags: dict[str, list[str]] = {}
        cid_to_event_slug: dict[str, str] = {}
        offset = 0
        try:
            while offset < 5000:
                status, data = await self._get(
                    f"{GAMMA_BASE_URL}/events",
                    {"limit": 100, "offset": offset, "active": "true", "closed": "false"})
                if status != 200 or not data:
                    break
                for event in data:
                    tags_raw = event.get("tags", [])
                    seen: set[str] = set()
                    tag_slugs: list[str] = []
                    for t in tags_raw:
                        slug = t.get("slug", "").lower().strip()
                        if slug and slug not in seen:
                            tag_slugs.append(slug)
                            seen.add(slug)
                    event_slug = event.get("slug", "")
                    # Map each child market's conditionId to these tags and event slug
                    for child in event.get("markets", []):
                        cid = child.get("conditionId", "")
                        if cid:
                            cid_to_tags[cid] = tag_slugs
                            cid_to_event_slug[cid] = event_slug  # NEW
                if len(data) < 100:
                    break
                offset += 100
        except aiohttp.ClientError as e:
            log.warning("scanner_enrich_client_error", error=str(e))
            # Markets list is intact, just won't have tags enriched

        # Apply tags and event_slug to cached markets
        enriched = 0
        for m in markets:
            tags = cid_to_tags.get(m["polymarket_id"], [])
            if tags:
                m["tags"] = tags
                enriched += 1
                # Re-derive category from tags
                for t in tags:
                    if t in CATEGORY_TAGS:
                        m["category"] = t
                        break
            m["event_slug"] = cid_to_event_slug.get(m["polymarket_id"], "")
            # Update price cache too
            if m["polymarket_id"] in self._price_cache:
                self._price_cache[m["polymarket_id"]] = m
        if enriched:
            log.info("event_tags_enriched", markets=enriched, events=len(cid_to_tags))

    def get_cached_price(self, polymarket_id: str) -> dict | None:
        return self._price_cache.get(polymarket_id)

    def get_all_cached_prices(self) -> dict[str, dict]:
        return self._price_cache

    async def fetch_order_book(self, token_id: str) -> dict[str, Any]:
        status, data = await self._get(f"{self._base_url}/book", {"token_id": token_id})
        if status != 200:
            return {"bids": [], "asks": []}
        return data

    async def fetch_price_history(self, token_id: str, interval: str = "1h") -> list[float]:
        status, data = await self._get(
            f"{self._base_url}/prices-history",
            {"market": token_id, "interval": interval, "fidelity": 1},
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
    def validate_exhaustive_group(markets: list[dict]) -> bool:
        """Validate that a group is likely mutually exclusive + collectively exhaustive.

        Checks:
        1. yes_sum in tight band around 1.0 (0.85-1.15)
        2. All markets share the same resolution_time (within 1h tolerance)
        3. Common question prefix (at least 40% of shortest question)
        """
        if len(markets) < 2:
            return False

        # Check 1: Probability sum near 1.0
        yes_sum = sum(m.get("yes_price", 0) for m in markets)
        if yes_sum < 0.85 or yes_sum > 1.15:
            return False

        # Check 2: Same resolution time (within 1 hour)
        res_times = [m.get("resolution_time") for m in markets if m.get("resolution_time")]
        if len(res_times) != len(markets):
            return False
        min_res = min(res_times)
        max_res = max(res_times)
        if (max_res - min_res).total_seconds() > 3600:
            return False

        # Check 3: Common question prefix
        questions = [m.get("question", "") for m in markets]
        if not all(questions):
            return False
        prefix = questions[0]
        for q in questions[1:]:
            while not q.startswith(prefix) and prefix:
                prefix = prefix[:-1]
        min_len = min(len(q) for q in questions)
        if min_len == 0 or len(prefix) / min_len < 0.4:
            return False

        return True

    @staticmethod
    def fetch_grouped_markets(markets: list[dict]) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        for m in markets:
            slug = m.get("group_slug")
            if slug:
                groups.setdefault(slug, []).append(m)
        # Require at least 2 markets AND passing exhaustive validation
        return {k: v for k, v in groups.items()
                if len(v) >= 2 and PolymarketScanner.validate_exhaustive_group(v)}

    def fetch_event_groups(self, markets: list[dict] | None = None) -> dict[str, list[dict]]:
        """Group markets by parent event slug and validate as exhaustive.

        Uses event_slug (set during enrichment) instead of group_slug.
        This finds ~400+ multi-outcome events vs ~3 from slug-based grouping.
        """
        if markets is None:
            markets = list(self._price_cache.values())
        groups: dict[str, list[dict]] = {}
        for m in markets:
            slug = m.get("event_slug", "")
            if slug:
                groups.setdefault(slug, []).append(m)
        # Require 3+ markets AND passing exhaustive validation
        return {k: v for k, v in groups.items()
                if len(v) >= 3 and self.validate_exhaustive_group(v)}
