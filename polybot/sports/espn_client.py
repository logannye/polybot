"""ESPN scoreboard client — fetches live MLB, NBA, and NHL game data."""

import aiohttp
import structlog

log = structlog.get_logger()

SPORT_URLS: dict[str, str] = {
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "ncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "ucl": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
    "epl": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "laliga": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
    "bundesliga": "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",
    "mls": "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard",
}

# ESPN status names that should be included (non-scheduled)
STATUS_MAP: dict[str, str] = {
    "STATUS_IN_PROGRESS": "in_progress",
    "STATUS_HALFTIME": "in_progress",
    "STATUS_FINAL": "final",
    "STATUS_END_PERIOD": "in_progress",
}

_TIMEOUT = aiohttp.ClientTimeout(total=10)


def parse_espn_scoreboard(data: dict, sport: str) -> list[dict]:
    """Parse an ESPN scoreboard API response into normalized game dicts.

    Scheduled and postponed games are skipped. Only games whose status type
    appears in STATUS_MAP are returned.

    Each returned dict contains:
        espn_id, sport, name, short_name,
        home_team, away_team, home_abbrev, away_abbrev,
        home_score, away_score, period, clock,
        status, completed
    """
    games: list[dict] = []

    for event in data.get("events", []):
        status_block = event.get("status", {})
        status_type = status_block.get("type", {})
        status_name = status_type.get("name", "")

        normalized_status = STATUS_MAP.get(status_name)
        if normalized_status is None:
            # Skip scheduled, postponed, or unknown statuses
            continue

        completed: bool = bool(status_type.get("completed", False))
        period: int = int(status_block.get("period", 0))
        clock: str = status_block.get("displayClock", "")

        # Extract competitors from the first competition
        competitions = event.get("competitions", [])
        competitors = competitions[0].get("competitors", []) if competitions else []

        home_team = away_team = ""
        home_abbrev = away_abbrev = ""
        home_score = away_score = 0

        for comp in competitors:
            team = comp.get("team", {})
            display_name = team.get("displayName", "")
            abbreviation = team.get("abbreviation", "")
            score = int(comp.get("score", 0) or 0)

            if comp.get("homeAway") == "home":
                home_team = display_name
                home_abbrev = abbreviation
                home_score = score
            elif comp.get("homeAway") == "away":
                away_team = display_name
                away_abbrev = abbreviation
                away_score = score

        games.append({
            "espn_id": event.get("id", ""),
            "sport": sport,
            "name": event.get("name", ""),
            "short_name": event.get("shortName", ""),
            "home_team": home_team,
            "away_team": away_team,
            "home_abbrev": home_abbrev,
            "away_abbrev": away_abbrev,
            "home_score": home_score,
            "away_score": away_score,
            "period": period,
            "clock": clock,
            "status": normalized_status,
            "completed": completed,
        })

    return games


class ESPNClient:
    """Async client for ESPN's free scoreboard API.

    Usage::

        client = ESPNClient(sports=["mlb", "nba", "nhl"])
        await client.start()
        games = await client.fetch_all_live_games()
        await client.close()
    """

    def __init__(self, sports: list[str] | None = None) -> None:
        self._sports: list[str] = sports if sports is not None else list(SPORT_URLS)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Open the underlying aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def fetch_scoreboard(self, sport: str) -> list[dict]:
        """Fetch and parse the ESPN scoreboard for a single sport.

        Returns a list of normalized game dicts (scheduled games excluded).
        Returns an empty list on any HTTP or parsing error.
        """
        if self._session is None or self._session.closed:
            raise RuntimeError("ESPNClient not started — call await client.start() first")

        url = SPORT_URLS.get(sport)
        if url is None:
            log.warning("espn_client.unknown_sport", sport=sport)
            return []

        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
            games = parse_espn_scoreboard(data, sport)
            log.debug("espn_client.fetched", sport=sport, game_count=len(games))
            return games
        except aiohttp.ClientError as exc:
            log.warning("espn_client.http_error", sport=sport, error=str(exc))
            return []
        except Exception as exc:
            log.warning("espn_client.parse_error", sport=sport, error=str(exc))
            return []

    async def fetch_all_live_games(self) -> list[dict]:
        """Fetch live games for all configured sports.

        Returns a flat list of normalized game dicts across all sports.
        Sports that error are skipped silently.
        """
        all_games: list[dict] = []
        for sport in self._sports:
            games = await self.fetch_scoreboard(sport)
            all_games.extend(games)
        log.info("espn_client.fetch_all_complete", total_games=len(all_games))
        return all_games
