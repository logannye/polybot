# Forecast Resolution Window Expansion

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the forecast strategy's resolution window from 72h to 168h so it can trade every day instead of going silent for 6-day stretches between event clusters.

**Architecture:** Single config default change + test updates. The existing time-stop system (`forecast_time_stop_fraction: 0.15`, capped at 8h) already scales with resolution time, so no position management changes needed. All other risk controls (stop-loss, Kelly sizing, consensus, edge threshold, blacklist, category filter, entry price guards) remain unchanged.

**Tech Stack:** Python 3.13, pytest, pydantic-settings

---

### Task 1: Update `resolution_hours_max` default from 72 to 168

**Files:**
- Modify: `polybot/core/config.py:79`
- Modify: `tests/test_config.py:31`

- [ ] **Step 1: Update the config default**

In `polybot/core/config.py`, change line 79:

```python
# Before
resolution_hours_max: int = 72

# After
resolution_hours_max: int = 168
```

- [ ] **Step 2: Update the config test**

In `tests/test_config.py`, change line 31:

```python
# Before
assert settings.resolution_hours_max == 72

# After
assert settings.resolution_hours_max == 168
```

- [ ] **Step 3: Run tests to verify**

Run: `cd ~/polybot && uv run pytest tests/test_config.py tests/test_filters.py tests/test_forecast_strategy.py -v`

Expected: All pass. The filter tests use explicit `resolution_hours_max=72` kwargs so they're unaffected by the default change.

- [ ] **Step 4: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py tests/test_config.py
git commit -m "feat: widen forecast resolution window 72h -> 168h

Forecast went silent Apr 3-7 because no quality markets resolved
within 72h after March Madness ended. Widening to 168h (1 week)
exposes forecast to the full active market pool while keeping all
other risk controls (stop-loss, time-stop scaling, Kelly, consensus,
entry price filters) intact."
```

### Task 2: Add filter test for the new 168h default

**Files:**
- Modify: `tests/test_filters.py`

- [ ] **Step 1: Write a test proving 168h window admits a 5-day market**

Add to `tests/test_filters.py` at the end of `TestFilterMarkets`:

```python
    def test_passes_market_within_168h(self):
        """A market resolving in 5 days passes the new 168h default window."""
        m = _make_market(resolution_time=datetime.now(timezone.utc) + timedelta(hours=120))
        assert len(filter_markets([m], resolution_hours_max=168)) == 1

    def test_rejects_market_beyond_168h(self):
        """A market resolving in 8 days is rejected by the 168h window."""
        m = _make_market(resolution_time=datetime.now(timezone.utc) + timedelta(hours=200))
        assert len(filter_markets([m], resolution_hours_max=168)) == 0
```

- [ ] **Step 2: Run the new tests**

Run: `cd ~/polybot && uv run pytest tests/test_filters.py -v`

Expected: All pass (including both new tests).

- [ ] **Step 3: Commit**

```bash
cd ~/polybot
git add tests/test_filters.py
git commit -m "test: add filter tests for 168h resolution window"
```

### Task 3: Restart the bot to pick up the new default

**Files:** None (runtime operation)

- [ ] **Step 1: Restart the LaunchAgent**

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist
sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist
```

- [ ] **Step 2: Verify it's running**

```bash
launchctl list | grep polybot
```

Expected: PID and exit code 0.
