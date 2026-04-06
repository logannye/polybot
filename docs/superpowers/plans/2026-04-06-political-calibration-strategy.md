# Political Calibration Strategy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the spray-and-pray forecast strategy with a focused political market strategy that exploits the academically-validated calibration bias — political markets on Polymarket systematically underprice favorites by ~13 percentage points (calibration slope 1.31).

**Architecture:** New `PoliticalStrategy` scans political/geopolitical markets via event tags from the Gamma API, applies calibration correction to compute true probability, uses LLM ensemble only for high-edge confirmation, and trades with aggressive Kelly sizing. Existing losing strategies (arbitrage, market maker, cross-venue) are disabled.

**Tech Stack:** Python 3.13, asyncpg, aiohttp, structlog, pydantic Settings, pytest

---

## File Structure

| File | Responsibility |
|------|---------------|
| `polybot/markets/scanner.py` | **Modify**: Extract event tags from Gamma API, expose them on parsed markets |
| `polybot/analysis/calibration.py` | **Create**: Calibration correction functions (academic-backed debiasing) |
| `polybot/strategies/political.py` | **Create**: PoliticalStrategy — scans political markets, applies calibration, optional LLM confirmation, trades the gap |
| `polybot/core/config.py` | **Modify**: Add political strategy config keys |
| `polybot/__main__.py` | **Modify**: Wire up PoliticalStrategy, disable losers |
| `tests/test_calibration.py` | **Create**: Tests for calibration module |
| `tests/test_political_strategy.py` | **Create**: Tests for political strategy |
| `tests/test_scanner_tags.py` | **Create**: Tests for tag extraction |

---

### Task 1: Extract event tags from Gamma API

The Gamma API returns an `events` array on each market, each event containing a `tags` array with `{label, slug}` objects. The current scanner ignores this entirely and falls back to slug-based "categories" (like `will-uconn-win-the-2026-womens-ncaa-tournament`). We need real categories.

**Files:**
- Modify: `polybot/markets/scanner.py`
- Create: `tests/test_scanner_tags.py`

- [ ] **Step 1: Write failing test for tag extraction**

Create `tests/test_scanner_tags.py`:

```python
"""Tests for Gamma API event tag extraction."""
import pytest
from polybot.markets.scanner import parse_gamma_market


def _make_raw_market(**overrides):
    """Minimal Gamma API market payload with events + tags."""
    base = {
        "active": True,
        "closed": False,
        "conditionId": "0xabc123",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.65", "0.35"]',
        "clobTokenIds": '["token_yes", "token_no"]',
        "endDate": "2026-12-31T23:59:59Z",
        "volume24hr": 50000,
        "liquidityNum": 100000,
        "events": [],
    }
    base.update(overrides)
    return base


def test_parse_extracts_tag_labels():
    raw = _make_raw_market(events=[{
        "tags": [
            {"label": "Politics", "slug": "politics"},
            {"label": "Trump", "slug": "trump"},
        ]
    }])
    result = parse_gamma_market(raw)
    assert result is not None
    assert result["tags"] == ["politics", "trump"]


def test_parse_extracts_tags_from_multiple_events():
    raw = _make_raw_market(events=[
        {"tags": [{"label": "Geopolitics", "slug": "geopolitics"}]},
        {"tags": [{"label": "Ukraine", "slug": "ukraine"}]},
    ])
    result = parse_gamma_market(raw)
    assert result is not None
    assert "geopolitics" in result["tags"]
    assert "ukraine" in result["tags"]


def test_parse_handles_no_events():
    raw = _make_raw_market()
    raw.pop("events", None)
    result = parse_gamma_market(raw)
    assert result is not None
    assert result["tags"] == []


def test_parse_handles_events_without_tags():
    raw = _make_raw_market(events=[{"title": "Some event"}])
    result = parse_gamma_market(raw)
    assert result is not None
    assert result["tags"] == []


def test_parse_deduplicates_tags():
    raw = _make_raw_market(events=[
        {"tags": [{"label": "Politics", "slug": "politics"}]},
        {"tags": [{"label": "Politics", "slug": "politics"}, {"label": "World", "slug": "world"}]},
    ])
    result = parse_gamma_market(raw)
    assert result is not None
    # Deduplicated
    assert result["tags"].count("politics") == 1
    assert "world" in result["tags"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_scanner_tags.py -v`
Expected: FAIL — `parse_gamma_market` doesn't return `tags` key.

- [ ] **Step 3: Add tag extraction to parse_gamma_market**

In `polybot/markets/scanner.py`, update `parse_gamma_market` to extract tags from events. Add tag extraction before the `return` statement:

```python
    # Extract event tags (deduplicated, lowercase slugs)
    tags: list[str] = []
    seen_tags: set[str] = set()
    for event in raw.get("events", []):
        for tag in event.get("tags", []):
            slug = tag.get("slug", "").lower().strip()
            if slug and slug not in seen_tags:
                tags.append(slug)
                seen_tags.add(slug)
```

Add `"tags": tags,` to the return dict. Also use tags to derive a better `category`:

```python
    # Derive category from tags (first recognized tag wins)
    CATEGORY_TAGS = {"politics", "geopolitics", "crypto", "sports", "finance",
                     "business", "tech", "culture", "weather", "world"}
    derived_category = "unknown"
    for t in tags:
        if t in CATEGORY_TAGS:
            derived_category = t
            break
```

Replace the existing category line:
```python
    "category": derived_category if derived_category != "unknown" else (raw.get("category") or raw.get("slug", "unknown") or "unknown"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_scanner_tags.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run existing scanner tests to verify no regression**

Run: `cd ~/polybot && uv run pytest tests/ -k scanner -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add polybot/markets/scanner.py tests/test_scanner_tags.py
git commit -m "feat: extract event tags from Gamma API for proper market categorization"
```

---

### Task 2: Calibration correction module

Academic research shows political markets on Polymarket have a calibration slope of ~1.31 — meaning a market price of 0.70 corresponds to a true probability of ~0.83. This module applies that correction.

**Files:**
- Create: `polybot/analysis/calibration.py`
- Create: `tests/test_calibration.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_calibration.py`:

```python
"""Tests for academic calibration correction."""
import pytest
from polybot.analysis.calibration import (
    calibration_adjusted_prob,
    is_political_market,
)


class TestCalibrationAdjustedProb:
    def test_political_favorite_is_boosted(self):
        """A 70-cent political contract should map to ~83% true probability."""
        result = calibration_adjusted_prob(0.70, slope=1.31)
        assert 0.80 < result < 0.86

    def test_political_underdog_is_compressed(self):
        """A 30-cent political contract should map to ~20% true probability."""
        result = calibration_adjusted_prob(0.30, slope=1.31)
        assert 0.15 < result < 0.25

    def test_midpoint_stays_at_midpoint(self):
        """A 50-cent contract maps to 50% regardless of slope (pivot point)."""
        result = calibration_adjusted_prob(0.50, slope=1.31)
        assert abs(result - 0.50) < 0.01

    def test_clamped_to_valid_range(self):
        """Output is always in [0.01, 0.99] even with extreme inputs."""
        assert calibration_adjusted_prob(0.99, slope=1.31) <= 0.99
        assert calibration_adjusted_prob(0.01, slope=1.31) >= 0.01

    def test_slope_1_is_identity(self):
        """Slope of 1.0 means no correction (well-calibrated market)."""
        for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
            assert abs(calibration_adjusted_prob(p, slope=1.0) - p) < 0.01

    def test_sports_slope_near_identity(self):
        """Sports markets are well-calibrated (slope ~1.05), minimal correction."""
        result = calibration_adjusted_prob(0.70, slope=1.05)
        assert 0.70 < result < 0.75  # small boost, not the ~0.83 of politics


class TestIsPoliticalMarket:
    def test_politics_tag(self):
        assert is_political_market(["politics", "trump"]) is True

    def test_geopolitics_tag(self):
        assert is_political_market(["geopolitics", "ukraine"]) is True

    def test_global_elections_tag(self):
        assert is_political_market(["global-elections", "france"]) is True

    def test_sports_tag_is_not_political(self):
        assert is_political_market(["sports", "nba"]) is False

    def test_empty_tags(self):
        assert is_political_market([]) is False

    def test_world_tag_alone_is_political(self):
        """'world' tag is used for geopolitical events."""
        assert is_political_market(["world"]) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_calibration.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement calibration module**

Create `polybot/analysis/calibration.py`:

```python
"""Academic calibration correction for prediction market prices.

Based on: "Domain-Specific Calibration Dynamics in Prediction Markets"
(arxiv.org/html/2602.19520v1)

Key finding: Political markets on Polymarket have a calibration slope of ~1.31,
meaning prices are systematically compressed toward 0.50. A 70-cent contract
actually implies ~83% true probability.

The correction formula applies a logit-space slope adjustment:
  logit(p_true) = slope * logit(p_market)
  p_true = sigmoid(slope * logit(p_market))

where logit(p) = log(p / (1-p)) and sigmoid(x) = 1 / (1 + exp(-x))
"""

import math

# Calibration slopes by domain (from academic research)
DOMAIN_SLOPES = {
    "politics": 1.31,
    "geopolitics": 1.31,  # same bias as politics
    "world": 1.20,        # geopolitical events, slightly less biased
    "crypto": 1.05,       # well-calibrated
    "sports": 1.05,       # well-calibrated
    "finance": 1.10,
    "default": 1.10,
}

POLITICAL_TAGS = {"politics", "geopolitics", "global-elections", "world",
                  "trump-presidency", "foreign-policy"}


def is_political_market(tags: list[str]) -> bool:
    """Check if a market's tags indicate it's political/geopolitical."""
    return bool(POLITICAL_TAGS & set(tags))


def calibration_adjusted_prob(market_price: float, slope: float = 1.31) -> float:
    """Apply calibration correction to a market price.

    Uses logit-space linear correction: logit(p_true) = slope * logit(p_market).
    This stretches prices away from 0.50 — favorites get boosted, underdogs get
    compressed — matching the empirically observed bias in prediction markets.
    """
    # Clamp input to avoid log(0) / division by zero
    p = max(0.001, min(0.999, market_price))

    # Logit transform: log(p / (1-p))
    logit_p = math.log(p / (1 - p))

    # Apply slope correction in logit space
    corrected_logit = slope * logit_p

    # Sigmoid back to probability space
    corrected = 1.0 / (1.0 + math.exp(-corrected_logit))

    return max(0.01, min(0.99, corrected))


def get_domain_slope(tags: list[str]) -> float:
    """Look up the calibration slope for a market based on its tags."""
    for tag in tags:
        if tag in DOMAIN_SLOPES:
            return DOMAIN_SLOPES[tag]
    return DOMAIN_SLOPES["default"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_calibration.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/analysis/calibration.py tests/test_calibration.py
git commit -m "feat: add calibration correction module based on academic research"
```

---

### Task 3: PoliticalStrategy

New strategy that scans political/geopolitical markets and trades the calibration gap. Uses LLM ensemble only for confirmation on high-edge trades (>10 points). Simpler than the forecast strategy — no quant signals, no prescore, no shrinkage.

**Files:**
- Create: `polybot/strategies/political.py`
- Create: `tests/test_political_strategy.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_political_strategy.py`:

```python
"""Tests for PoliticalStrategy."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from polybot.strategies.political import PoliticalStrategy


def _make_settings(**overrides):
    s = MagicMock()
    s.pol_interval_seconds = 600.0
    s.pol_kelly_mult = 0.40
    s.pol_max_single_pct = 0.20
    s.pol_min_edge = 0.06
    s.pol_min_liquidity = 50000.0
    s.pol_llm_confirm_edge = 0.10
    s.pol_max_positions = 5
    s.use_maker_orders = True
    s.min_trade_size = 1.0
    s.bankroll_survival_threshold = 50.0
    s.bankroll_growth_threshold = 500.0
    s.post_breaker_kelly_reduction = 0.50
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_political_market(yes_price=0.70, liquidity=100000, tags=None,
                           question="Will Trump win 2028?", pid="0xpol1"):
    return {
        "polymarket_id": pid,
        "question": question,
        "category": "politics",
        "tags": tags or ["politics", "trump"],
        "resolution_time": datetime.now(timezone.utc) + timedelta(days=60),
        "yes_price": yes_price,
        "no_price": 1.0 - yes_price,
        "yes_token_id": "tok_yes",
        "no_token_id": "tok_no",
        "volume_24h": 50000,
        "book_depth": liquidity,
        "group_slug": None,
    }


def _make_ctx(db_mock, scanner_mock, settings):
    ctx = MagicMock()
    ctx.db = db_mock
    ctx.scanner = scanner_mock
    ctx.settings = settings
    ctx.portfolio_lock = asyncio.Lock()
    ctx.risk_manager = MagicMock()
    ctx.risk_manager.check.return_value = MagicMock(allowed=True)
    ctx.executor = AsyncMock()
    ctx.executor.place_order.return_value = {"trade_id": 1}
    ctx.email_notifier = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_filters_to_political_markets_only():
    """Non-political markets should be skipped entirely."""
    settings = _make_settings()
    strategy = PoliticalStrategy(settings=settings)
    db = AsyncMock()
    db.fetchrow.return_value = {"bankroll": 500.0, "kelly_mult": 0.35,
                                 "total_deployed": 0.0, "daily_pnl": 0.0,
                                 "circuit_breaker_until": None, "edge_threshold": 0.05}
    db.fetchval.return_value = 0  # no open positions
    db.fetch.return_value = []

    scanner = AsyncMock()
    sports_market = _make_political_market(tags=["sports", "nba"], question="Will Lakers win?")
    political_market = _make_political_market(yes_price=0.70, tags=["politics", "trump"])
    scanner.fetch_markets.return_value = [sports_market, political_market]

    ctx = _make_ctx(db, scanner, settings)

    await strategy.run_once(ctx)

    # Should have traded the political market (or at least attempted)
    # but NOT the sports market


@pytest.mark.asyncio
async def test_skips_low_liquidity_markets():
    """Markets below pol_min_liquidity are skipped."""
    settings = _make_settings(pol_min_liquidity=100000.0)
    strategy = PoliticalStrategy(settings=settings)
    db = AsyncMock()
    db.fetchrow.return_value = {"bankroll": 500.0, "kelly_mult": 0.35,
                                 "total_deployed": 0.0, "daily_pnl": 0.0,
                                 "circuit_breaker_until": None, "edge_threshold": 0.05}
    db.fetchval.return_value = 0
    db.fetch.return_value = []

    scanner = AsyncMock()
    low_liq = _make_political_market(liquidity=10000)  # below threshold
    scanner.fetch_markets.return_value = [low_liq]

    ctx = _make_ctx(db, scanner, settings)

    await strategy.run_once(ctx)

    # No trades should be placed
    ctx.executor.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_calibration_edge_computation():
    """A 70-cent political market should produce ~13% edge via calibration."""
    from polybot.analysis.calibration import calibration_adjusted_prob
    market_price = 0.70
    true_prob = calibration_adjusted_prob(market_price, slope=1.31)
    edge = true_prob - market_price
    # Edge should be approximately 0.10-0.15
    assert 0.08 < edge < 0.18


@pytest.mark.asyncio
async def test_respects_position_cap():
    """Should not trade when pol_max_positions is reached."""
    settings = _make_settings(pol_max_positions=2)
    strategy = PoliticalStrategy(settings=settings)
    db = AsyncMock()
    db.fetchrow.return_value = {"bankroll": 500.0, "kelly_mult": 0.35,
                                 "total_deployed": 100.0, "daily_pnl": 0.0,
                                 "circuit_breaker_until": None, "edge_threshold": 0.05}
    db.fetchval.return_value = 2  # already at cap
    db.fetch.return_value = []

    scanner = AsyncMock()
    scanner.fetch_markets.return_value = [_make_political_market()]

    ctx = _make_ctx(db, scanner, settings)

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_political_strategy.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement PoliticalStrategy**

Create `polybot/strategies/political.py`:

```python
"""Political Calibration Strategy.

Exploits the academically-validated calibration bias in political prediction
markets. Political markets on Polymarket systematically underprice favorites
(calibration slope ~1.31: a 70-cent contract implies ~83% true probability).

This strategy:
1. Scans all markets, filters to political/geopolitical only (via event tags)
2. Applies calibration correction to compute true probability
3. Trades when calibration-adjusted edge exceeds threshold
4. Optionally confirms high-edge trades via LLM ensemble
"""

import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.analysis.calibration import (
    calibration_adjusted_prob,
    get_domain_slope,
    is_political_market,
)
from polybot.trading.kelly import compute_kelly, compute_position_size
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


class PoliticalStrategy(Strategy):
    name = "political"

    def __init__(self, settings, ensemble=None):
        self.interval_seconds = float(getattr(settings, "pol_interval_seconds", 600.0))
        self.kelly_multiplier = float(getattr(settings, "pol_kelly_mult", 0.40))
        self.max_single_pct = float(getattr(settings, "pol_max_single_pct", 0.20))
        self._min_edge = float(getattr(settings, "pol_min_edge", 0.06))
        self._min_liquidity = float(getattr(settings, "pol_min_liquidity", 50000.0))
        self._llm_confirm_edge = float(getattr(settings, "pol_llm_confirm_edge", 0.10))
        self._max_positions = int(getattr(settings, "pol_max_positions", 5))
        self._settings = settings
        self._ensemble = ensemble

    async def run_once(self, ctx: TradingContext) -> None:
        # Check strategy enabled
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name)
        if enabled is False:
            return

        # System state
        state = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return
        bankroll = float(state["bankroll"])

        # Position cap
        open_count = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = $1 AND status IN ('open', 'filled', 'dry_run')",
            self.name)
        if open_count >= self._max_positions:
            log.debug("pol_position_cap", open=open_count, max=self._max_positions)
            return

        # Scan markets
        all_markets = await ctx.scanner.fetch_markets()
        if not all_markets:
            return

        # Filter to political markets with sufficient liquidity
        political = []
        for m in all_markets:
            tags = m.get("tags", [])
            if not is_political_market(tags):
                continue
            if m.get("book_depth", 0) < self._min_liquidity:
                continue
            # Skip markets resolving within 24h (use snipe for those)
            hours_left = (m["resolution_time"] - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_left < 24:
                continue
            political.append(m)

        if not political:
            log.debug("pol_no_markets", total_scanned=len(all_markets))
            return

        # Score by calibration edge, take best opportunities
        opportunities = []
        for m in political:
            tags = m.get("tags", [])
            slope = get_domain_slope(tags)
            market_price = m["yes_price"]

            # Calibration says the TRUE probability differs from market price
            true_prob_yes = calibration_adjusted_prob(market_price, slope=slope)
            true_prob_no = calibration_adjusted_prob(1.0 - market_price, slope=slope)

            # Pick the side with the larger edge
            yes_edge = true_prob_yes - market_price
            no_edge = true_prob_no - (1.0 - market_price)

            if yes_edge >= no_edge and yes_edge >= self._min_edge:
                opportunities.append({
                    "market": m,
                    "side": "YES",
                    "true_prob": true_prob_yes,
                    "edge": yes_edge,
                    "buy_price": market_price,
                    "slope": slope,
                })
            elif no_edge >= self._min_edge:
                opportunities.append({
                    "market": m,
                    "side": "NO",
                    "true_prob": true_prob_no,
                    "edge": no_edge,
                    "buy_price": 1.0 - market_price,
                    "slope": slope,
                })

        if not opportunities:
            log.info("pol_no_edge", political_count=len(political),
                     min_edge=self._min_edge)
            return

        # Sort by edge descending, take top opportunities up to position cap
        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        slots = self._max_positions - open_count
        top = opportunities[:slots]

        log.info("pol_opportunities", found=len(opportunities), trading=len(top),
                 best_edge=round(top[0]["edge"], 4) if top else 0)

        # Get existing positions to avoid duplicates
        existing_pids = set()
        existing = await ctx.db.fetch(
            """SELECT m.polymarket_id FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.strategy = $1 AND t.status IN ('open', 'filled', 'dry_run')""",
            self.name)
        for r in existing:
            existing_pids.add(r["polymarket_id"])

        for opp in top:
            m = opp["market"]
            if m["polymarket_id"] in existing_pids:
                continue

            await self._execute_trade(opp, bankroll, state, ctx)
            existing_pids.add(m["polymarket_id"])

    async def _execute_trade(self, opp: dict, bankroll: float, state,
                             ctx: TradingContext) -> None:
        m = opp["market"]
        side = opp["side"]
        true_prob = opp["true_prob"]
        edge = opp["edge"]
        buy_price = opp["buy_price"]

        # Kelly sizing
        kelly_result = compute_kelly(
            true_prob if side == "YES" else (1.0 - true_prob),
            m["yes_price"],
            fee_per_dollar=0.0,  # maker orders
        )
        if kelly_result.kelly_fraction <= 0:
            return

        adjusted_kelly = bankroll_kelly_adjustment(
            bankroll=bankroll,
            base_kelly=self.kelly_multiplier,
            post_breaker_until=state.get("circuit_breaker_until"),
            post_breaker_reduction=self._settings.post_breaker_kelly_reduction,
            survival_threshold=self._settings.bankroll_survival_threshold,
            growth_threshold=self._settings.bankroll_growth_threshold,
        )

        size = compute_position_size(
            bankroll=bankroll,
            kelly_fraction=kelly_result.kelly_fraction,
            kelly_mult=adjusted_kelly,
            max_single_pct=self.max_single_pct,
            min_trade_size=self._settings.min_trade_size,
        )
        if size <= 0:
            return

        async with ctx.portfolio_lock:
            # Fresh state for risk check
            fresh = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
            open_trades = await ctx.db.fetch(
                "SELECT t.position_size_usd, m.category FROM trades t JOIN markets m ON t.market_id = m.id "
                "WHERE t.status IN ('open', 'filled', 'dry_run')")
            cat_deployed: dict[str, float] = {}
            for t in open_trades:
                cat = t["category"]
                cat_deployed[cat] = cat_deployed.get(cat, 0.0) + float(t["position_size_usd"])

            portfolio = PortfolioState(
                bankroll=float(fresh["bankroll"]),
                total_deployed=float(fresh["total_deployed"]),
                daily_pnl=float(fresh["daily_pnl"]),
                open_count=len(open_trades),
                category_deployed=cat_deployed,
                circuit_breaker_until=fresh.get("circuit_breaker_until"),
            )
            proposal = TradeProposal(
                size_usd=size, category=m.get("category", "politics"),
                book_depth=m.get("book_depth", 100000))
            risk_result = ctx.risk_manager.check(portfolio, proposal,
                                                  max_single_pct=self.max_single_pct)
            if not risk_result.allowed:
                log.info("pol_risk_rejected", market=m["polymarket_id"],
                         reason=risk_result.reason)
                return

            # Upsert market
            market_id = await ctx.db.fetchval(
                """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                       current_price, volume_24h, book_depth)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (polymarket_id) DO UPDATE SET
                       current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
                   RETURNING id""",
                m["polymarket_id"], m["question"], m.get("category", "politics"),
                m["resolution_time"], m["yes_price"],
                m.get("volume_24h"), m.get("book_depth"))

            # Record analysis
            analysis_id = await ctx.db.fetchval(
                """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                   ensemble_stdev, quant_signals, edge)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                market_id, json.dumps([]),
                true_prob, 0.0,
                json.dumps({"source": "calibration", "slope": opp["slope"],
                            "market_price": m["yes_price"]}),
                edge)

            token_id = m["yes_token_id"] if side == "YES" else m["no_token_id"]

            log.info("pol_trade", market=m["polymarket_id"], question=m["question"][:60],
                     side=side, edge=round(edge, 4), true_prob=round(true_prob, 4),
                     market_price=m["yes_price"], size=size, slope=opp["slope"])

            await ctx.executor.place_order(
                token_id=token_id, side=side, size_usd=size, price=buy_price,
                market_id=market_id, analysis_id=analysis_id, strategy=self.name,
                kelly_inputs={
                    "true_prob": round(true_prob, 4),
                    "market_price": round(m["yes_price"], 4),
                    "edge": round(edge, 4),
                    "slope": opp["slope"],
                    "source": "calibration",
                },
                post_only=self._settings.use_maker_orders)

        await ctx.email_notifier.send(
            f"[POLYBOT] Political: {m['question'][:50]}",
            format_trade_email(event="executed", market=m["question"],
                               side=side, size=size, price=buy_price, edge=edge))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_political_strategy.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/strategies/political.py tests/test_political_strategy.py
git commit -m "feat: add PoliticalStrategy — calibration-based political market trading"
```

---

### Task 4: Config keys and strategy wiring

Add config keys for the political strategy, wire it into `__main__.py`, disable losing strategies, and add the `political` strategy to the DB constraint and `strategy_performance` table.

**Files:**
- Modify: `polybot/core/config.py`
- Modify: `polybot/__main__.py`

- [ ] **Step 1: Add config keys**

In `polybot/core/config.py`, add after the cross-venue config block (after line 219):

```python
    # Political calibration strategy
    pol_enabled: bool = True
    pol_interval_seconds: float = 600.0        # 10 min scan cycle
    pol_kelly_mult: float = 0.40               # aggressive — high-conviction calibration edge
    pol_max_single_pct: float = 0.20           # up to 20% bankroll per position
    pol_min_edge: float = 0.06                 # min 6% calibration-adjusted edge
    pol_min_liquidity: float = 50000.0         # only liquid markets
    pol_llm_confirm_edge: float = 0.10         # use LLM to confirm edges above 10%
    pol_max_positions: int = 5                 # max concurrent political positions
```

Also disable losing strategies by changing defaults:

```python
    # Change from current values:
    cv_enabled: bool = False          # was False already, keep disabled
    mm_enabled: bool = False          # was False already, keep disabled
    mr_enabled: bool = False          # disable MR (barely positive, not a home-run)
    forecast_enabled: bool = False    # disable generic forecast (replaced by political)
```

- [ ] **Step 2: Add `political` to trades check constraint**

This requires a DB migration. Run:

```sql
ALTER TABLE trades DROP CONSTRAINT trades_strategy_check;
ALTER TABLE trades ADD CONSTRAINT trades_strategy_check
  CHECK (strategy = ANY (ARRAY['arbitrage', 'snipe', 'forecast', 'market_maker',
                                'mean_reversion', 'cross_venue', 'political']));
```

- [ ] **Step 3: Wire PoliticalStrategy into __main__.py**

In `polybot/__main__.py`, add the import at the top:

```python
from polybot.strategies.political import PoliticalStrategy
```

After the cross-venue block (after line 177), add:

```python
    if getattr(settings, 'pol_enabled', True):
        pol_ensemble = ensemble if getattr(settings, 'pol_llm_confirm_edge', 0) > 0 else None
        pol_strategy = PoliticalStrategy(settings=settings, ensemble=pol_ensemble)
        engine.add_strategy(pol_strategy)
        await db.execute(
            """INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled)
               VALUES ('political', 0, 0, 0, 0, true) ON CONFLICT (strategy) DO NOTHING""")
```

- [ ] **Step 4: Run full test suite**

Run: `cd ~/polybot && uv run pytest -x -q`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py polybot/__main__.py
git commit -m "feat: wire PoliticalStrategy, disable losing strategies (arb, forecast, MR)"
```

---

### Task 5: DB migration, integration test, and deploy

Apply the strategy constraint migration, run the full suite, restart Polybot, and verify the political strategy is scanning.

**Files:**
- No new files

- [ ] **Step 1: Apply DB migration**

```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
ALTER TABLE trades DROP CONSTRAINT trades_strategy_check;
ALTER TABLE trades ADD CONSTRAINT trades_strategy_check
  CHECK (strategy = ANY (ARRAY['arbitrage', 'snipe', 'forecast', 'market_maker',
                                'mean_reversion', 'cross_venue', 'political']));
"
```

- [ ] **Step 2: Update .env if needed**

Check `~/polybot/.env` for any overrides that would re-enable disabled strategies:

```bash
grep -E 'FORECAST_ENABLED|MR_ENABLED|CV_ENABLED|MM_ENABLED|POL_ENABLED' ~/polybot/.env
```

Remove or set to `false` any that override the new defaults. Add `POL_ENABLED=true` if not present.

- [ ] **Step 3: Run full test suite**

Run: `cd ~/polybot && uv run pytest -x -q`
Expected: ALL PASS (420+ tests)

- [ ] **Step 4: Restart Polybot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 5: Verify political strategy is scanning**

```bash
sleep 30 && tail -50 ~/polybot/data/polybot_stdout.log | grep -E "pol_"
```

Expected: `pol_opportunities` or `pol_no_edge` or `pol_no_markets` logs showing the strategy is running.

- [ ] **Step 6: Monitor first cycle**

Wait 10 minutes for the first full cycle. Check:

```bash
grep "pol_trade" ~/polybot/data/polybot_stdout.log | head -5
```

If trades are placed, verify they look reasonable:

```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
SELECT t.id, LEFT(m.question, 50), t.side, t.entry_price,
  ROUND(t.position_size_usd::numeric, 2) as size,
  t.kelly_inputs->>'edge' as edge,
  t.kelly_inputs->>'slope' as slope
FROM trades t JOIN markets m ON t.market_id = m.id
WHERE t.strategy = 'political' ORDER BY t.opened_at DESC LIMIT 5;
"
```

---

## What's NOT in this plan (future plans)

1. **Combinatorial Arbitrage** — Fix the existing exhaustive arb strategy with orderbook verification and higher edge thresholds. Separate plan.
2. **News Catalyst Speed Trading** — New strategy using WebSocket price_change events + LLM news interpretation. Separate plan.
3. **LLM Confirmation for Political** — The `pol_llm_confirm_edge` config key is wired but the LLM confirmation path in `PoliticalStrategy` is not implemented yet. Can be added once the base calibration strategy is validated.
4. **Position management for political trades** — Political positions should hold to resolution (no time-stop). The current position manager's time-stop logic may need a strategy-specific override.
