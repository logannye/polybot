# Expand Sports Coverage — NCAAB, Soccer, Spreads/O-U

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Triple the Live Game Closer's trading opportunities by adding NCAAB, soccer (UCL/EPL/La Liga/Bundesliga/MLS), and spread/over-under market matching to the existing strategy.

**Architecture:** Three surgical changes to existing files: (1) add 6 new ESPN sport URLs, (2) add NCAAB + soccer win probability models, (3) teach the market matcher to find spread/O-U markets alongside moneylines. Plus config update for the expanded sport list.

**Tech Stack:** Python 3.13, asyncio, aiohttp, pytest, structlog. Same ESPN scoreboard API (free, no auth).

---

## File Structure (modifications only)

| File | Change |
|------|--------|
| `polybot/analysis/espn_client.py` | Add 6 sport URLs to `SPORT_URLS` dict |
| `polybot/analysis/win_probability.py` | Add `ncaab` + `soccer` models to `TOTAL_PERIODS` and `compute_win_probability` |
| `polybot/strategies/live_game.py` | Add `match_game_to_all_markets()` returning multiple matches (moneyline + spread + O/U) |
| `polybot/core/config.py` | Expand `lg_sports` default |
| `tests/test_espn_client.py` | Add tests for new sports |
| `tests/test_win_probability.py` | Add tests for NCAAB + soccer models |
| `tests/test_live_game.py` | Add tests for spread/O-U matching |

---

### Task 1: Expand ESPN Sport URLs

Add NCAAB, UCL, EPL, La Liga, Bundesliga, and MLS to the ESPN client.

**Files:**
- Modify: `polybot/analysis/espn_client.py:8-12`
- Test: `tests/test_espn_client.py`

- [ ] **Step 1: Write failing test for new sport URLs**

Add to `tests/test_espn_client.py`:

```python
from polybot.analysis.espn_client import SPORT_URLS

class TestSportURLs:
    def test_original_sports_present(self):
        assert "mlb" in SPORT_URLS
        assert "nba" in SPORT_URLS
        assert "nhl" in SPORT_URLS

    def test_ncaab_present(self):
        assert "ncaab" in SPORT_URLS
        assert "college-basketball" in SPORT_URLS["ncaab"]

    def test_soccer_leagues_present(self):
        assert "ucl" in SPORT_URLS
        assert "epl" in SPORT_URLS
        assert "laliga" in SPORT_URLS
        assert "bundesliga" in SPORT_URLS
        assert "mls" in SPORT_URLS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_espn_client.py::TestSportURLs -v`

Expected: FAIL — `ncaab` not in SPORT_URLS.

- [ ] **Step 3: Add sport URLs**

In `polybot/analysis/espn_client.py`, expand `SPORT_URLS` (lines 8-12):

```python
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
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_espn_client.py -v`

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/analysis/espn_client.py tests/test_espn_client.py
git commit -m "$(cat <<'EOF'
feat: add NCAAB and soccer leagues to ESPN client

Adds 6 new sport feeds: NCAAB (NCAA Tournament), UCL, EPL, La Liga,
Bundesliga, and MLS. All use the same free ESPN scoreboard API.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add NCAAB + Soccer Win Probability Models

NCAAB uses the same model as NBA (basketball with 2 halves). Soccer uses a different model (goals are rare, clock runs 0→90).

**Files:**
- Modify: `polybot/analysis/win_probability.py`
- Test: `tests/test_win_probability.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_win_probability.py`:

```python
class TestNCAABWinProbability:
    def test_blowout_second_half(self):
        """Up 20 in second half → near-certain."""
        wp = compute_win_probability(sport="ncaab", lead=20, period=2, total_periods=2)
        assert wp >= 0.97

    def test_close_game_second_half(self):
        """Up 3 in second half → slight favorite."""
        wp = compute_win_probability(sport="ncaab", lead=3, period=2, total_periods=2)
        assert 0.55 <= wp <= 0.75

    def test_halftime_lead(self):
        """Up 10 at halftime → solid favorite."""
        wp = compute_win_probability(sport="ncaab", lead=10, period=1, total_periods=2)
        assert 0.65 <= wp <= 0.85


class TestSoccerWinProbability:
    def test_two_goal_lead_second_half(self):
        """Up 2 goals in second half → strong favorite."""
        wp = compute_win_probability(sport="soccer", lead=2, period=2, total_periods=2)
        assert wp >= 0.90

    def test_one_goal_lead_second_half(self):
        """Up 1 goal in second half → moderate favorite."""
        wp = compute_win_probability(sport="soccer", lead=1, period=2, total_periods=2)
        assert 0.65 <= wp <= 0.85

    def test_one_goal_lead_first_half(self):
        """Up 1 goal in first half → slight favorite."""
        wp = compute_win_probability(sport="soccer", lead=1, period=1, total_periods=2)
        assert 0.55 <= wp <= 0.75

    def test_tied_game(self):
        wp = compute_win_probability(sport="soccer", lead=0, period=1, total_periods=2)
        assert 0.45 <= wp <= 0.55
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_win_probability.py::TestNCAABWinProbability tests/test_win_probability.py::TestSoccerWinProbability -v`

Expected: FAIL — `ncaab` and `soccer` return None.

- [ ] **Step 3: Add models**

In `polybot/analysis/win_probability.py`:

1. Update `TOTAL_PERIODS`:
```python
TOTAL_PERIODS: dict[str, int] = {
    "nba": 4,
    "mlb": 9,
    "nhl": 3,
    "ncaab": 2,
    "soccer": 2,
    "ucl": 2,
    "epl": 2,
    "laliga": 2,
    "bundesliga": 2,
    "mls": 2,
}
```

2. Update `compute_win_probability` to accept the new sports. In the sport check (line 40), change from:
```python
    if sport_key not in ("nba", "mlb", "nhl"):
        return None
```
To:
```python
    if sport_key not in _SPORT_MODELS:
        return None
```

3. Add the two new model functions:

```python
def _ncaab_win_prob(lead: int, game_progress: float) -> float:
    """NCAAB model: similar to NBA but with 2 halves and higher variance.

    College basketball has more upsets than NBA, so leads are slightly
    less decisive. Uses same formula structure as NBA with slightly
    lower per-point coefficient.
    """
    sign = 1 if lead >= 0 else -1
    abs_lead = abs(lead)
    per_point = 0.008 + 0.022 * game_progress
    prob = 0.5 + sign * abs_lead * per_point
    return max(0.01, min(0.99, prob))


def _soccer_win_prob(lead: int, game_progress: float) -> float:
    """Soccer model: goals are rare and decisive.

    Each goal of lead is worth 15% at kickoff → 30% at full time.
    A 2-goal lead in the second half is very hard to overcome.
    """
    sign = 1 if lead >= 0 else -1
    abs_lead = abs(lead)
    per_goal = 0.15 + 0.15 * game_progress
    prob = 0.5 + sign * abs_lead * per_goal
    return max(0.01, min(0.99, prob))
```

4. Add a `_SPORT_MODELS` dispatch dict and update the main function to use it:

```python
_SPORT_MODELS = {
    "nba": _nba_win_prob,
    "mlb": _mlb_win_prob,
    "nhl": _nhl_win_prob,
    "ncaab": _ncaab_win_prob,
    "soccer": _soccer_win_prob,
    "ucl": _soccer_win_prob,
    "epl": _soccer_win_prob,
    "laliga": _soccer_win_prob,
    "bundesliga": _soccer_win_prob,
    "mls": _soccer_win_prob,
}
```

Then in `compute_win_probability`, replace the `if/elif/else` dispatch block with:
```python
    model = _SPORT_MODELS.get(sport_key)
    if model is None:
        return None
    return model(lead, game_progress)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_win_probability.py -v`

Expected: All pass (old + new tests). Adjust coefficients if bounds fail.

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/analysis/win_probability.py tests/test_win_probability.py
git commit -m "$(cat <<'EOF'
feat: add NCAAB and soccer win probability models

NCAAB uses basketball-like model with higher variance. Soccer uses
goal-based model (15-30% per goal). All soccer leagues (UCL, EPL,
La Liga, Bundesliga, MLS) share the same model. Refactored dispatch
to use _SPORT_MODELS dict.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add Spread/O-U Market Matching

Teach the strategy to find spread and over/under markets for the same game, not just moneyline. A single NBA blowout can now generate 3 trades: moneyline + spread + O/U.

**Files:**
- Modify: `polybot/strategies/live_game.py`
- Test: `tests/test_live_game.py`

- [ ] **Step 1: Write failing test for multi-market matching**

Add to `tests/test_live_game.py`:

```python
from polybot.strategies.live_game import match_game_to_all_markets


def test_match_finds_moneyline_and_spread():
    """Should find both the moneyline and spread markets for the same game."""
    game = {
        "home_team": "Memphis Grizzlies",
        "away_team": "Cleveland Cavaliers",
        "sport": "nba",
    }
    price_cache = {
        "0xabc": {
            "polymarket_id": "0xabc",
            "question": "Cavaliers vs. Grizzlies",
            "outcomes": '["Cavaliers", "Grizzlies"]',
            "yes_price": 0.87,
            "yes_token_id": "tok1", "no_token_id": "tok2",
            "book_depth": 50000, "volume_24h": 1000000,
            "category": "sports",
        },
        "0xdef": {
            "polymarket_id": "0xdef",
            "question": "Spread: Cavaliers (-13.5)",
            "outcomes": '["Cavaliers -13.5", "Grizzlies +13.5"]',
            "yes_price": 0.55,
            "yes_token_id": "tok3", "no_token_id": "tok4",
            "book_depth": 30000, "volume_24h": 500000,
            "category": "sports",
        },
        "0xghi": {
            "polymarket_id": "0xghi",
            "question": "Cavaliers vs. Grizzlies: O/U 231.5",
            "outcomes": '["Over 231.5", "Under 231.5"]',
            "yes_price": 0.50,
            "yes_token_id": "tok5", "no_token_id": "tok6",
            "book_depth": 20000, "volume_24h": 400000,
            "category": "sports",
        },
        "0xzzz": {
            "polymarket_id": "0xzzz",
            "question": "Will Trump win 2028?",
            "yes_price": 0.30,
        },
    }
    matches = match_game_to_all_markets(game, price_cache)
    assert len(matches) >= 2  # at least moneyline + spread (O/U may or may not match both teams)
    pids = {m["polymarket_id"] for m in matches}
    assert "0xabc" in pids  # moneyline
    assert "0xdef" in pids  # spread


def test_match_all_returns_empty_on_no_match():
    game = {"home_team": "Boston Celtics", "away_team": "Miami Heat", "sport": "nba"}
    matches = match_game_to_all_markets(game, {"0x1": {"question": "unrelated", "polymarket_id": "0x1"}})
    assert matches == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_live_game.py::test_match_finds_moneyline_and_spread -v`

Expected: FAIL — `match_game_to_all_markets` doesn't exist.

- [ ] **Step 3: Implement `match_game_to_all_markets`**

In `polybot/strategies/live_game.py`, add a new function after `match_game_to_market`:

```python
def match_game_to_all_markets(game: dict, price_cache: dict[str, dict]) -> list[dict]:
    """Match an ESPN game to ALL related Polymarket markets.

    Finds moneyline, spread, and O/U markets by checking if both team
    names appear in the question. Returns a list of matched market dicts
    (same shape as match_game_to_market output).
    """
    home_team = game.get("home_team", "")
    away_team = game.get("away_team", "")

    if not home_team or not away_team:
        return []

    matches = []
    for pid, market in price_cache.items():
        question = market.get("question", "")
        q_lower = question.lower()

        # At least one team must appear in the question
        home_match = _team_matches_question(home_team, q_lower)
        away_match = _team_matches_question(away_team, q_lower)

        if not (home_match and away_match):
            # For spreads like "Spread: Cavaliers (-13.5)", only one team name appears
            # Also check for "spread" or "o/u" keywords with at least one team
            is_derivative = any(kw in q_lower for kw in ["spread:", "o/u ", "over/under"])
            if not (is_derivative and (home_match or away_match)):
                continue

        outcomes = _parse_outcomes(market.get("outcomes"))

        home_outcome = None
        away_outcome = None

        if len(outcomes) >= 2:
            for outcome in outcomes:
                outcome_lower = outcome.lower()
                if _team_matches_question(home_team, outcome_lower):
                    home_outcome = outcome
                elif _team_matches_question(away_team, outcome_lower):
                    away_outcome = outcome

        if home_outcome is None or away_outcome is None:
            if len(outcomes) >= 2:
                home_outcome = outcomes[0]
                away_outcome = outcomes[1]
            else:
                home_outcome = "YES"
                away_outcome = "NO"

        matches.append({
            "polymarket_id": pid,
            "question": question,
            "yes_price": market.get("yes_price", 0.0),
            "yes_token_id": market.get("yes_token_id", ""),
            "no_token_id": market.get("no_token_id", ""),
            "home_outcome": home_outcome,
            "away_outcome": away_outcome,
            "book_depth": market.get("book_depth", 0.0),
            "volume_24h": market.get("volume_24h", 0.0),
            "category": market.get("category", "unknown"),
            "resolution_time": market.get("resolution_time"),
        })

    return matches
```

- [ ] **Step 4: Update `run_once` to use `match_game_to_all_markets`**

In `LiveGameCloserStrategy.run_once()`, replace the single `match_game_to_market` call (around line 238) with a loop over `match_game_to_all_markets`. Change the dedup key from `espn_id` to `espn_id + polymarket_id` so we can trade multiple markets for the same game.

Replace:
```python
            # 5c. Match to Polymarket market
            matched = match_game_to_market(game, price_cache)
            if matched is None:
                continue
```

With:
```python
            # 5c. Match to ALL Polymarket markets for this game
            all_matched = match_game_to_all_markets(game, price_cache)
            if not all_matched:
                continue

        for matched in all_matched:
            trade_key = f"{espn_id}:{matched['polymarket_id']}"
            if trade_key in self._traded_games:
                continue
```

And change `self._traded_games.add(espn_id)` (line 398) to `self._traded_games.add(trade_key)`.

**IMPORTANT**: The indentation needs to change — the `for matched in all_matched:` loop replaces the single-match flow. Everything from the book_depth check through trade execution (lines 242-419) goes inside this inner loop. The outer `for game in games:` loop now only handles ESPN game filtering + win probability.

- [ ] **Step 5: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_live_game.py tests/test_win_probability.py tests/test_espn_client.py -v`

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/strategies/live_game.py tests/test_live_game.py
git commit -m "$(cat <<'EOF'
feat: match multiple markets per game (moneyline + spread + O/U)

A single NBA blowout can now generate trades on 3 markets: moneyline,
spread, and over/under. match_game_to_all_markets finds all related
Polymarket markets by team name. Dedup key changed from ESPN ID to
ESPN ID + Polymarket ID.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Update Config + Deploy

Expand the default sport list and restart.

**Files:**
- Modify: `polybot/core/config.py`

- [ ] **Step 1: Update config**

In `polybot/core/config.py`, change the `lg_sports` default from:
```python
    lg_sports: str = "mlb,nba,nhl"
```
To:
```python
    lg_sports: str = "mlb,nba,nhl,ncaab,ucl,epl,laliga,bundesliga,mls"
```

- [ ] **Step 2: Update .env**

```bash
# Update the .env to match (or it will override with old value)
cd ~/polybot && sed -i '' 's/^LG_SPORTS=.*//' .env 2>/dev/null
echo "LG_SPORTS=mlb,nba,nhl,ncaab,ucl,epl,laliga,bundesliga,mls" >> .env
```

- [ ] **Step 3: Run full test suite**

Run: `cd ~/polybot && uv run pytest tests/ -v --tb=short`

Expected: All pass.

- [ ] **Step 4: Commit and push**

```bash
cd ~/polybot && git add polybot/core/config.py .env
git commit -m "$(cat <<'EOF'
feat: expand lg_sports to include NCAAB and soccer leagues

Default now covers MLB, NBA, NHL, NCAAB, UCL, EPL, La Liga,
Bundesliga, and MLS — 9 sports feeds polling every 30 seconds.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

- [ ] **Step 5: Restart bot**

```bash
launchctl stop ai.polybot.trader 2>/dev/null; sleep 3; launchctl start ai.polybot.trader
```

- [ ] **Step 6: Verify expanded coverage**

```bash
sleep 30 && grep 'espn_client.fetch_all_complete\|lg_cycle_complete' ~/polybot/data/polybot_stdout.log | tail -5
```

Expected: `total_games` should be higher than before (was 21 with 3 sports — should be 30+ with 9 sports).
