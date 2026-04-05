# Cross-Venue Arbitrage Strategy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new trading strategy that compares Polymarket prices against sportsbook consensus (via The Odds API) and trades when Polymarket diverges significantly — an entirely new, uncorrelated alpha source.

**Architecture:** Three new files: (1) `OddsClient` fetches and parses The Odds API responses, (2) `find_divergences()` compares sportsbook consensus to Polymarket and returns actionable divergences, (3) `CrossVenueStrategy` is a standard Strategy subclass that runs every 5 minutes, calls the odds client, finds divergences, and enters trades. The Odds API conveniently includes Polymarket as a bookmaker in its `us_ex` region, so event matching between venues is handled upstream.

**Tech Stack:** Python 3.13, aiohttp, pytest, asyncio, The Odds API (free tier: 500 credits/month)

**Prerequisite:** Sign up at https://the-odds-api.com/ for a free API key. Add `ODDS_API_KEY=your_key` to `.env`.

---

## How It Works

1. Every 5 minutes, fetch odds for NBA, NHL, and soccer from The Odds API
2. For each event, average the implied probabilities across FanDuel, DraftKings, BetMGM (the "sportsbook consensus")
3. Compare the consensus to the Polymarket price for the same event
4. If Polymarket underprices an outcome by ≥3% vs consensus: buy YES on Polymarket
5. If Polymarket overprices an outcome by ≥3% vs consensus: buy NO on Polymarket
6. Size using Kelly criterion based on divergence magnitude
7. Hold until convergence (exit via the existing position manager TP/SL)

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `polybot/analysis/odds_client.py` | Create | Fetch + parse The Odds API responses |
| `polybot/strategies/cross_venue.py` | Create | CrossVenueStrategy — find divergences + enter trades |
| `polybot/core/config.py` | Modify | Add cv_* config fields + odds_api_key |
| `polybot/__main__.py` | Modify | Wire CrossVenueStrategy into engine |
| `tests/test_odds_client.py` | Create | Tests for odds parsing + divergence detection |
| `tests/test_cross_venue.py` | Create | Tests for strategy behavior |

---

### Task 1: Odds conversion pure functions + OddsClient

Create the odds client with conversion functions and API fetching.

**Files:**
- Create: `polybot/analysis/odds_client.py`
- Create: `tests/test_odds_client.py`

- [ ] **Step 1: Write tests for odds conversion**

Create `tests/test_odds_client.py`:

```python
import pytest
from polybot.analysis.odds_client import american_to_prob, devig


class TestAmericanToProb:
    def test_negative_odds(self):
        # -150 means bet $150 to win $100 → 60% implied
        assert american_to_prob(-150) == pytest.approx(0.60, abs=0.001)

    def test_positive_odds(self):
        # +200 means bet $100 to win $200 → 33.3% implied
        assert american_to_prob(200) == pytest.approx(0.333, abs=0.001)

    def test_even_odds(self):
        # +100 = 50%
        assert american_to_prob(100) == pytest.approx(0.50, abs=0.001)

    def test_heavy_favorite(self):
        # -500 = 83.3%
        assert american_to_prob(-500) == pytest.approx(0.833, abs=0.001)

    def test_heavy_underdog(self):
        # +500 = 16.7%
        assert american_to_prob(500) == pytest.approx(0.167, abs=0.001)


class TestDevig:
    def test_removes_standard_vig(self):
        # -110/-110 = 52.4%/52.4% (sum 104.8%) → devigged 50%/50%
        p_a = american_to_prob(-110)
        p_b = american_to_prob(-110)
        fair_a, fair_b = devig(p_a, p_b)
        assert fair_a == pytest.approx(0.50, abs=0.01)
        assert fair_b == pytest.approx(0.50, abs=0.01)
        assert fair_a + fair_b == pytest.approx(1.0, abs=0.001)

    def test_asymmetric_vig(self):
        # -200/+170 → devigged should be closer to 65/35
        p_a = american_to_prob(-200)  # 0.667
        p_b = american_to_prob(170)   # 0.370
        fair_a, fair_b = devig(p_a, p_b)
        assert fair_a + fair_b == pytest.approx(1.0, abs=0.001)
        assert fair_a > 0.60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_odds_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement the pure functions + OddsClient**

Create `polybot/analysis/odds_client.py`:

```python
"""Client for The Odds API — fetches sportsbook odds for cross-venue comparison."""

import aiohttp
import structlog

log = structlog.get_logger()

# Sportsbooks to use for consensus (major US books)
CONSENSUS_BOOKS = {"fanduel", "draftkings", "betmgm", "williamhill_us", "bovada"}

# Sports that overlap with Polymarket
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
    """Average implied probabilities across major sportsbooks for each outcome.

    Args:
        bookmakers: List of bookmaker dicts from The Odds API response.

    Returns:
        Dict mapping outcome name → fair probability, or None if insufficient data.
    """
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

    # Average across books
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
            # Convert to probabilities
            prices = {}
            for o in outcomes:
                prices[o["name"]] = american_to_prob(o["price"])
            return prices
    return None


def find_divergences(
    events: list[dict],
    min_divergence: float = 0.03,
) -> list[dict]:
    """Compare sportsbook consensus to Polymarket prices for all events.

    Returns list of actionable divergences with fields:
    event_id, sport, home_team, away_team, outcome_name,
    consensus_prob, polymarket_prob, divergence, side.
    """
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

    def __init__(self, api_key: str, sports: list[str] | None = None):
        self._api_key = api_key
        self._sports = sports or DEFAULT_SPORTS
        self._session: aiohttp.ClientSession | None = None
        self._credits_remaining: int | None = None

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()

    async def fetch_odds(self, sport_key: str) -> list[dict]:
        """Fetch odds for a sport from The Odds API.

        Uses regions=us,us_ex to get both sportsbook and Polymarket prices.
        Costs 2 credits per call (1 market × 2 regions).
        """
        if not self._session or not self._api_key:
            return []

        url = f"{self.BASE_URL}/sports/{sport_key}/odds/"
        params = {
            "apiKey": self._api_key,
            "regions": "us,us_ex",
            "markets": "h2h",
            "oddsFormat": "american",
        }

        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                # Track credit usage from headers
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
        """Fetch odds for all configured sports. Returns combined event list."""
        all_events = []
        for sport in self._sports:
            events = await self.fetch_odds(sport)
            all_events.extend(events)
        return all_events

    @property
    def credits_remaining(self) -> int | None:
        return self._credits_remaining
```

- [ ] **Step 4: Add tests for compute_consensus and find_divergences**

Append to `tests/test_odds_client.py`:

```python
from polybot.analysis.odds_client import compute_consensus, find_polymarket_prices, find_divergences


class TestComputeConsensus:
    def test_averages_across_books(self):
        bookmakers = [
            {
                "key": "fanduel",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -150},
                    {"name": "Team B", "price": 130},
                ]}],
            },
            {
                "key": "draftkings",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -160},
                    {"name": "Team B", "price": 140},
                ]}],
            },
        ]
        result = compute_consensus(bookmakers)
        assert result is not None
        assert "Team A" in result
        assert "Team B" in result
        assert result["Team A"] + result["Team B"] == pytest.approx(1.0, abs=0.02)
        assert result["Team A"] > 0.55  # favorite

    def test_ignores_non_consensus_books(self):
        bookmakers = [
            {
                "key": "polymarket",  # not in CONSENSUS_BOOKS
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Team A", "price": -200},
                    {"name": "Team B", "price": 170},
                ]}],
            },
        ]
        result = compute_consensus(bookmakers)
        assert result is None

    def test_returns_none_with_no_data(self):
        assert compute_consensus([]) is None


class TestFindDivergences:
    def test_detects_underpriced_polymarket(self):
        events = [{
            "id": "evt1", "sport_key": "basketball_nba",
            "home_team": "Warriors", "away_team": "Lakers",
            "commence_time": "2026-04-05T01:00:00Z",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Warriors", "price": -200},  # ~66.7% → devigged ~64%
                        {"name": "Lakers", "price": 170},     # ~37.0% → devigged ~36%
                    ]}],
                },
                {
                    "key": "draftkings",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Warriors", "price": -190},
                        {"name": "Lakers", "price": 165},
                    ]}],
                },
                {
                    "key": "polymarket",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Warriors", "price": -130},  # ~56.5% — underpriced vs ~64% consensus
                        {"name": "Lakers", "price": 110},
                    ]}],
                },
            ],
        }]
        divs = find_divergences(events, min_divergence=0.03)
        assert len(divs) >= 1
        warriors_div = next(d for d in divs if d["outcome_name"] == "Warriors")
        assert warriors_div["side"] == "YES"  # consensus > polymarket → buy YES
        assert warriors_div["divergence"] > 0.03

    def test_ignores_small_divergence(self):
        events = [{
            "id": "evt2", "sport_key": "basketball_nba",
            "home_team": "A", "away_team": "B",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": -150},
                        {"name": "B", "price": 130},
                    ]}],
                },
                {
                    "key": "polymarket",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": -145},  # very close to consensus
                        {"name": "B", "price": 125},
                    ]}],
                },
            ],
        }]
        divs = find_divergences(events, min_divergence=0.03)
        assert len(divs) == 0

    def test_skips_events_without_polymarket(self):
        events = [{
            "id": "evt3", "sport_key": "basketball_nba",
            "home_team": "A", "away_team": "B",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": -200},
                        {"name": "B", "price": 170},
                    ]}],
                },
                # No polymarket bookmaker
            ],
        }]
        divs = find_divergences(events, min_divergence=0.03)
        assert len(divs) == 0
```

- [ ] **Step 5: Run all tests**

Run: `cd ~/polybot && uv run pytest tests/test_odds_client.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run full suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
cd ~/polybot
git add polybot/analysis/odds_client.py tests/test_odds_client.py
git commit -m "feat: add OddsClient + cross-venue divergence detection

OddsClient fetches from The Odds API (sportsbook + Polymarket prices
in one call). find_divergences() compares sportsbook consensus to
Polymarket prices and returns actionable divergences above a threshold.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: CrossVenueStrategy + config + wiring

Create the strategy, add config fields, and wire everything into the engine.

**Files:**
- Create: `polybot/strategies/cross_venue.py`
- Create: `tests/test_cross_venue.py`
- Modify: `polybot/core/config.py`
- Modify: `polybot/__main__.py`

- [ ] **Step 1: Add config fields**

In `polybot/core/config.py`, after the `mr_history_concurrency` line (end of MR block), add:

```python

    # Cross-venue arbitrage strategy
    cv_enabled: bool = False
    cv_interval_seconds: float = 300.0   # 5 minutes between scans
    cv_kelly_mult: float = 0.25
    cv_max_single_pct: float = 0.15
    cv_min_divergence: float = 0.03      # 3% minimum divergence to trade
    cv_sports: str = "basketball_nba,icehockey_nhl,soccer_epl"
    cv_cooldown_hours: float = 12.0      # per-event cooldown
    odds_api_key: str = ""
```

- [ ] **Step 2: Write tests for the strategy**

Create `tests/test_cross_venue.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from polybot.strategies.cross_venue import CrossVenueStrategy


def _make_settings():
    s = MagicMock()
    s.cv_interval_seconds = 300.0
    s.cv_kelly_mult = 0.25
    s.cv_max_single_pct = 0.15
    s.cv_min_divergence = 0.03
    s.cv_cooldown_hours = 12.0
    s.use_maker_orders = True
    s.min_trade_size = 1.0
    s.post_breaker_kelly_reduction = 0.5
    s.bankroll_survival_threshold = 50.0
    s.bankroll_growth_threshold = 500.0
    return s


class TestCrossVenueInit:
    def test_reads_settings(self):
        s = _make_settings()
        odds_client = MagicMock()
        strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)
        assert strategy.name == "cross_venue"
        assert strategy.interval_seconds == 300.0
        assert strategy._min_divergence == 0.03


@pytest.mark.asyncio
async def test_run_once_skips_when_no_divergences():
    """Should not place trades when odds client returns no divergences."""
    s = _make_settings()
    odds_client = MagicMock()
    odds_client.fetch_all_sports = AsyncMock(return_value=[])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)  # enabled
    ctx.db.fetchrow = AsyncMock(return_value={"bankroll": 500.0, "total_deployed": 0,
                                                "daily_pnl": 0, "post_breaker_until": None})
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {}

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_cross_venue.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Create CrossVenueStrategy**

Create `polybot/strategies/cross_venue.py`:

```python
"""Cross-venue arbitrage strategy.

Compares Polymarket prices against sportsbook consensus (via The Odds API)
and trades when Polymarket diverges significantly. The sportsbook consensus
is backed by much deeper liquidity and more sophisticated pricing models,
making it a reliable estimate of "true" probability.
"""

import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.trading.kelly import compute_position_size
from polybot.analysis.odds_client import find_divergences
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


class CrossVenueStrategy(Strategy):
    name = "cross_venue"

    def __init__(self, settings, odds_client):
        self.interval_seconds = settings.cv_interval_seconds
        self.kelly_multiplier = settings.cv_kelly_mult
        self.max_single_pct = settings.cv_max_single_pct
        self._min_divergence = settings.cv_min_divergence
        self._cooldown_hours = settings.cv_cooldown_hours
        self._odds_client = odds_client
        self._settings = settings
        # Track recently traded events to avoid re-entry
        self._traded_events: dict[str, datetime] = {}

    async def run_once(self, ctx: TradingContext) -> None:
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name)
        if enabled is False:
            return

        # Fetch odds from The Odds API (all configured sports)
        all_events = await self._odds_client.fetch_all_sports()
        if not all_events:
            return

        # Find divergences
        divergences = find_divergences(all_events, min_divergence=self._min_divergence)
        if not divergences:
            log.debug("cv_no_divergences", events_checked=len(all_events))
            return

        log.info("cv_divergences_found", count=len(divergences),
                 events_checked=len(all_events))

        now = datetime.now(timezone.utc)

        # Match divergences to Polymarket markets and trade
        price_cache = ctx.scanner.get_all_cached_prices()

        for div in divergences:
            event_id = div["event_id"]

            # Cooldown check
            if event_id in self._traded_events:
                elapsed = (now - self._traded_events[event_id]).total_seconds() / 3600
                if elapsed < self._cooldown_hours:
                    continue

            # Find matching Polymarket market
            # Search by team/outcome name in the scanner cache
            target_name = div["outcome_name"].lower()
            matching_market = None
            for m in price_cache.values():
                q = m.get("question", "").lower()
                if target_name in q:
                    matching_market = m
                    break

            if not matching_market:
                log.debug("cv_no_matching_market", outcome=div["outcome_name"])
                continue

            side = div["side"]
            divergence = abs(div["divergence"])
            buy_price = matching_market["yes_price"] if side == "YES" else (1 - matching_market["yes_price"])

            # Kelly sizing: use divergence as edge estimate
            kelly_fraction = divergence / (1 - buy_price) if buy_price < 1.0 else 0.0

            async with ctx.portfolio_lock:
                state_row = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
                if not state_row:
                    continue
                bankroll = float(state_row["bankroll"])
                kelly_adj = bankroll_kelly_adjustment(
                    bankroll=bankroll, base_kelly=self.kelly_multiplier,
                    post_breaker_until=state_row.get("post_breaker_until"),
                    post_breaker_reduction=ctx.settings.post_breaker_kelly_reduction,
                    survival_threshold=ctx.settings.bankroll_survival_threshold,
                    growth_threshold=ctx.settings.bankroll_growth_threshold,
                )
                size = compute_position_size(
                    bankroll=bankroll, kelly_fraction=kelly_fraction,
                    kelly_mult=kelly_adj, confidence_mult=1.0,
                    max_single_pct=self.max_single_pct,
                    min_trade_size=ctx.settings.min_trade_size)
                if size <= 0:
                    continue

                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=0, category_deployed={},
                    circuit_breaker_until=state_row.get("circuit_breaker_until"))
                proposal = TradeProposal(
                    size_usd=size,
                    category=matching_market.get("category", "unknown"),
                    book_depth=matching_market.get("book_depth", 1000.0))
                risk_result = ctx.risk_manager.check(portfolio, proposal,
                                                      max_single_pct=self.max_single_pct)
                if not risk_result.allowed:
                    log.info("cv_risk_rejected", outcome=div["outcome_name"],
                             reason=risk_result.reason)
                    continue

                pid = matching_market["polymarket_id"]

                # Upsert market
                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                           current_price, volume_24h, book_depth)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (polymarket_id) DO UPDATE SET
                           current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
                       RETURNING id""",
                    pid, matching_market["question"],
                    matching_market.get("category", "unknown"),
                    matching_market.get("resolution_time"),
                    matching_market["yes_price"],
                    matching_market.get("volume_24h"),
                    matching_market.get("book_depth"))

                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                       ensemble_stdev, quant_signals, edge)
                       VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                    market_id, json.dumps([]),
                    div["consensus_prob"], 0.0,
                    json.dumps({"source": "cross_venue", "sportsbook_consensus": div["consensus_prob"],
                                "polymarket_prob": div["polymarket_prob"]}),
                    divergence)

                token_id = matching_market.get("yes_token_id", "") if side == "YES" else matching_market.get("no_token_id", "")
                result = await ctx.executor.place_order(
                    token_id=token_id, side=side, size_usd=size,
                    price=buy_price, market_id=market_id,
                    analysis_id=analysis_id, strategy=self.name,
                    kelly_inputs={
                        "consensus_prob": div["consensus_prob"],
                        "polymarket_prob": div["polymarket_prob"],
                        "divergence": div["divergence"],
                        "sport": div["sport"],
                        "outcome": div["outcome_name"],
                    },
                    post_only=self._settings.use_maker_orders)
                if not result:
                    continue

            self._traded_events[event_id] = now
            log.info("cv_trade", outcome=div["outcome_name"], side=side,
                     divergence=round(divergence, 4), size=size,
                     consensus=div["consensus_prob"], polymarket=div["polymarket_prob"])
            await ctx.email_notifier.send(
                f"[POLYBOT] Cross-venue: {div['outcome_name']}",
                format_trade_email(event="executed",
                                   market=f"{div['outcome_name']} ({div['sport']})",
                                   side=side, size=size, price=buy_price,
                                   edge=divergence))

        # Prune old cooldowns
        self._traded_events = {
            k: v for k, v in self._traded_events.items()
            if (now - v).total_seconds() / 3600 < self._cooldown_hours * 2
        }
```

- [ ] **Step 5: Wire into `__main__.py`**

In `polybot/__main__.py`, add imports at the top:

```python
from polybot.analysis.odds_client import OddsClient
from polybot.strategies.cross_venue import CrossVenueStrategy
```

After the MR strategy block and before the Engine constructor, add:

```python
    if getattr(settings, 'cv_enabled', False) and getattr(settings, 'odds_api_key', ''):
        odds_client = OddsClient(
            api_key=settings.odds_api_key,
            sports=getattr(settings, 'cv_sports', 'basketball_nba,icehockey_nhl').split(','))
        await odds_client.start()
        cv_strategy = CrossVenueStrategy(settings=settings, odds_client=odds_client)
        engine.add_strategy(cv_strategy)
```

Also ensure `strategy_performance` row exists for `cross_venue`. In the engine startup or schema, add:

```python
    # Ensure strategy_performance row exists for cross_venue
    await db.execute(
        """INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled)
           VALUES ('cross_venue', 0, 0, 0, 0, true) ON CONFLICT (strategy) DO NOTHING""")
```

Add this after the existing `INSERT INTO system_state` block.

- [ ] **Step 6: Run all tests**

Run: `cd ~/polybot && uv run pytest tests/test_cross_venue.py tests/test_odds_client.py -v`
Expected: ALL PASS

- [ ] **Step 7: Run full suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 8: Commit everything**

```bash
cd ~/polybot
git add polybot/strategies/cross_venue.py polybot/analysis/odds_client.py \
        polybot/core/config.py polybot/__main__.py \
        tests/test_cross_venue.py tests/test_odds_client.py
git commit -m "feat: add cross-venue arbitrage strategy

New strategy compares Polymarket prices against sportsbook consensus
from The Odds API (FanDuel, DraftKings, BetMGM). When Polymarket
diverges by >3% from consensus, enters a trade toward the sportsbook
price. Backed by much deeper liquidity in traditional sportsbooks.

Disabled by default — set CV_ENABLED=true and ODDS_API_KEY in .env.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Activation

After implementation, to enable:

1. Sign up at https://the-odds-api.com/ (free, instant API key)
2. Add to `.env`:
```bash
ODDS_API_KEY=your_key_here
CV_ENABLED=true
```
3. Restart polybot

## Credit Budget (Free Tier: 500/month)

Each call to one sport with 2 regions (us + us_ex) costs 2 credits.
With 4 configured sports polling every 5 minutes during game hours (~5h/day):
- 4 sports × 12 polls/hour × 5 hours × 2 credits = 480 credits/day

**For free tier:** reduce to 2 sports (NBA + NHL) and poll every 15 minutes:
- 2 sports × 4 polls/hour × 5 hours × 2 credits = 80 credits/day
- Monthly: ~2,400 — exceeds free tier

**Recommendation:** Start with $30/month plan (20,000 credits) or poll every 30 min on free tier.
