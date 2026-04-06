# Volatility Harvester Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten the entire system around fast MR cycles — 1h time-stop, 20% max position, 30min cooldown, disable MM, 6h snipe time-stop, and CV short-dated filter — to maximize capital velocity and daily PnL.

**Architecture:** Five .env config changes (Tasks 1-2) plus one code change adding a resolution-time filter to cross-venue strategy (Task 3). The config changes are applied to .env (not tracked in git) and config.py defaults where appropriate. The CV filter adds a `cv_max_days_to_resolution` config key and a 3-line check in `cross_venue.py`.

**Tech Stack:** Python 3.13, pytest, pydantic Settings (.env)

---

### Task 1: MR Config Tightening

**Files:**
- Modify: `.env`
- Modify: `polybot/core/config.py`

Three MR config changes that tighten the fast-cycle pattern.

- [ ] **Step 1: Cut MR time-stop from 3h to 1h**

In `.env`, change:

```
MR_MAX_HOLD_HOURS=3.0
```

To:

```
MR_MAX_HOLD_HOURS=1.0
```

In `polybot/core/config.py`, change:

```python
    mr_max_hold_hours: float = 3.0
```

To:

```python
    mr_max_hold_hours: float = 1.0
```

- [ ] **Step 2: Raise MR max single position from 15% to 20%**

Add to `.env`:

```
MR_MAX_SINGLE_PCT=0.20
```

In `polybot/core/config.py`, change:

```python
    mr_max_single_pct: float = 0.15
```

To:

```python
    mr_max_single_pct: float = 0.20
```

- [ ] **Step 3: Cut MR cooldown from 3h to 30min**

In `.env`, change:

```
MR_COOLDOWN_HOURS=3
```

To:

```
MR_COOLDOWN_HOURS=0.5
```

In `polybot/core/config.py`, change:

```python
    mr_cooldown_hours: float = 6.0
```

To:

```python
    mr_cooldown_hours: float = 0.5
```

- [ ] **Step 4: Commit config.py changes**

```bash
cd ~/polybot && git add polybot/core/config.py && git commit -m "$(cat <<'EOF'
tune: MR volatility harvester — 1h time-stop, 20% max, 30min cooldown

Production data: under-1h trades are +$31.68 (13W/9L), over-1h are
-$13.33 (1W/6L). Tightening around the fast-cycle pattern.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Disable MM and Tighten Snipe

**Files:**
- Modify: `.env`
- Modify: `polybot/core/config.py`

- [ ] **Step 1: Disable market maker**

In `.env`, change:

```
MM_ENABLED=true
```

To:

```
MM_ENABLED=false
```

- [ ] **Step 2: Cut snipe time-stop from 48h to 6h**

In `.env` (if `SNIPE_MAX_HOLD_HOURS` already exists, change it; otherwise add):

```
SNIPE_MAX_HOLD_HOURS=6.0
```

In `polybot/core/config.py`, change:

```python
    snipe_max_hold_hours: float = 48.0
```

To:

```python
    snipe_max_hold_hours: float = 6.0
```

- [ ] **Step 3: Commit config.py change**

```bash
cd ~/polybot && git add polybot/core/config.py && git commit -m "$(cat <<'EOF'
tune: disable MM, cut snipe time-stop to 6h

MM has +$0.33 simulated PnL after 700+ fills — no real edge.
Snipe 6h time-stop frees $215 in stale capital for MR trades.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Cross-Venue Short-Dated Filter

**Files:**
- Modify: `polybot/core/config.py` (add config key)
- Modify: `polybot/strategies/cross_venue.py:74-76` (add resolution check)
- Test: `tests/test_cross_venue.py`

Add a filter that skips CV trades on markets resolving more than 7 days out. This prevents capital from being locked in long-dated futures (NBA Finals at $0.001 resolving June 30).

- [ ] **Step 1: Add config key**

In `polybot/core/config.py`, after the line `cv_cooldown_hours: float = 12.0`, add:

```python
    cv_max_days_to_resolution: float = 7.0
```

- [ ] **Step 2: Read the config in CrossVenueStrategy.__init__**

In `polybot/strategies/cross_venue.py`, after line 30 (`self._settings = settings`), add:

```python
        self._max_days_to_resolution = getattr(settings, 'cv_max_days_to_resolution', 7.0)
```

- [ ] **Step 3: Write the failing test**

Add to `tests/test_cross_venue.py`:

```python
from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_run_once_skips_long_dated_market():
    """Should skip markets resolving more than 7 days out."""
    s = _make_settings()
    s.cv_max_days_to_resolution = 7.0
    odds_client = MagicMock()
    odds_client.fetch_all_sports = AsyncMock(return_value=[
        {"id": "evt1", "sport_key": "basketball_nba",
         "home_team": "Lakers", "away_team": "Celtics",
         "commence_time": "2026-04-06T00:00:00Z",
         "bookmakers": [
             {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": -200},
                 {"name": "Boston Celtics", "price": +170}]}]},
             {"key": "polymarket", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Los Angeles Lakers", "price": -110},
                 {"name": "Boston Celtics", "price": -110}]}]},
         ]}
    ])

    strategy = CrossVenueStrategy(settings=s, odds_client=odds_client)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=True)  # enabled
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    # Market resolves 90 days from now — should be skipped
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xabc", "question": "Will the Los Angeles Lakers win?",
               "yes_price": 0.45, "category": "sports", "book_depth": 5000,
               "resolution_time": datetime.now(timezone.utc) + timedelta(days=90),
               "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
```

- [ ] **Step 4: Run test to verify it fails**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_cross_venue.py::test_run_once_skips_long_dated_market -v
```

Expected: FAIL — no resolution time check exists yet.

- [ ] **Step 5: Add resolution time filter in cross_venue.py**

In `polybot/strategies/cross_venue.py`, after the `cv_no_matching_market` check (after line 76 `continue`), add:

```python
            # Skip long-dated markets — don't lock capital for months
            res_time = matching_market.get("resolution_time")
            if res_time is not None:
                if isinstance(res_time, str):
                    from datetime import datetime as _dt
                    try:
                        res_time = _dt.fromisoformat(res_time.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        res_time = None
                if res_time is not None:
                    days_to_resolution = (res_time - now).total_seconds() / 86400
                    if days_to_resolution > self._max_days_to_resolution:
                        log.debug("cv_too_long_dated", outcome=div["outcome_name"],
                                  days=round(days_to_resolution, 1))
                        continue
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_cross_venue.py::test_run_once_skips_long_dated_market -v
```

Expected: PASS

- [ ] **Step 7: Run all cross-venue tests**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_cross_venue.py -v
```

Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py polybot/strategies/cross_venue.py tests/test_cross_venue.py && git commit -m "$(cat <<'EOF'
feat: CV short-dated filter — skip markets resolving >7 days out

Prevents capital from being locked in long-dated futures (e.g.,
NBA Finals at $0.001 resolving June 30). Only trades markets
resolving within cv_max_days_to_resolution (default 7 days).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Update Config Test Assertions

**Files:**
- Modify: `tests/test_config.py`

The config defaults test asserts specific values that we've changed. Update to match new defaults.

- [ ] **Step 1: Update test assertions**

In `tests/test_config.py`, find and update these assertions to match the new config.py defaults:

- `mr_max_hold_hours`: was 24.0, now 1.0 (if tested)
- `mr_max_single_pct`: was 0.15, now 0.20 (if tested)
- `mr_cooldown_hours`: was 6.0, now 0.5 (if tested)
- `snipe_max_hold_hours`: was 48.0, now 6.0 (if tested)

Read the test file first to find which values are actually asserted, then update only those.

- [ ] **Step 2: Run config tests**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_config.py -v
```

Expected: All tests pass.

- [ ] **Step 3: Commit if changes were needed**

```bash
cd ~/polybot && git add tests/test_config.py && git commit -m "$(cat <<'EOF'
test: update config assertions for volatility harvester defaults

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Restart and Verify

**Files:** None (operational)

- [ ] **Step 1: Run full test suite**

```bash
cd ~/polybot && .venv/bin/python -m pytest -x -q
```

Expected: All tests pass.

- [ ] **Step 2: Restart the bot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 3: Verify strategies loaded**

```bash
sleep 15 && grep "engine_starting" ~/polybot/data/polybot_stdout.log | tail -1
```

Expected: `["snipe", "mean_reversion", "cross_venue"]` — no `forecast`, no `market_maker`.

- [ ] **Step 4: Push to GitHub**

```bash
cd ~/polybot && git push origin main
```
