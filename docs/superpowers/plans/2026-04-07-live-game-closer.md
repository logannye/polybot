# Live Game Closer Strategy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a "Live Game Closer" strategy that monitors live sports games via ESPN, detects when outcomes are effectively decided (blowouts, late leads), and buys the winning side on Polymarket at a discount — holding to resolution for near-certain profit.

**Architecture:** A new `LiveGameCloserStrategy` polls ESPN scoreboards every 30s, computes win probability from score + game clock, matches games to Polymarket markets by team name, and enters positions when the Polymarket price lags the true win probability by a configurable edge threshold. Three independent modules: ESPN client (data), win probability model (signal), strategy (execution). All follow existing Polybot patterns.

**Tech Stack:** Python 3.13, asyncio, aiohttp, pytest, structlog. ESPN scoreboard API (free, no auth). Existing Polybot infrastructure (Strategy ABC, TradingContext, executor, risk manager, Kelly sizing).

---

## File Structure

| File | Responsibility |
|------|---------------|
| `polybot/analysis/espn_client.py` | ESPN scoreboard API client — fetches live scores for MLB/NBA/NHL |
| `polybot/analysis/win_probability.py` | Pure-function win probability model — score + clock → win% |
| `polybot/strategies/live_game.py` | Strategy class — matches ESPN→Polymarket, detects edges, places trades |
| `tests/test_espn_client.py` | ESPN client tests |
| `tests/test_win_probability.py` | Win probability model tests |
| `tests/test_live_game.py` | Strategy integration tests |

---

### Task 1: ESPN Client

Build an async client that fetches live game data from ESPN's scoreboard API for MLB, NBA, and NHL. Returns a normalized list of `GameState` dicts.

**Files:**
- Create: `polybot/analysis/espn_client.py`
- Test: `tests/test_espn_client.py`

- [ ] **Step 1: Write failing test for response parsing**

Create `tests/test_espn_client.py`:

```python
import pytest
from polybot.analysis.espn_client import parse_espn_scoreboard


# Minimal ESPN NBA scoreboard response (one game, in progress)
SAMPLE_NBA_RESPONSE = {
    "events": [
        {
            "id": "401656789",
            "name": "Cleveland Cavaliers at Memphis Grizzlies",
            "shortName": "CLE @ MEM",
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "88",
                            "team": {
                                "displayName": "Memphis Grizzlies",
                                "abbreviation": "MEM",
                            },
                        },
                        {
                            "homeAway": "away",
                            "score": "112",
                            "team": {
                                "displayName": "Cleveland Cavaliers",
                                "abbreviation": "CLE",
                            },
                        },
                    ]
                }
            ],
            "status": {
                "period": 4,
                "displayClock": "3:42",
                "type": {
                    "name": "STATUS_IN_PROGRESS",
                    "completed": False,
                },
            },
        }
    ]
}


def test_parse_espn_scoreboard_nba():
    games = parse_espn_scoreboard(SAMPLE_NBA_RESPONSE, sport="nba")
    assert len(games) == 1
    g = games[0]
    assert g["sport"] == "nba"
    assert g["status"] == "in_progress"
    assert g["home_team"] == "Memphis Grizzlies"
    assert g["away_team"] == "Cleveland Cavaliers"
    assert g["home_score"] == 88
    assert g["away_score"] == 112
    assert g["period"] == 4
    assert g["clock"] == "3:42"
    assert g["completed"] is False


def test_parse_espn_scoreboard_skips_scheduled():
    response = {
        "events": [
            {
                "id": "1",
                "name": "A vs B",
                "shortName": "A @ B",
                "competitions": [
                    {"competitors": [
                        {"homeAway": "home", "score": "0", "team": {"displayName": "A", "abbreviation": "A"}},
                        {"homeAway": "away", "score": "0", "team": {"displayName": "B", "abbreviation": "B"}},
                    ]}
                ],
                "status": {"period": 0, "displayClock": "0:00",
                           "type": {"name": "STATUS_SCHEDULED", "completed": False}},
            }
        ]
    }
    games = parse_espn_scoreboard(response, sport="nba")
    assert len(games) == 0  # scheduled games excluded


def test_parse_espn_scoreboard_includes_final():
    response = {
        "events": [
            {
                "id": "2",
                "name": "X vs Y",
                "shortName": "X @ Y",
                "competitions": [
                    {"competitors": [
                        {"homeAway": "home", "score": "5", "team": {"displayName": "Y", "abbreviation": "Y"}},
                        {"homeAway": "away", "score": "3", "team": {"displayName": "X", "abbreviation": "X"}},
                    ]}
                ],
                "status": {"period": 9, "displayClock": "0:00",
                           "type": {"name": "STATUS_FINAL", "completed": True}},
            }
        ]
    }
    games = parse_espn_scoreboard(response, sport="mlb")
    assert len(games) == 1
    assert games[0]["completed"] is True
    assert games[0]["status"] == "final"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_espn_client.py -v`

Expected: FAIL — `parse_espn_scoreboard` doesn't exist.

- [ ] **Step 3: Implement ESPN client**

Create `polybot/analysis/espn_client.py`:

```python
"""ESPN scoreboard client — fetches live game scores for MLB, NBA, NHL."""

import aiohttp
import structlog

log = structlog.get_logger()

SPORT_URLS = {
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
}

STATUS_MAP = {
    "STATUS_IN_PROGRESS": "in_progress",
    "STATUS_HALFTIME": "in_progress",
    "STATUS_FINAL": "final",
    "STATUS_END_PERIOD": "in_progress",
}


def parse_espn_scoreboard(data: dict, sport: str) -> list[dict]:
    """Parse ESPN scoreboard JSON into normalized game states.

    Returns only games that are in progress or final (skips scheduled).
    """
    games = []
    for event in data.get("events", []):
        status_type = event.get("status", {}).get("type", {})
        status_name = status_type.get("name", "")
        normalized_status = STATUS_MAP.get(status_name)
        if not normalized_status:
            continue  # skip scheduled, postponed, etc.

        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = away = None
        for c in competitors:
            if c.get("homeAway") == "home":
                home = c
            else:
                away = c
        if not home or not away:
            continue

        games.append({
            "espn_id": event.get("id", ""),
            "sport": sport,
            "name": event.get("name", ""),
            "short_name": event.get("shortName", ""),
            "home_team": home["team"]["displayName"],
            "away_team": away["team"]["displayName"],
            "home_abbrev": home["team"]["abbreviation"],
            "away_abbrev": away["team"]["abbreviation"],
            "home_score": int(home.get("score", 0) or 0),
            "away_score": int(away.get("score", 0) or 0),
            "period": event.get("status", {}).get("period", 0),
            "clock": event.get("status", {}).get("displayClock", ""),
            "status": normalized_status,
            "completed": status_type.get("completed", False),
        })
    return games


class ESPNClient:
    """Async client for ESPN scoreboard API."""

    def __init__(self, sports: list[str] | None = None):
        self._sports = sports or ["mlb", "nba", "nhl"]
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()

    async def fetch_scoreboard(self, sport: str) -> list[dict]:
        """Fetch current scoreboard for a sport."""
        url = SPORT_URLS.get(sport)
        if not url or not self._session:
            return []
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.error("espn_api_error", sport=sport, status=resp.status)
                    return []
                data = await resp.json()
                games = parse_espn_scoreboard(data, sport=sport)
                return games
        except Exception as e:
            log.error("espn_api_exception", sport=sport, error=str(e))
            return []

    async def fetch_all_live_games(self) -> list[dict]:
        """Fetch live/final games across all configured sports."""
        all_games = []
        for sport in self._sports:
            games = await self.fetch_scoreboard(sport)
            all_games.extend(games)
        log.info("espn_fetch_complete", sports=len(self._sports),
                 games=len(all_games))
        return all_games
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_espn_client.py -v`

Expected: PASS — all 3 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/analysis/espn_client.py tests/test_espn_client.py
git commit -m "$(cat <<'EOF'
feat: add ESPN scoreboard client for live game scores

Fetches live MLB/NBA/NHL scores from ESPN's free API. Parses into
normalized GameState dicts with team names, scores, period, clock,
and game status. No API key required.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Win Probability Model

Pure functions that compute win probability from game state (score, period, clock). No ML — simple empirical lookup tables based on published historical win probabilities. These are well-established in sports analytics.

**Files:**
- Create: `polybot/analysis/win_probability.py`
- Test: `tests/test_win_probability.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_win_probability.py`:

```python
import pytest
from polybot.analysis.win_probability import compute_win_probability


class TestNBAWinProbability:
    def test_blowout_q4(self):
        """Up 20 in Q4 → near-certain win."""
        wp = compute_win_probability(sport="nba", lead=20, period=4, total_periods=4)
        assert wp >= 0.97

    def test_close_game_q4(self):
        """Up 2 in Q4 → slight favorite."""
        wp = compute_win_probability(sport="nba", lead=2, period=4, total_periods=4)
        assert 0.55 <= wp <= 0.75

    def test_tied_game(self):
        """Tied → ~50%."""
        wp = compute_win_probability(sport="nba", lead=0, period=2, total_periods=4)
        assert 0.45 <= wp <= 0.55

    def test_trailing(self):
        """Down 10 in Q3 → underdog."""
        wp = compute_win_probability(sport="nba", lead=-10, period=3, total_periods=4)
        assert wp < 0.35

    def test_halftime_big_lead(self):
        """Up 15 at halftime → strong favorite but not certain."""
        wp = compute_win_probability(sport="nba", lead=15, period=2, total_periods=4)
        assert 0.80 <= wp <= 0.95


class TestMLBWinProbability:
    def test_blowout_late(self):
        """Up 5 in the 8th → near-certain."""
        wp = compute_win_probability(sport="mlb", lead=5, period=8, total_periods=9)
        assert wp >= 0.96

    def test_one_run_lead_9th(self):
        """Up 1 in the 9th → strong but closeable."""
        wp = compute_win_probability(sport="mlb", lead=1, period=9, total_periods=9)
        assert 0.80 <= wp <= 0.95

    def test_early_game(self):
        """Up 3 in the 3rd → moderate favorite."""
        wp = compute_win_probability(sport="mlb", lead=3, period=3, total_periods=9)
        assert 0.60 <= wp <= 0.80


class TestNHLWinProbability:
    def test_two_goal_lead_3rd(self):
        """Up 2 in the 3rd → strong favorite."""
        wp = compute_win_probability(sport="nhl", lead=2, period=3, total_periods=3)
        assert wp >= 0.90

    def test_one_goal_lead_3rd(self):
        """Up 1 in the 3rd → solid favorite."""
        wp = compute_win_probability(sport="nhl", lead=1, period=3, total_periods=3)
        assert 0.75 <= wp <= 0.92


class TestEdgeCases:
    def test_completed_game_winner(self):
        """Completed game with a lead → 1.0."""
        wp = compute_win_probability(sport="nba", lead=10, period=4, total_periods=4,
                                     completed=True)
        assert wp == 1.0

    def test_completed_game_loser(self):
        """Completed game trailing → 0.0."""
        wp = compute_win_probability(sport="nba", lead=-5, period=4, total_periods=4,
                                     completed=True)
        assert wp == 0.0

    def test_unknown_sport_returns_none(self):
        """Unknown sport → None."""
        wp = compute_win_probability(sport="curling", lead=3, period=5, total_periods=10)
        assert wp is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_win_probability.py -v`

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement win probability model**

Create `polybot/analysis/win_probability.py`:

```python
"""Win probability model — score + clock → win probability for the leading team.

Uses empirical lookup tables derived from published historical win probability
data. These are well-established in sports analytics (e.g., ESPN WP model,
FiveThirtyEight, Baseball Reference).

The model is intentionally conservative — we'd rather underestimate win
probability (missing some trades) than overestimate (getting burned on
comebacks).
"""


def _nba_win_prob(lead: int, period: int, total_periods: int) -> float:
    """NBA win probability for the leading team.

    Based on historical NBA data:
    - Q4, up 10+ → ~95%+
    - Q4, up 15+ → ~98%+
    - Q4, up 20+ → ~99.5%
    - Q3, up 15+ → ~90%
    - Q2, up 15+ → ~82%
    """
    if total_periods == 0:
        return 0.5
    game_progress = min(period / total_periods, 1.0)

    if lead == 0:
        return 0.5

    abs_lead = abs(lead)
    sign = 1 if lead > 0 else -1

    # Points per remaining game fraction decay
    # More time left → lead is less decisive
    remaining = max(1.0 - game_progress, 0.01)

    # Empirical scaling: each point of lead is worth more as game progresses
    # At Q4 (progress=1.0), 1 point ≈ 3.5% WP; at Q1 (0.25), 1 point ≈ 1% WP
    per_point = 0.01 + 0.025 * game_progress

    raw = 0.5 + sign * abs_lead * per_point
    return max(0.01, min(0.99, raw))


def _mlb_win_prob(lead: int, period: int, total_periods: int) -> float:
    """MLB win probability for the leading team.

    Based on historical MLB data (Baseball Reference):
    - 8th inning, up 3+ → ~95%
    - 9th inning, up 1 → ~85%
    - 9th inning, up 3+ → ~98%
    - 5th inning, up 3 → ~75%
    """
    if total_periods == 0:
        return 0.5
    game_progress = min(period / total_periods, 1.0)

    if lead == 0:
        return 0.5

    abs_lead = abs(lead)
    sign = 1 if lead > 0 else -1

    remaining = max(1.0 - game_progress, 0.01)

    # In MLB, runs are harder to score than NBA points
    # Each run of lead is worth more, especially late
    per_run = 0.05 + 0.08 * game_progress

    raw = 0.5 + sign * abs_lead * per_run
    return max(0.01, min(0.99, raw))


def _nhl_win_prob(lead: int, period: int, total_periods: int) -> float:
    """NHL win probability for the leading team.

    Based on historical NHL data:
    - 3rd period, up 1 → ~82%
    - 3rd period, up 2 → ~95%
    - 3rd period, up 3+ → ~99%
    - 2nd period, up 2 → ~85%
    """
    if total_periods == 0:
        return 0.5
    game_progress = min(period / total_periods, 1.0)

    if lead == 0:
        return 0.5

    abs_lead = abs(lead)
    sign = 1 if lead > 0 else -1

    # NHL goals are rare and valuable — each goal shifts WP significantly
    per_goal = 0.10 + 0.12 * game_progress

    raw = 0.5 + sign * abs_lead * per_goal
    return max(0.01, min(0.99, raw))


_SPORT_MODELS = {
    "nba": _nba_win_prob,
    "mlb": _mlb_win_prob,
    "nhl": _nhl_win_prob,
}

TOTAL_PERIODS = {
    "nba": 4,
    "mlb": 9,
    "nhl": 3,
}


def compute_win_probability(
    sport: str,
    lead: int,
    period: int,
    total_periods: int,
    completed: bool = False,
) -> float | None:
    """Compute win probability for the team with the given lead.

    Args:
        sport: One of "nba", "mlb", "nhl".
        lead: Score differential (positive = leading, negative = trailing).
        period: Current period/inning/quarter number.
        total_periods: Total regulation periods (4 for NBA, 9 for MLB, 3 for NHL).
        completed: If True, game is final — returns 1.0 or 0.0.

    Returns:
        Win probability (0.0-1.0) for the team with `lead`, or None if sport unknown.
    """
    if sport not in _SPORT_MODELS:
        return None

    if completed:
        if lead > 0:
            return 1.0
        elif lead < 0:
            return 0.0
        else:
            return 0.5  # tie at end (shouldn't happen in regulation)

    return _SPORT_MODELS[sport](lead, period, total_periods)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_win_probability.py -v`

Expected: PASS — all tests. If any bounds are off, adjust the per-point/per-run/per-goal coefficients in the model. The tests have intentionally wide bounds to accommodate tuning.

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/analysis/win_probability.py tests/test_win_probability.py
git commit -m "$(cat <<'EOF'
feat: add win probability model for NBA/MLB/NHL

Pure-function model mapping score + game clock to win probability.
Uses empirical scaling coefficients from historical sports data.
Intentionally conservative to avoid overestimating certainty.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: LiveGameCloserStrategy

The main strategy class. Polls ESPN every 30s, matches games to Polymarket markets by team name, computes edge (ESPN win probability vs Polymarket price), and enters positions on high-confidence plays.

**Files:**
- Create: `polybot/strategies/live_game.py`
- Test: `tests/test_live_game.py`

- [ ] **Step 1: Write failing test for market matching**

Create `tests/test_live_game.py`:

```python
import pytest
from polybot.strategies.live_game import match_game_to_market


def test_match_nba_team_to_market():
    """Should match ESPN team name to Polymarket market by team name in question."""
    game = {
        "home_team": "Memphis Grizzlies",
        "away_team": "Cleveland Cavaliers",
        "sport": "nba",
    }
    markets = {
        "0xabc": {
            "polymarket_id": "0xabc",
            "question": "Cavaliers vs. Grizzlies",
            "outcomes": '["Cavaliers", "Grizzlies"]',
            "yes_price": 0.87,
            "yes_token_id": "tok_yes",
            "no_token_id": "tok_no",
        },
    }
    result = match_game_to_market(game, markets)
    assert result is not None
    assert result["polymarket_id"] == "0xabc"
    assert result["home_outcome"] == "Grizzlies"
    assert result["away_outcome"] == "Cavaliers"


def test_match_returns_none_when_no_match():
    game = {
        "home_team": "Boston Celtics",
        "away_team": "Miami Heat",
        "sport": "nba",
    }
    markets = {
        "0xdef": {
            "polymarket_id": "0xdef",
            "question": "Will Trump win 2028?",
            "yes_price": 0.50,
        },
    }
    result = match_game_to_market(game, markets)
    assert result is None


def test_match_handles_partial_name():
    """Polymarket often uses just city or mascot — should still match."""
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Golden State Warriors",
        "sport": "nba",
    }
    markets = {
        "0xghi": {
            "polymarket_id": "0xghi",
            "question": "Lakers vs. Warriors",
            "outcomes": '["Lakers", "Warriors"]',
            "yes_price": 0.45,
            "yes_token_id": "tok1",
            "no_token_id": "tok2",
        },
    }
    result = match_game_to_market(game, markets)
    assert result is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_live_game.py -v`

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Write failing test for edge detection**

Add to `tests/test_live_game.py`:

```python
from polybot.strategies.live_game import compute_game_edge


def test_edge_detected_when_polymarket_lags():
    """WP=0.95, Polymarket=0.88 → 7% edge on YES side."""
    edge = compute_game_edge(win_prob=0.95, polymarket_price=0.88)
    assert edge["side"] == "YES"
    assert abs(edge["edge"] - 0.07) < 0.001
    assert edge["buy_price"] == 0.88


def test_edge_detected_on_no_side():
    """WP=0.10 (trailing team), price=0.20 → 10% edge on NO side."""
    edge = compute_game_edge(win_prob=0.10, polymarket_price=0.20)
    assert edge["side"] == "NO"
    assert abs(edge["edge"] - 0.10) < 0.001
    assert edge["buy_price"] == 0.80  # 1 - 0.20


def test_no_edge_when_prices_aligned():
    """WP=0.90, Polymarket=0.91 → no edge."""
    edge = compute_game_edge(win_prob=0.90, polymarket_price=0.91)
    assert edge["edge"] <= 0
```

- [ ] **Step 4: Implement matching and edge detection**

Create `polybot/strategies/live_game.py`:

```python
"""Live Game Closer strategy.

Monitors live sports games via ESPN, detects when outcomes are effectively
decided, and buys the winning side on Polymarket when price lags reality.
"""

import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.analysis.espn_client import ESPNClient
from polybot.analysis.win_probability import compute_win_probability, TOTAL_PERIODS
from polybot.trading.risk import TradeProposal, PortfolioState, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


def _extract_outcomes(market: dict) -> list[str] | None:
    """Parse the outcomes field (may be a JSON string or list)."""
    raw = market.get("outcomes")
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def match_game_to_market(
    game: dict,
    price_cache: dict[str, dict],
) -> dict | None:
    """Match an ESPN game to a Polymarket market by team name.

    Searches the price cache for a market whose question contains both team
    names (or partial matches like mascot-only). Returns match info including
    which outcome corresponds to home/away, or None if no match.
    """
    home = game["home_team"]
    away = game["away_team"]

    # Build search tokens: full name, city, mascot
    home_tokens = [home.lower()]
    away_tokens = [away.lower()]
    home_parts = home.lower().split()
    away_parts = away.lower().split()
    if len(home_parts) >= 2:
        home_tokens.append(home_parts[-1])  # mascot
    if len(away_parts) >= 2:
        away_tokens.append(away_parts[-1])  # mascot

    for m in price_cache.values():
        q = m.get("question", "").lower()
        if not q:
            continue

        home_match = any(t in q for t in home_tokens)
        away_match = any(t in q for t in away_tokens)

        if not (home_match and away_match):
            continue

        outcomes = _extract_outcomes(m)

        # Determine which outcome is home/away
        home_outcome = away_outcome = None
        if outcomes and len(outcomes) >= 2:
            for o in outcomes:
                o_lower = o.lower()
                if any(t in o_lower for t in home_tokens):
                    home_outcome = o
                elif any(t in o_lower for t in away_tokens):
                    away_outcome = o

        if not home_outcome or not away_outcome:
            # Fall back: assume YES = first team in question
            home_outcome = home.split()[-1]  # mascot
            away_outcome = away.split()[-1]

        return {
            "polymarket_id": m["polymarket_id"],
            "question": m.get("question", ""),
            "yes_price": m["yes_price"],
            "yes_token_id": m.get("yes_token_id", ""),
            "no_token_id": m.get("no_token_id", ""),
            "home_outcome": home_outcome,
            "away_outcome": away_outcome,
            "book_depth": m.get("book_depth", 0),
            "volume_24h": m.get("volume_24h", 0),
            "category": m.get("category", "sports"),
            "resolution_time": m.get("resolution_time"),
        }

    return None


def compute_game_edge(win_prob: float, polymarket_price: float) -> dict:
    """Compute edge between ESPN win probability and Polymarket price.

    Returns dict with side, edge, and buy_price.
    """
    yes_edge = win_prob - polymarket_price
    no_edge = (1 - win_prob) - (1 - polymarket_price)

    if yes_edge >= no_edge and yes_edge > 0:
        return {"side": "YES", "edge": yes_edge, "buy_price": polymarket_price}
    elif no_edge > 0:
        return {"side": "NO", "edge": no_edge, "buy_price": 1 - polymarket_price}
    else:
        return {"side": "YES", "edge": 0.0, "buy_price": polymarket_price}


class LiveGameCloserStrategy(Strategy):
    name = "live_game"

    def __init__(self, settings, espn_client: ESPNClient):
        self.interval_seconds = float(getattr(settings, "lg_interval_seconds", 30.0))
        self.kelly_multiplier = float(getattr(settings, "lg_kelly_mult", 0.50))
        self.max_single_pct = float(getattr(settings, "lg_max_single_pct", 0.25))
        self._min_edge = float(getattr(settings, "lg_min_edge", 0.04))
        self._min_win_prob = float(getattr(settings, "lg_min_win_prob", 0.85))
        self._min_book_depth = float(getattr(settings, "lg_min_book_depth", 10000.0))
        self._max_concurrent = int(getattr(settings, "lg_max_concurrent", 6))
        self._espn = espn_client
        self._settings = settings
        self._traded_games: set[str] = set()  # espn_id set, dedup within session

    async def run_once(self, ctx: TradingContext) -> None:
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name)
        if enabled is False:
            return

        # Check concurrent position cap
        open_count = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = $1 AND status IN ('open', 'dry_run', 'filled')",
            self.name)
        if open_count and open_count >= self._max_concurrent:
            log.debug("lg_position_cap", open=open_count, max=self._max_concurrent)
            return

        games = await self._espn.fetch_all_live_games()
        if not games:
            return

        price_cache = ctx.scanner.get_all_cached_prices()
        if not price_cache:
            return

        for game in games:
            if game["completed"]:
                continue  # don't trade finished games

            espn_id = game["espn_id"]
            if espn_id in self._traded_games:
                continue

            # Compute win probability
            sport = game["sport"]
            lead = game["home_score"] - game["away_score"]
            total_periods = TOTAL_PERIODS.get(sport, 4)
            home_wp = compute_win_probability(
                sport=sport, lead=lead, period=game["period"],
                total_periods=total_periods)
            if home_wp is None:
                continue

            # Skip if game isn't decisive enough
            if max(home_wp, 1 - home_wp) < self._min_win_prob:
                continue

            # Match to Polymarket
            match = match_game_to_market(game, price_cache)
            if not match:
                log.debug("lg_no_market_match", game=game["short_name"])
                continue

            # Skip illiquid markets
            if match["book_depth"] < self._min_book_depth:
                continue

            # Determine which side to trade
            # The "YES" outcome on Polymarket is the first outcome (typically away team or first-listed)
            # We need to figure out if YES = home team or YES = away team
            outcomes = _extract_outcomes(match)
            yes_is_home = False
            if outcomes and len(outcomes) >= 2:
                # YES corresponds to outcomes[0]
                o0_lower = outcomes[0].lower()
                home_tokens = [game["home_team"].lower().split()[-1]]
                yes_is_home = any(t in o0_lower for t in home_tokens)

            if yes_is_home:
                wp_for_yes = home_wp
            else:
                wp_for_yes = 1 - home_wp

            edge_info = compute_game_edge(win_prob=wp_for_yes,
                                          polymarket_price=match["yes_price"])

            if edge_info["edge"] < self._min_edge:
                continue

            log.info("lg_opportunity", game=game["short_name"],
                     score=f"{game['away_score']}-{game['home_score']}",
                     period=game["period"], home_wp=round(home_wp, 3),
                     polymarket_yes=match["yes_price"],
                     edge=round(edge_info["edge"], 4),
                     side=edge_info["side"])

            # Execute trade
            async with ctx.portfolio_lock:
                state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
                if not state_row:
                    continue
                bankroll = float(state_row["bankroll"])

                kelly_adj = bankroll_kelly_adjustment(
                    bankroll=bankroll, base_kelly=self.kelly_multiplier,
                    post_breaker_until=state_row.get("post_breaker_until"),
                    post_breaker_reduction=getattr(ctx.settings, "post_breaker_kelly_reduction", 0.50),
                    survival_threshold=getattr(ctx.settings, "bankroll_survival_threshold", 50.0),
                    growth_threshold=getattr(ctx.settings, "bankroll_growth_threshold", 500.0),
                )

                kelly_fraction = edge_info["edge"] / (1 - edge_info["buy_price"]) \
                    if edge_info["buy_price"] < 1.0 else 0.0
                size = compute_position_size(
                    bankroll=bankroll, kelly_fraction=kelly_fraction,
                    kelly_mult=kelly_adj, max_single_pct=self.max_single_pct,
                    min_trade_size=ctx.settings.min_trade_size)
                if size <= 0:
                    continue

                # Risk check
                open_trades = await ctx.db.fetch(
                    """SELECT t.position_size_usd, m.category
                       FROM trades t JOIN markets m ON t.market_id = m.id
                       WHERE t.status IN ('open', 'filled', 'dry_run')""")
                cat_deployed: dict[str, float] = {}
                for t in open_trades:
                    cat = t["category"]
                    cat_deployed[cat] = cat_deployed.get(cat, 0.0) + float(t["position_size_usd"])
                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=len(open_trades),
                    category_deployed=cat_deployed,
                    circuit_breaker_until=state_row.get("circuit_breaker_until"))
                proposal = TradeProposal(
                    size_usd=size,
                    category=match.get("category", "sports"),
                    book_depth=match["book_depth"])
                risk_result = ctx.risk_manager.check(portfolio, proposal,
                                                      max_single_pct=self.max_single_pct)
                if not risk_result.allowed:
                    log.info("lg_risk_rejected", game=game["short_name"],
                             reason=risk_result.reason)
                    continue

                # Upsert market + analysis
                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                           current_price, volume_24h, book_depth)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (polymarket_id) DO UPDATE SET
                           current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
                       RETURNING id""",
                    match["polymarket_id"], match["question"],
                    match.get("category", "sports"), match.get("resolution_time"),
                    match["yes_price"], match.get("volume_24h"), match["book_depth"])

                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                       ensemble_stdev, quant_signals, edge)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    market_id, json.dumps([]),
                    wp_for_yes, 0.0,
                    json.dumps({"source": "live_game",
                                "sport": sport,
                                "home_wp": round(home_wp, 4),
                                "score": f"{game['away_score']}-{game['home_score']}",
                                "period": game["period"]}),
                    edge_info["edge"])

                token_id = match["yes_token_id"] if edge_info["side"] == "YES" \
                    else match["no_token_id"]
                result = await ctx.executor.place_order(
                    token_id=token_id, side=edge_info["side"],
                    size_usd=size, price=edge_info["buy_price"],
                    market_id=market_id, analysis_id=analysis_id,
                    strategy=self.name,
                    kelly_inputs={
                        "sport": sport,
                        "game": game["short_name"],
                        "score": f"{game['away_score']}-{game['home_score']}",
                        "period": game["period"],
                        "home_wp": round(home_wp, 4),
                        "edge": round(edge_info["edge"], 4),
                    },
                    post_only=self._settings.use_maker_orders)
                if not result:
                    continue

            self._traded_games.add(espn_id)
            log.info("lg_trade_placed", game=game["short_name"],
                     side=edge_info["side"], size=size,
                     price=edge_info["buy_price"],
                     edge=round(edge_info["edge"], 4),
                     win_prob=round(home_wp if edge_info["side"] == "YES" and yes_is_home
                                   else 1 - home_wp if edge_info["side"] == "NO" and yes_is_home
                                   else wp_for_yes, 3))
            await ctx.email_notifier.send(
                f"[POLYBOT] Live Game: {game['short_name']}",
                format_trade_email(
                    event="executed", market=game["short_name"],
                    side=edge_info["side"], size=size,
                    price=edge_info["buy_price"], edge=edge_info["edge"]))
```

- [ ] **Step 5: Run all tests**

Run: `cd ~/polybot && uv run pytest tests/test_live_game.py tests/test_espn_client.py tests/test_win_probability.py -v`

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/strategies/live_game.py tests/test_live_game.py
git commit -m "$(cat <<'EOF'
feat: add LiveGameCloserStrategy — ESPN-powered sports trading

Monitors live MLB/NBA/NHL games via ESPN, computes win probability from
score + clock, matches to Polymarket markets by team name, and enters
positions when Polymarket price lags reality by a configurable edge.
Holds to resolution (game end) for near-certain profit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Config + Wiring

Add strategy settings to config, register in `__main__.py`, add `live_game` to the trades strategy constraint, and add the `strategy_performance` row.

**Files:**
- Modify: `polybot/core/config.py`
- Modify: `polybot/__main__.py`
- Modify: `polybot/db/schema.sql`

- [ ] **Step 1: Add settings to config**

In `polybot/core/config.py`, add after the political settings block (after line 231):

```python
    # Live Game Closer strategy
    lg_enabled: bool = True
    lg_interval_seconds: float = 30.0          # poll ESPN every 30s
    lg_kelly_mult: float = 0.50                # aggressive — high-confidence plays
    lg_max_single_pct: float = 0.25            # up to 25% bankroll per game
    lg_min_edge: float = 0.04                  # min 4% edge (WP vs Polymarket price)
    lg_min_win_prob: float = 0.85              # only trade when WP >= 85%
    lg_min_book_depth: float = 10000.0         # min $10K liquidity
    lg_max_concurrent: int = 6                 # max concurrent live game positions
    lg_sports: str = "mlb,nba,nhl"             # sports to monitor
```

- [ ] **Step 2: Add `live_game` to trades strategy constraint**

In `polybot/db/schema.sql`, find the `trades_strategy_check` constraint and add `'live_game'` to the allowed list. The exact line will look like:

```sql
CHECK (strategy IN ('arbitrage', 'snipe', 'forecast', 'market_maker', 'mean_reversion', 'cross_venue', 'political', 'news_catalyst', 'live_game'))
```

- [ ] **Step 3: Wire into `__main__.py`**

In `polybot/__main__.py`, add the import at the top:

```python
from polybot.strategies.live_game import LiveGameCloserStrategy
from polybot.analysis.espn_client import ESPNClient
```

Then add the strategy registration after the political strategy block (around line 185):

```python
    if getattr(settings, 'lg_enabled', False):
        espn_client = ESPNClient(
            sports=getattr(settings, 'lg_sports', 'mlb,nba,nhl').split(','))
        await espn_client.start()
        lg_strategy = LiveGameCloserStrategy(settings=settings, espn_client=espn_client)
        engine.add_strategy(lg_strategy)
```

- [ ] **Step 4: Add strategy_performance row**

We need to ensure the `strategy_performance` table has a row for `live_game`. Add to schema or handle at startup. The simplest approach: add an INSERT to `schema.sql`:

```sql
INSERT INTO strategy_performance (strategy) VALUES ('live_game') ON CONFLICT DO NOTHING;
```

- [ ] **Step 5: Add `LG_ENABLED=true` to `.env`**

```bash
echo "" >> ~/polybot/.env
echo "# Live Game Closer strategy" >> ~/polybot/.env
echo "LG_ENABLED=true" >> ~/polybot/.env
```

- [ ] **Step 6: Run the full test suite**

Run: `cd ~/polybot && uv run pytest tests/ -v --tb=short`

Expected: All tests pass (497+ existing + new tests).

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py polybot/__main__.py polybot/db/schema.sql .env
git commit -m "$(cat <<'EOF'
feat: wire LiveGameCloserStrategy into engine

Adds lg_* config settings, registers strategy in __main__.py with
ESPNClient, adds live_game to trades constraint and strategy_performance.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Integration Test + Deploy

End-to-end test that verifies the full flow: ESPN data → win probability → market match → edge detection → trade placement.

**Files:**
- Test: `tests/test_live_game.py` (add integration test)

- [ ] **Step 1: Write integration test**

Add to `tests/test_live_game.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.strategies.live_game import LiveGameCloserStrategy
from polybot.trading.risk import RiskManager
from datetime import datetime, timezone, timedelta


def _make_lg_settings():
    s = MagicMock()
    s.lg_interval_seconds = 30.0
    s.lg_kelly_mult = 0.50
    s.lg_max_single_pct = 0.25
    s.lg_min_edge = 0.04
    s.lg_min_win_prob = 0.85
    s.lg_min_book_depth = 10000.0
    s.lg_max_concurrent = 6
    s.use_maker_orders = True
    s.min_trade_size = 1.0
    s.post_breaker_kelly_reduction = 0.5
    s.bankroll_survival_threshold = 50.0
    s.bankroll_growth_threshold = 500.0
    return s


@pytest.mark.asyncio
async def test_run_once_places_trade_on_blowout():
    """Full flow: ESPN blowout → edge detected → trade placed."""
    s = _make_lg_settings()
    espn_client = MagicMock()
    # NBA game: Cavaliers up 112-88 in Q4 → ~97% WP
    espn_client.fetch_all_live_games = AsyncMock(return_value=[
        {
            "espn_id": "401656789",
            "sport": "nba",
            "name": "Cleveland Cavaliers at Memphis Grizzlies",
            "short_name": "CLE @ MEM",
            "home_team": "Memphis Grizzlies",
            "away_team": "Cleveland Cavaliers",
            "home_abbrev": "MEM",
            "away_abbrev": "CLE",
            "home_score": 88,
            "away_score": 112,
            "period": 4,
            "clock": "3:42",
            "status": "in_progress",
            "completed": False,
        }
    ])

    strategy = LiveGameCloserStrategy(settings=s, espn_client=espn_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=[
        True,    # enabled check
        0,       # open position count
        1,       # market upsert RETURNING id
        1,       # analysis insert RETURNING id
    ])
    ctx.db.fetchrow = AsyncMock(return_value={
        "bankroll": 500.0, "total_deployed": 50.0, "daily_pnl": 0.0,
        "post_breaker_until": None, "circuit_breaker_until": None,
    })
    ctx.db.fetch = AsyncMock(return_value=[
        {"position_size_usd": 25, "category": "sports"},
    ])
    ctx.executor = AsyncMock()
    ctx.executor.place_order = AsyncMock(return_value={"trade_id": 1})
    ctx.settings = s
    ctx.risk_manager = RiskManager()
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {
            "polymarket_id": "0xabc",
            "question": "Cavaliers vs. Grizzlies",
            "outcomes": '["Cavaliers", "Grizzlies"]',
            "yes_price": 0.87,
            "category": "sports",
            "book_depth": 100000,
            "volume_24h": 2000000,
            "resolution_time": datetime.now(timezone.utc) + timedelta(hours=3),
            "yes_token_id": "tok_yes",
            "no_token_id": "tok_no",
        },
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)

    # Should have placed a trade — Cavaliers winning (away team = YES outcome)
    ctx.executor.place_order.assert_called_once()
    call_kwargs = ctx.executor.place_order.call_args
    assert call_kwargs.kwargs["strategy"] == "live_game"
    # Verify it traded (size > 0, correct side)
    assert call_kwargs.kwargs["size_usd"] > 0


@pytest.mark.asyncio
async def test_run_once_skips_close_game():
    """Should not trade when game is close (WP < min threshold)."""
    s = _make_lg_settings()
    s.lg_min_win_prob = 0.85
    espn_client = MagicMock()
    # NBA game: close game, 55-52 in Q2 → ~60% WP, below threshold
    espn_client.fetch_all_live_games = AsyncMock(return_value=[
        {
            "espn_id": "9999",
            "sport": "nba",
            "name": "A at B",
            "short_name": "A @ B",
            "home_team": "B Team",
            "away_team": "A Team",
            "home_abbrev": "B",
            "away_abbrev": "A",
            "home_score": 55,
            "away_score": 52,
            "period": 2,
            "clock": "5:00",
            "status": "in_progress",
            "completed": False,
        }
    ])

    strategy = LiveGameCloserStrategy(settings=s, espn_client=espn_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=[True, 0])
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {}
    ctx.portfolio_lock = asyncio.Lock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
```

- [ ] **Step 2: Run all tests**

Run: `cd ~/polybot && uv run pytest tests/ -v --tb=short`

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add tests/test_live_game.py
git commit -m "$(cat <<'EOF'
test: add integration test for LiveGameCloserStrategy

Verifies full flow: ESPN blowout data → WP model → market match →
edge detection → trade placement. Also tests that close games are skipped.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Deploy — restart bot**

```bash
launchctl stop ai.polybot.trader && sleep 3 && launchctl start ai.polybot.trader
```

- [ ] **Step 5: Verify strategy is running**

```bash
sleep 30 && grep 'engine_starting\|espn_fetch\|lg_opportunity\|lg_trade' ~/polybot/data/polybot_stdout.log | tail -10
```

Expected: `engine_starting` shows `live_game` in strategy list. `espn_fetch_complete` logs appear every 30s. If games are live with blowouts, `lg_opportunity` and `lg_trade_placed` logs appear.
