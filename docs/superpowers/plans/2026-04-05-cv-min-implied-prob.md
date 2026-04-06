# Cross-Venue Minimum Implied Probability Floor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Cross-Venue from trading penny-odds contracts where divergences are bid-ask noise, not real edge.

**Architecture:** Add a `cv_min_implied_prob` config setting (default 0.10). In `CrossVenueStrategy.run_once()`, skip any divergence where `buy_price < self._min_implied_prob`. This is a 1-config-key + 3-line-filter change with full TDD.

**Tech Stack:** Python 3.13, pydantic Settings, pytest + AsyncMock

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `polybot/core/config.py` | Modify (line ~211) | Add `cv_min_implied_prob` setting |
| `polybot/strategies/cross_venue.py` | Modify (lines 23-28, 95-99) | Read setting + filter on buy_price |
| `tests/test_cross_venue.py` | Modify (append) | Two new tests: penny-odds skipped, mid-range allowed |

---

### Task 1: Test that penny-odds divergences are skipped

**Files:**
- Modify: `tests/test_cross_venue.py` (append after line 168)

- [ ] **Step 1: Write the failing test**

Add this test to the end of `tests/test_cross_venue.py`:

```python
@pytest.mark.asyncio
async def test_run_once_skips_penny_odds():
    """Should skip divergences where buy_price < cv_min_implied_prob (default 0.10)."""
    s = _make_settings()
    s.cv_min_implied_prob = 0.10  # 10% floor
    odds_client = MagicMock()
    # Consensus says 5%, Polymarket says 1.5% — 3.5% divergence but penny odds
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt1", "sport_key": "basketball_nba",
         "home_team": "Lakers", "away_team": "Celtics",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": 1900},
                 {"name": "Boston Celtics", "price": -5000}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": 5566},
                 {"name": "Boston Celtics", "price": -10000}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)  # enabled
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xabc", "question": "Will the Los Angeles Lakers win?",
               "yes_price": 0.015, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=3),
               "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && python -m pytest tests/test_cross_venue.py::test_run_once_skips_penny_odds -v`
Expected: FAIL — `AttributeError: Mock object has no attribute 'cv_min_implied_prob'` (the setting doesn't exist yet)

- [ ] **Step 3: Commit the failing test**

```bash
cd ~/polybot && git add tests/test_cross_venue.py && git commit -m "test: add penny-odds skip test for cross-venue min implied prob"
```

---

### Task 2: Add config key and filter implementation

**Files:**
- Modify: `polybot/core/config.py` (line ~211, after `cv_min_divergence`)
- Modify: `polybot/strategies/cross_venue.py` (lines 27 and 95-97)
- Modify: `tests/test_cross_venue.py` (`_make_settings` helper, line ~14)

- [ ] **Step 1: Add the config key**

In `polybot/core/config.py`, after line 211 (`cv_min_divergence: float = 0.03`), add:

```python
    cv_min_implied_prob: float = 0.10   # skip outcomes below 10% implied
```

- [ ] **Step 2: Read the setting in CrossVenueStrategy.__init__**

In `polybot/strategies/cross_venue.py`, after line 27 (`self._min_divergence = settings.cv_min_divergence`), add:

```python
        self._min_implied_prob = settings.cv_min_implied_prob
```

- [ ] **Step 3: Add the filter after buy_price is computed**

In `polybot/strategies/cross_venue.py`, after line 97 (`buy_price = matching_market["yes_price"] if side == "YES" else (1 - matching_market["yes_price"])`), add:

```python
            if buy_price < self._min_implied_prob:
                log.debug("cv_penny_odds_skip", outcome=div["outcome_name"],
                          buy_price=round(buy_price, 4), floor=self._min_implied_prob)
                continue
```

- [ ] **Step 4: Update test helper to include new setting**

In `tests/test_cross_venue.py`, in `_make_settings()`, after line 13 (`s.cv_min_divergence = 0.03`), add:

```python
    s.cv_min_implied_prob = 0.10
```

- [ ] **Step 5: Run the penny-odds test to verify it passes**

Run: `cd ~/polybot && python -m pytest tests/test_cross_venue.py::test_run_once_skips_penny_odds -v`
Expected: PASS

- [ ] **Step 6: Run all existing cross-venue tests to verify no regressions**

Run: `cd ~/polybot && python -m pytest tests/test_cross_venue.py -v`
Expected: All 6 tests PASS (5 existing + 1 new)

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py polybot/strategies/cross_venue.py tests/test_cross_venue.py && git commit -m "feat: add cv_min_implied_prob filter — skip penny-odds cross-venue trades"
```

---

### Task 3: Test that mid-range divergences still trade

**Files:**
- Modify: `tests/test_cross_venue.py` (append after penny-odds test)

- [ ] **Step 1: Write the positive test**

Add this test to the end of `tests/test_cross_venue.py`:

```python
@pytest.mark.asyncio
async def test_run_once_trades_mid_range_divergence():
    """Should place a trade when divergence is real and buy_price >= cv_min_implied_prob."""
    s = _make_settings()
    s.cv_min_implied_prob = 0.10
    odds_client = MagicMock()
    # Consensus says 55%, Polymarket says 45% — 10% divergence, mid-range price
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt2", "sport_key": "basketball_nba",
         "home_team": "Nuggets", "away_team": "Suns",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Denver Nuggets", "price": -120},
                 {"name": "Phoenix Suns", "price": 100}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Denver Nuggets", "price": -120},
                 {"name": "Phoenix Suns", "price": 100}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=[
        True,   # enabled check
        1,      # market upsert RETURNING id
        1,      # analysis insert RETURNING id
    ])
    ctx.db.fetchrow = AsyncMock(return_value={
        "bankroll": 500.0, "total_deployed": 50.0, "daily_pnl": 0.0,
        "post_breaker_until": None, "circuit_breaker_until": None,
    })
    ctx.db.fetch = AsyncMock(return_value=[
        {"position_size_usd": 10, "category": "sports"},
    ])
    ctx.executor = AsyncMock()
    ctx.executor.place_order = AsyncMock(return_value={"order_id": "test123"})
    ctx.settings = s
    ctx.risk_manager = RiskManager()
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xdef", "question": "Will the Denver Nuggets win?",
               "yes_price": 0.45, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=2),
               "volume_24h": 50000,
               "yes_token_id": "tok3", "no_token_id": "tok4"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_called_once()
```

- [ ] **Step 2: Run the new test**

Run: `cd ~/polybot && python -m pytest tests/test_cross_venue.py::test_run_once_trades_mid_range_divergence -v`
Expected: PASS (the mid-range price 0.45 is above the 0.10 floor)

- [ ] **Step 3: Run full test suite**

Run: `cd ~/polybot && python -m pytest tests/test_cross_venue.py -v`
Expected: All 7 tests PASS

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add tests/test_cross_venue.py && git commit -m "test: verify mid-range divergences still trade with min implied prob floor"
```

---

### Task 4: Set runtime value in .env and restart

**Files:**
- Modify: `.env` (add `CV_MIN_IMPLIED_PROB=0.10`)

- [ ] **Step 1: Add to .env**

Append to `.env`:

```
CV_MIN_IMPLIED_PROB=0.10
```

- [ ] **Step 2: Restart polybot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 3: Tail logs to confirm filter is active**

```bash
tail -50 ~/polybot/data/polybot.log | grep -i "penny_odds_skip\|cv_trade\|cv_divergences"
```

Expected: Within 5 minutes (one CV scan cycle), you should see `cv_penny_odds_skip` log entries for any sub-10% outcomes, confirming the filter is working.

- [ ] **Step 4: Commit .env change**

```bash
cd ~/polybot && git add .env && git commit -m "config: set CV_MIN_IMPLIED_PROB=0.10 in production"
```
