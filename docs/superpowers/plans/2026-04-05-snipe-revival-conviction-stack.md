# Resolution Snipe Revival + Conviction Stacking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Revive the dormant snipe strategy by adding a cross-venue verification path that uses sportsbook consensus instead of LLM calls, and (2) add conviction stacking so that trades confirmed by multiple strategies get larger position sizes.

**Architecture:** For snipe revival, add a new verification method `_verify_via_odds` to `ResolutionSnipeStrategy` that queries the existing `OddsClient` for the event's sportsbook consensus. If the consensus agrees with the snipe direction and exceeds a confidence threshold, the snipe is verified — no LLM needed. For conviction stacking, add a `conviction_multiplier` function to `kelly.py` and call it from MR and cross-venue strategies when the other strategy has a concurrent signal on the same market.

**Tech Stack:** Python 3.13, pytest, asyncio, AsyncMock

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `polybot/strategies/snipe.py` | Modify | Add `_verify_via_odds` method as alternative to LLM for tiers 1-3 |
| `polybot/trading/kelly.py` | Modify | Add `conviction_multiplier()` pure function |
| `polybot/strategies/mean_reversion.py` | Modify | Query for cross-venue agreement, apply conviction multiplier |
| `polybot/strategies/cross_venue.py` | Modify | Query for MR agreement, apply conviction multiplier |
| `polybot/__main__.py` | Modify | Pass `odds_client` to snipe strategy |
| `polybot/core/config.py` | Modify | Add `snipe_odds_verification_enabled`, `conviction_stack_multiplier` |
| `tests/test_snipe.py` | Modify | Add tests for odds-based verification |
| `tests/test_kelly.py` | Modify | Add tests for conviction_multiplier |

---

### Task 1: Add `conviction_multiplier` to kelly.py

A pure function that returns a sizing multiplier when multiple independent signals agree.

**Files:**
- Modify: `polybot/trading/kelly.py`
- Modify: `tests/test_kelly.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kelly.py`:

```python
from polybot.trading.kelly import conviction_multiplier


class TestConvictionMultiplier:
    def test_no_confirmations(self):
        """Zero confirming signals → no boost (1.0x)."""
        assert conviction_multiplier(0) == 1.0

    def test_one_confirmation(self):
        """One confirming signal → 1.5x boost."""
        assert conviction_multiplier(1) == 1.5

    def test_two_confirmations(self):
        """Two confirming signals → 2.0x boost."""
        assert conviction_multiplier(2) == 2.0

    def test_capped_at_max(self):
        """Should never exceed max_multiplier."""
        assert conviction_multiplier(5, max_multiplier=2.5) == 2.5

    def test_custom_per_signal(self):
        """Custom per-signal boost."""
        assert conviction_multiplier(1, per_signal=0.75) == 1.75
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_kelly.py::TestConvictionMultiplier -v`
Expected: FAIL — `ImportError: cannot import name 'conviction_multiplier'`

- [ ] **Step 3: Implement the function**

Append to `polybot/trading/kelly.py` (after the `compute_position_size` function):

```python


def conviction_multiplier(
    confirming_signals: int,
    per_signal: float = 0.5,
    max_multiplier: float = 3.0,
) -> float:
    """Compute a position size multiplier based on confirming signal count.

    When multiple independent strategies agree on the same trade direction,
    confidence is higher and position sizing should scale up.

    Args:
        confirming_signals: Number of other strategies confirming this trade.
        per_signal: Additional multiplier per confirming signal.
        max_multiplier: Cap on the total multiplier.

    Returns:
        Multiplier to apply to position size (1.0 = no boost).
    """
    return min(1.0 + confirming_signals * per_signal, max_multiplier)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_kelly.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/trading/kelly.py tests/test_kelly.py
git commit -m "feat: add conviction_multiplier for multi-signal sizing

When multiple independent strategies agree on the same trade direction,
conviction_multiplier scales position size by 1.5x per confirming
signal (capped at 3.0x). Enables larger bets on high-confidence trades.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire conviction stacking into MR and cross-venue strategies

Both strategies check if the OTHER strategy has a concurrent signal on the same market. If so, apply the conviction multiplier.

**Files:**
- Modify: `polybot/strategies/mean_reversion.py`
- Modify: `polybot/strategies/cross_venue.py`
- Modify: `polybot/core/config.py`

- [ ] **Step 1: Add config field**

In `polybot/core/config.py`, after the `cv_cooldown_hours` line, add:

```python
    conviction_stack_enabled: bool = True
    conviction_stack_per_signal: float = 0.5   # 1.5x per confirming signal
    conviction_stack_max: float = 3.0
```

- [ ] **Step 2: Add conviction check to MR strategy**

In `polybot/strategies/mean_reversion.py`, add this import at the top (after the existing imports):

```python
from polybot.trading.kelly import conviction_multiplier
```

In the `__init__` method, after `self._min_expected_reversion = ...`, add:

```python
        self._conviction_enabled = getattr(settings, 'conviction_stack_enabled', False)
        self._conviction_per_signal = getattr(settings, 'conviction_stack_per_signal', 0.5)
        self._conviction_max = getattr(settings, 'conviction_stack_max', 3.0)
```

In `run_once`, find the `size = compute_position_size(...)` call (around line 170). AFTER the `if size <= 0: continue` check, add:

```python
                # Conviction stacking: check if cross-venue agrees on this market
                if self._conviction_enabled and size > 0:
                    cv_confirms = await ctx.db.fetchval(
                        """SELECT COUNT(*) FROM trades
                           WHERE strategy = 'cross_venue'
                             AND status IN ('open', 'filled', 'dry_run')
                             AND market_id IN (
                                 SELECT id FROM markets WHERE polymarket_id = $1
                             )""", pid)
                    if cv_confirms and cv_confirms > 0:
                        mult = conviction_multiplier(
                            cv_confirms, self._conviction_per_signal, self._conviction_max)
                        old_size = size
                        size = min(size * mult, bankroll * self.max_single_pct)
                        if size > old_size:
                            log.info("mr_conviction_boost", market=pid,
                                     multiplier=round(mult, 2), old_size=old_size, new_size=size)
```

- [ ] **Step 3: Add conviction check to cross-venue strategy**

In `polybot/strategies/cross_venue.py`, add this import at the top:

```python
from polybot.trading.kelly import conviction_multiplier
```

In `__init__`, after `self._traded_events: dict[str, datetime] = {}`, add:

```python
        self._conviction_enabled = getattr(settings, 'conviction_stack_enabled', False)
        self._conviction_per_signal = getattr(settings, 'conviction_stack_per_signal', 0.5)
        self._conviction_max = getattr(settings, 'conviction_stack_max', 3.0)
```

In `run_once`, find `if size <= 0: continue` (around line 98). AFTER it, add:

```python
                # Conviction stacking: check if MR has a position on matching market
                if self._conviction_enabled and size > 0 and matching_market:
                    mr_confirms = await ctx.db.fetchval(
                        """SELECT COUNT(*) FROM trades
                           WHERE strategy = 'mean_reversion'
                             AND status IN ('open', 'filled', 'dry_run')
                             AND market_id IN (
                                 SELECT id FROM markets WHERE polymarket_id = $1
                             )""", pid)
                    if mr_confirms and mr_confirms > 0:
                        mult = conviction_multiplier(
                            mr_confirms, self._conviction_per_signal, self._conviction_max)
                        old_size = size
                        size = min(size * mult, bankroll * self.max_single_pct)
                        if size > old_size:
                            log.info("cv_conviction_boost", market=pid,
                                     multiplier=round(mult, 2), old_size=old_size, new_size=size)
```

- [ ] **Step 4: Run full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/strategies/mean_reversion.py polybot/strategies/cross_venue.py polybot/core/config.py
git commit -m "feat: wire conviction stacking into MR + cross-venue

When MR and cross-venue both have positions on the same market,
the later entry gets a 1.5x size boost (capped at 3.0x). Two
independent signals agreeing means higher confidence → larger bet.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Add odds-based verification to snipe strategy

Add a `_verify_via_odds` method that uses the existing OddsClient to verify snipe candidates without an LLM call. This runs BEFORE the LLM path — if odds verification passes, the LLM is skipped entirely.

**Files:**
- Modify: `polybot/strategies/snipe.py`
- Modify: `tests/test_snipe.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_snipe.py`:

```python
from unittest.mock import AsyncMock, MagicMock
from polybot.strategies.snipe import verify_snipe_via_odds


@pytest.mark.asyncio
async def test_odds_verify_confirms_yes_snipe():
    """Sportsbook consensus > 85% for YES snipe should verify."""
    odds_client = MagicMock()
    odds_client.fetch_odds = AsyncMock(return_value=[{
        "id": "evt1", "sport_key": "basketball_nba",
        "home_team": "Thunder", "away_team": "Jazz",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Thunder", "price": -1000},
                {"name": "Jazz", "price": 600},
            ]}]},
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Thunder", "price": -900},
                {"name": "Jazz", "price": 550},
            ]}]},
        ],
    }])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Oklahoma City Thunder win on 2026-04-05?",
        side="YES",
        min_consensus=0.85,
    )
    assert result is True


@pytest.mark.asyncio
async def test_odds_verify_rejects_weak_consensus():
    """Sportsbook consensus 60% for YES snipe should NOT verify."""
    odds_client = MagicMock()
    odds_client.fetch_odds = AsyncMock(return_value=[{
        "id": "evt2", "sport_key": "basketball_nba",
        "home_team": "Lakers", "away_team": "Mavericks",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Lakers", "price": -150},
                {"name": "Mavericks", "price": 125},
            ]}]},
        ],
    }])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Los Angeles Lakers win on 2026-04-05?",
        side="YES",
        min_consensus=0.85,
    )
    assert result is False


@pytest.mark.asyncio
async def test_odds_verify_returns_false_no_data():
    """No odds data should return False (fall back to LLM)."""
    odds_client = MagicMock()
    odds_client.fetch_odds = AsyncMock(return_value=[])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will something happen?",
        side="YES",
        min_consensus=0.85,
    )
    assert result is False


@pytest.mark.asyncio
async def test_odds_verify_no_side_snipe():
    """NO-side snipe: consensus < 15% (i.e., NO is > 85%) should verify."""
    odds_client = MagicMock()
    odds_client.fetch_odds = AsyncMock(return_value=[{
        "id": "evt3", "sport_key": "basketball_nba",
        "home_team": "Thunder", "away_team": "Jazz",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Jazz", "price": 3000},
                {"name": "Thunder", "price": -10000},
            ]}]},
        ],
    }])

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Utah Jazz win on 2026-04-05?",
        side="NO",
        min_consensus=0.85,
    )
    assert result is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_snipe.py::test_odds_verify_confirms_yes_snipe -v`
Expected: FAIL — `ImportError: cannot import name 'verify_snipe_via_odds'`

- [ ] **Step 3: Implement `verify_snipe_via_odds`**

In `polybot/strategies/snipe.py`, add this import at the top (after the existing imports):

```python
from polybot.analysis.odds_client import compute_consensus
```

Add this standalone async function BEFORE the `ResolutionSnipeStrategy` class:

```python
async def verify_snipe_via_odds(
    odds_client,
    question: str,
    side: str,
    min_consensus: float = 0.85,
) -> bool:
    """Verify a snipe candidate using sportsbook consensus instead of LLM.

    Searches The Odds API events for a matching event by team name,
    then checks if the sportsbook consensus supports the snipe direction.

    Args:
        odds_client: OddsClient instance with fetch_odds method.
        question: The Polymarket question (e.g., "Will the Thunder win on ...?").
        side: "YES" or "NO" — the snipe direction.
        min_consensus: Minimum sportsbook probability to verify (default 85%).

    Returns:
        True if sportsbook consensus confirms the snipe, False otherwise.
    """
    # Extract team name from question
    q_lower = question.lower()

    # Fetch odds for the most likely sport
    for sport in ["basketball_nba", "icehockey_nhl", "soccer_epl",
                   "soccer_uefa_champs_league", "soccer_usa_mls"]:
        try:
            events = await odds_client.fetch_odds(sport)
        except Exception:
            continue

        for event in events:
            home = event.get("home_team", "").lower()
            away = event.get("away_team", "").lower()

            # Check if either team name appears in the question
            matched_team = None
            if home and home in q_lower:
                matched_team = event.get("home_team")
            elif away and away in q_lower:
                matched_team = event.get("away_team")

            if not matched_team:
                continue

            consensus = compute_consensus(event.get("bookmakers", []))
            if not consensus or matched_team not in consensus:
                continue

            team_prob = consensus[matched_team]

            if side == "YES" and team_prob >= min_consensus:
                log.info("snipe_odds_verified", team=matched_team,
                         consensus=round(team_prob, 3), side=side)
                return True
            elif side == "NO" and team_prob <= (1.0 - min_consensus):
                log.info("snipe_odds_verified", team=matched_team,
                         consensus=round(team_prob, 3), side=side)
                return True

    return False
```

- [ ] **Step 4: Run snipe tests**

Run: `cd ~/polybot && uv run pytest tests/test_snipe.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/strategies/snipe.py tests/test_snipe.py
git commit -m "feat: add verify_snipe_via_odds for LLM-free snipe verification

Standalone async function that verifies snipe candidates using
sportsbook consensus from The Odds API. If consensus > 85% agrees
with the snipe direction, the trade is verified without an LLM call.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire odds verification into snipe strategy + pass odds_client

Modify `ResolutionSnipeStrategy.run_once` to try odds verification BEFORE the LLM path. If odds verification succeeds, skip the LLM entirely. Pass `odds_client` from `__main__.py`.

**Files:**
- Modify: `polybot/strategies/snipe.py` (run_once method)
- Modify: `polybot/__main__.py` (pass odds_client to snipe)
- Modify: `polybot/core/config.py` (add snipe_odds_verification_enabled)

- [ ] **Step 1: Add config field**

In `polybot/core/config.py`, after `snipe_max_market_exposure_pct`, add:

```python
    snipe_odds_verification_enabled: bool = True
    snipe_odds_min_consensus: float = 0.85
```

- [ ] **Step 2: Modify snipe __init__ to accept odds_client**

In `polybot/strategies/snipe.py`, in the `ResolutionSnipeStrategy.__init__` method, change the signature from:

```python
    def __init__(self, settings, ensemble=None):
```

to:

```python
    def __init__(self, settings, ensemble=None, odds_client=None):
```

And add after `self._market_cooldowns: dict[str, dict] = {}`:

```python
        self._odds_client = odds_client
        self._odds_verification_enabled = getattr(settings, 'snipe_odds_verification_enabled', False)
        self._odds_min_consensus = getattr(settings, 'snipe_odds_min_consensus', 0.85)
```

- [ ] **Step 3: Add odds verification path in run_once**

In `polybot/strategies/snipe.py`, find the LLM verification block (around line 214-239). It starts with:

```python
            if tier in (1, 2, 3) and self._ensemble:
```

Replace that entire block with:

```python
            if tier in (1, 2, 3):
                verified = False

                # Try odds-based verification first (faster, cheaper, no LLM)
                if self._odds_client and self._odds_verification_enabled:
                    try:
                        verified = await verify_snipe_via_odds(
                            odds_client=self._odds_client,
                            question=m["question"],
                            side=side,
                            min_consensus=self._odds_min_consensus,
                        )
                        if verified:
                            log.info("snipe_verified_via_odds", market=m["polymarket_id"],
                                     tier=tier, side=side)
                    except Exception as e:
                        log.error("snipe_odds_verify_error", error=str(e))

                # Fall back to LLM if odds verification didn't confirm
                if not verified and self._ensemble:
                    tier_max_hours = {1: 12.0, 2: getattr(ctx.settings, "snipe_tier2_llm_max_hours", 48.0), 3: getattr(ctx.settings, "snipe_tier3_llm_max_hours", 120.0)}
                    if hours_remaining > tier_max_hours.get(tier, 12.0):
                        log.info("snipe_rejected_far_future", market=m["polymarket_id"],
                                 hours=round(hours_remaining, 1), tier=tier)
                        continue
                    prompt = build_snipe_prompt(m["question"], str(m["resolution_time"]), hours_remaining, m["yes_price"])
                    try:
                        response = await self._ensemble._google.aio.models.generate_content(
                            model="gemini-2.5-flash", contents=prompt)
                        parsed = parse_snipe_response(response.text)
                        if not parsed or not parsed["determined"] or parsed["confidence"] < self._min_confidence:
                            log.info("snipe_rejected_llm", market=m["polymarket_id"],
                                     tier=tier, parsed=parsed)
                            continue
                        if parsed["outcome"] == "NO" and side == "YES":
                            log.info("snipe_rejected_llm_disagree", market=m["polymarket_id"],
                                     side=side, llm_outcome=parsed["outcome"])
                            continue
                        if parsed["outcome"] == "YES" and side == "NO":
                            log.info("snipe_rejected_llm_disagree", market=m["polymarket_id"],
                                     side=side, llm_outcome=parsed["outcome"])
                            continue
                        verified = True
                    except Exception as e:
                        log.error("snipe_llm_error", error=str(e))
                        continue

                if not verified:
                    log.debug("snipe_not_verified", market=m["polymarket_id"], tier=tier)
                    continue
```

- [ ] **Step 4: Wire odds_client in __main__.py**

In `polybot/__main__.py`, find the snipe strategy creation (around line 131):

```python
    engine.add_strategy(ResolutionSnipeStrategy(settings=settings, ensemble=ensemble))
```

Replace with:

```python
    # odds_client may have been created above for cross-venue; reuse if available
    _snipe_odds = None
    if getattr(settings, 'snipe_odds_verification_enabled', False) and getattr(settings, 'odds_api_key', ''):
        if 'odds_client' in dir():
            _snipe_odds = odds_client
        else:
            from polybot.analysis.odds_client import OddsClient as _OC
            _snipe_odds = _OC(api_key=settings.odds_api_key)
            await _snipe_odds.start()
    engine.add_strategy(ResolutionSnipeStrategy(
        settings=settings, ensemble=ensemble, odds_client=_snipe_odds))
```

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add polybot/strategies/snipe.py polybot/__main__.py polybot/core/config.py
git commit -m "feat: wire odds verification into snipe strategy

Snipe now tries sportsbook consensus verification BEFORE the LLM path.
If The Odds API confirms the outcome with >85% consensus, the snipe
trade is verified instantly — no LLM call needed. Falls back to LLM
if odds verification doesn't confirm.

This revives snipe for sports markets where sportsbook consensus
provides stronger verification than an LLM at lower cost and latency.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Verification

After deploying, check snipe activity:

```sql
-- Check if snipe is finding candidates again
SELECT id, strategy, side, ROUND(entry_price::numeric, 4) as entry,
       ROUND(position_size_usd::numeric, 2) as size,
       status, opened_at
FROM trades
WHERE strategy = 'snipe' AND opened_at > NOW() - INTERVAL '24 hours'
ORDER BY opened_at DESC;
```

Check conviction stacking:

```bash
# Look for conviction boost log entries
# (these appear when MR and cross-venue agree on the same market)
```

Expected outcomes:
- Snipe should start finding markets where sportsbooks show >85% consensus but Polymarket price is at 0.75-0.85 range
- Snipe position sizes should be ~$40-70 (historically $64 avg — much larger than MR)
- Conviction stacking should occasionally boost MR or cross-venue positions by 1.5x when both strategies fire on the same market
