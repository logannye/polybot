"""Client for The Odds API — fetches sportsbook odds for cross-venue comparison."""

import aiohttp
import structlog

log = structlog.get_logger()

CONSENSUS_BOOKS = {"fanduel", "draftkings", "betmgm", "williamhill_us", "bovada"}

DEFAULT_SPORTS = ["basketball_nba", "icehockey_nhl", "soccer_epl",
                  "soccer_uefa_champs_league"]


def american_to_prob(american_odds: int | float) -> float:
    """Convert American odds to implied probability (0-1)."""
    odds = float(american_odds)
    if odds >= 100:
        return 100.0 / (odds + 100.0)
    else:
        return abs(odds) / (abs(odds) + 100.0)


def devig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Remove vig from a two-outcome market. Returns fair probabilities summing to 1.0."""
    total = prob_a + prob_b
    if total == 0:
        return 0.5, 0.5
    return prob_a / total, prob_b / total


def compute_consensus(bookmakers: list[dict]) -> dict[str, float] | None:
    """Average implied probabilities across major sportsbooks for each outcome."""
    outcome_probs: dict[str, list[float]] = {}

    for bk in bookmakers:
        if bk.get("key") not in CONSENSUS_BOOKS:
            continue
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) != 2:
                continue
            raw_a = american_to_prob(outcomes[0]["price"])
            raw_b = american_to_prob(outcomes[1]["price"])
            fair_a, fair_b = devig(raw_a, raw_b)
            outcome_probs.setdefault(outcomes[0]["name"], []).append(fair_a)
            outcome_probs.setdefault(outcomes[1]["name"], []).append(fair_b)

    if len(outcome_probs) < 2:
        return None

    consensus = {}
    for name, probs in outcome_probs.items():
        consensus[name] = sum(probs) / len(probs)

    return consensus


def find_polymarket_prices(bookmakers: list[dict]) -> dict[str, float] | None:
    """Extract Polymarket prices from The Odds API response (us_ex region)."""
    for bk in bookmakers:
        if bk.get("key") != "polymarket":
            continue
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) != 2:
                continue
            prices = {}
            for o in outcomes:
                prices[o["name"]] = american_to_prob(o["price"])
            return prices
    return None


def find_divergences(
    events: list[dict],
    min_divergence: float = 0.03,
) -> list[dict]:
    """Compare sportsbook consensus to Polymarket prices for all events."""
    divergences = []

    for event in events:
        consensus = compute_consensus(event.get("bookmakers", []))
        poly_prices = find_polymarket_prices(event.get("bookmakers", []))

        if not consensus or not poly_prices:
            continue

        for outcome_name in consensus:
            if outcome_name not in poly_prices:
                continue

            consensus_prob = consensus[outcome_name]
            poly_prob = poly_prices[outcome_name]
            divergence = consensus_prob - poly_prob

            if abs(divergence) >= min_divergence:
                divergences.append({
                    "event_id": event.get("id", ""),
                    "sport": event.get("sport_key", ""),
                    "home_team": event.get("home_team", ""),
                    "away_team": event.get("away_team", ""),
                    "commence_time": event.get("commence_time", ""),
                    "outcome_name": outcome_name,
                    "consensus_prob": round(consensus_prob, 4),
                    "polymarket_prob": round(poly_prob, 4),
                    "divergence": round(divergence, 4),
                    "side": "YES" if divergence > 0 else "NO",
                })

    return divergences


class OddsClient:
    """Async client for The Odds API."""

    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str, sports: list[str] | None = None,
                 credit_reserve: int = 10):
        self._api_key = api_key
        self._sports = sports or DEFAULT_SPORTS
        self._session: aiohttp.ClientSession | None = None
        self._credits_remaining: int | None = None
        self._credit_reserve = credit_reserve

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()

    async def fetch_odds(self, sport_key: str) -> list[dict]:
        """Fetch odds for a sport. Costs 2 credits (1 market × 2 regions)."""
        if not self._session or not self._api_key:
            return []

        if (self._credits_remaining is not None
                and self._credits_remaining <= self._credit_reserve):
            log.warning("odds_api_credits_low", credits_remaining=self._credits_remaining,
                        credit_reserve=self._credit_reserve)
            return []

        url = f"{self.BASE_URL}/sports/{sport_key}/odds/"
        params = {
            "apiKey": self._api_key,
            "regions": "us,us_ex",
            "markets": "h2h",
            "oddsFormat": "american",
        }

        try:
            async with self._session.get(url, params=params,
                                          timeout=aiohttp.ClientTimeout(total=15)) as resp:
                remaining = resp.headers.get("x-requests-remaining")
                if remaining:
                    self._credits_remaining = int(remaining)

                if resp.status != 200:
                    log.error("odds_api_error", sport=sport_key, status=resp.status)
                    return []

                data = await resp.json()
                log.info("odds_fetched", sport=sport_key, events=len(data),
                         credits_remaining=self._credits_remaining)
                return data

        except Exception as e:
            log.error("odds_api_exception", sport=sport_key, error=str(e))
            return []

    async def fetch_all_sports(self) -> list[dict]:
        """Fetch odds for all configured sports."""
        all_events = []
        for sport in self._sports:
            events = await self.fetch_odds(sport)
            all_events.extend(events)
        return all_events

    @property
    def credits_remaining(self) -> int | None:
        return self._credits_remaining
