"""ESPN scoreboard client — fetches live MLB, NBA, and NHL game data.

Also exposes pregame BPI / matchup-predictor probabilities via the
/summary endpoint for the v11.0b PregameSharpStrategy.
"""

from datetime import datetime, timezone
from typing import Optional

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

# ESPN /summary endpoints accept ?event=ID and return predictor + odds.
# Path matches the scoreboard URL minus the trailing /scoreboard.
SUMMARY_URLS: dict[str, str] = {
    sport: url.rsplit("/scoreboard", 1)[0] + "/summary"
    for sport, url in SPORT_URLS.items()
}

# ESPN status names that should be included (non-scheduled)
STATUS_MAP: dict[str, str] = {
    "STATUS_IN_PROGRESS": "in_progress",
    "STATUS_HALFTIME": "in_progress",
    "STATUS_FINAL": "final",
    "STATUS_END_PERIOD": "in_progress",
}

# Pre-game statuses — relevant for v11.0b PregameSharpStrategy
PREGAME_STATUS_MAP: dict[str, str] = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_PRE_GAME": "scheduled",
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

    async def fetch_pregame_events(self, sport: str) -> list[dict]:
        """Return scheduled (not-yet-started) games for ``sport``.

        Each dict has espn_id, sport, name, home_team, away_team,
        start_time (UTC datetime), plus the raw status string. Used by
        v11.0b PregameSharpStrategy to identify upcoming games to fetch
        BPI predictions for.
        """
        if self._session is None or self._session.closed:
            raise RuntimeError("ESPNClient not started — call await client.start() first")
        url = SPORT_URLS.get(sport)
        if url is None:
            return []
        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("espn_client.pregame_http_error", sport=sport, error=str(exc))
            return []
        return parse_espn_pregame_scoreboard(data, sport)

    async def fetch_pregame_summary(self, sport: str, event_id: str
                                     ) -> Optional[dict]:
        """Fetch the per-event /summary and extract the predictor data.

        Returns a dict with home_win_prob (0-1), total_line (or None),
        spread_line (or None), fetched_at (UTC), or None on any failure.
        """
        if self._session is None or self._session.closed:
            raise RuntimeError("ESPNClient not started — call await client.start() first")
        url = SUMMARY_URLS.get(sport)
        if url is None:
            return None
        try:
            async with self._session.get(
                url, params={"event": event_id}, timeout=_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("espn_client.summary_http_error",
                        sport=sport, event=event_id, error=str(exc))
            return None
        return parse_pregame_summary(data)


def parse_espn_pregame_scoreboard(data: dict, sport: str) -> list[dict]:
    """Extract scheduled (not-yet-started) games from a scoreboard response."""
    out: list[dict] = []
    for event in data.get("events", []):
        status_block = event.get("status", {})
        status_name = status_block.get("type", {}).get("name", "")
        if status_name not in PREGAME_STATUS_MAP:
            continue

        competitions = event.get("competitions", [])
        competitors = competitions[0].get("competitors", []) if competitions else []
        home_team = away_team = ""
        for comp in competitors:
            team = comp.get("team", {})
            display_name = team.get("displayName", "")
            if comp.get("homeAway") == "home":
                home_team = display_name
            elif comp.get("homeAway") == "away":
                away_team = display_name

        start_iso = event.get("date") or competitions[0].get("date") if competitions else None
        start_time = _parse_espn_iso(start_iso)

        out.append({
            "espn_id": event.get("id", ""),
            "sport": sport,
            "name": event.get("name", ""),
            "short_name": event.get("shortName", ""),
            "home_team": home_team,
            "away_team": away_team,
            "start_time": start_time,
            "status": PREGAME_STATUS_MAP[status_name],
        })
    return out


def parse_pregame_summary(data: dict) -> Optional[dict]:
    """Extract predictor + closing-line fields from a /summary response."""
    predictor = data.get("predictor") or {}
    home_proj = predictor.get("homeTeam", {}).get("gameProjection")
    if home_proj is None:
        # Some sports / dates lack predictor data entirely.
        return None
    try:
        home_win_prob = float(home_proj) / 100.0
    except (TypeError, ValueError):
        return None
    if not 0.0 <= home_win_prob <= 1.0:
        return None

    pickcenter = data.get("pickcenter") or []
    pc0 = pickcenter[0] if pickcenter else {}
    total_line = pc0.get("overUnder")
    spread_line = pc0.get("spread")

    return {
        "home_win_prob": home_win_prob,
        "total_line": float(total_line) if total_line is not None else None,
        "spread_line": float(spread_line) if spread_line is not None else None,
        "fetched_at": datetime.now(timezone.utc),
    }


def _parse_espn_iso(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
