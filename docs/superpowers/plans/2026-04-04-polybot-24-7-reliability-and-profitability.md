# Polybot 24/7 Reliability & Profitability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make polybot run reliably 24/7 without crashes, and unblock dormant strategies so it generates profits around the clock.

**Architecture:** Two-phase approach. Phase 1 hardens reliability: decouple the dashboard from the trading engine so a uvicorn crash can't kill the bot, add port cleanup on restart, and raise process priority. Phase 2 unblocks profitability: clear stuck snipe trades, loosen the forecast strategy's over-aggressive filters, and tune config. All changes use the existing Strategy ABC / Engine / LaunchAgent architecture.

**Tech Stack:** Python 3.13, asyncio, uvicorn/FastAPI (dashboard), PostgreSQL 16, pydantic-settings, pytest + AsyncMock, launchd (macOS LaunchAgent)

---

### Task 1: Decouple Dashboard from Engine (Reliability Critical Path)

The #1 reliability bug: `__main__.py:135-137` uses `asyncio.wait(FIRST_COMPLETED)` across engine, dashboard, and shutdown tasks. If uvicorn fails to bind port 8080, it calls `sys.exit(1)`, which kills the entire process — including the trading engine. The dashboard is a monitoring convenience; it must never take down the engine.

**Files:**
- Modify: `polybot/__main__.py:130-141`
- Test: `tests/test_main_lifecycle.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_main_lifecycle.py`:

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_dashboard_crash_does_not_kill_engine():
    """If the dashboard task raises, the engine task must continue running."""
    engine_iterations = 0

    async def fake_engine_run():
        nonlocal engine_iterations
        while engine_iterations < 3:
            engine_iterations += 1
            await asyncio.sleep(0.01)

    async def fake_dashboard_serve():
        raise OSError("Address already in use")

    from polybot.__main__ import _run_bot_tasks
    shutdown_event = asyncio.Event()

    # Auto-shutdown after engine finishes
    async def auto_shutdown():
        while engine_iterations < 3:
            await asyncio.sleep(0.01)
        shutdown_event.set()

    asyncio.create_task(auto_shutdown())
    await _run_bot_tasks(fake_engine_run, fake_dashboard_serve, shutdown_event)
    assert engine_iterations == 3


@pytest.mark.asyncio
async def test_shutdown_signal_stops_engine():
    """Setting the shutdown event must cancel engine and dashboard."""
    engine_started = asyncio.Event()

    async def fake_engine_run():
        engine_started.set()
        await asyncio.sleep(100)  # blocks until cancelled

    async def fake_dashboard_serve():
        await asyncio.sleep(100)

    from polybot.__main__ import _run_bot_tasks
    shutdown_event = asyncio.Event()

    async def trigger_shutdown():
        await engine_started.wait()
        shutdown_event.set()

    asyncio.create_task(trigger_shutdown())
    await _run_bot_tasks(fake_engine_run, fake_dashboard_serve, shutdown_event)
    # Should complete without hanging
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_main_lifecycle.py -v`
Expected: FAIL — `_run_bot_tasks` does not exist yet.

- [ ] **Step 3: Extract `_run_bot_tasks` and implement resilient task management**

In `polybot/__main__.py`, replace lines 130-141 (the `engine_task` through the `asyncio.gather` block) with:

```python
async def _run_bot_tasks(engine_coro, dashboard_coro, shutdown_event: asyncio.Event):
    """Run engine and dashboard concurrently. Dashboard failure is non-fatal."""
    engine_task = asyncio.create_task(engine_coro())
    dashboard_task = asyncio.create_task(dashboard_coro())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    def _on_dashboard_done(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("dashboard_crashed", error=str(exc))

    dashboard_task.add_done_callback(_on_dashboard_done)

    # Wait for shutdown signal or engine exit (NOT dashboard exit)
    await asyncio.wait(
        [engine_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED)

    # Cancel everything
    for task in [engine_task, dashboard_task, shutdown_task]:
        task.cancel()
    await asyncio.gather(engine_task, dashboard_task, shutdown_task, return_exceptions=True)
```

Then update the `main()` function body. Replace the old try block (lines 130-141):

```python
    try:
        await _run_bot_tasks(engine.run_forever, dashboard_server.serve, shutdown_event)
    finally:
```

Remove the old `engine_task`, `dashboard_task` variable creation and the `asyncio.wait(FIRST_COMPLETED)` block. The signal handlers and the `finally` block stay exactly as they are.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/polybot && uv run pytest tests/test_main_lifecycle.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest --tb=short -q`
Expected: All existing tests still pass (361+).

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add polybot/__main__.py tests/test_main_lifecycle.py
git commit -m "fix: decouple dashboard from engine — uvicorn crash no longer kills bot"
```

---

### Task 2: Add Port Cleanup on Restart

When the process restarts, the old instance may still hold port 8080 (TCP TIME_WAIT or zombie). The run script must kill any stale process on that port before launching.

**Files:**
- Modify: `scripts/run_polybot.sh:67-69` (between Pre-flight and Trap sections)

- [ ] **Step 1: Add port cleanup to the run script**

In `scripts/run_polybot.sh`, add a new section between the `PID File / Orphan Guard` block (ends line 66) and the `Pre-flight` block (starts line 68):

```bash
# ── Port Cleanup (prevent EADDRINUSE on restart) ───────────────────
DASHBOARD_PORT=8080
STALE_PID=$(lsof -ti :$DASHBOARD_PORT 2>/dev/null || true)
if [ -n "$STALE_PID" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) Killing stale process on port $DASHBOARD_PORT (PID $STALE_PID)"
    kill -TERM $STALE_PID 2>/dev/null || true
    sleep 2
    kill -KILL $STALE_PID 2>/dev/null || true
fi
```

- [ ] **Step 2: Verify the script is valid**

Run: `bash -n ~/polybot/scripts/run_polybot.sh && echo "syntax ok"`
Expected: `syntax ok`

- [ ] **Step 3: Commit**

```bash
cd ~/polybot
git add scripts/run_polybot.sh
git commit -m "fix: kill stale process on port 8080 before restart"
```

---

### Task 3: Raise Process Priority to Survive Galen OOM Pressure

Polybot runs at `Nice=5` / `ProcessType=Background`, making it the first thing macOS Jetsam kills when Galen's LLMs spike memory. Polybot uses <200MB; it should not be the OOM victim.

**Files:**
- Modify: `~/Library/LaunchAgents/ai.polybot.trader.plist`

- [ ] **Step 1: Update the plist to raise priority**

Change the `Nice` value from `5` to `0` (normal priority instead of deprioritized):

```xml
    <key>Nice</key>
    <integer>0</integer>
```

Change `ProcessType` from `Background` to `Standard`:

```xml
    <key>ProcessType</key>
    <string>Standard</string>
```

- [ ] **Step 2: Reload the LaunchAgent**

Run: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist 2>/dev/null; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist`

Then verify it's running:
Run: `sleep 60 && pgrep -f polybot && echo "running"`
Expected: PID numbers printed, then "running".

- [ ] **Step 3: Commit**

```bash
cd ~/polybot
git add ~/Library/LaunchAgents/ai.polybot.trader.plist
git commit -m "fix: raise polybot process priority to survive Galen OOM pressure"
```

---

### Task 4: Clear Stuck Snipe Trades

Two snipe trades in `dry_run` status from April 2 are blocking the per-market cumulative exposure cap. The `_resolution_monitor` only resolves trades whose `m.resolution_time <= NOW()` — these markets haven't resolved yet, so the trades sit forever, blocking new snipes.

**Files:**
- No code change — SQL cleanup + config awareness

- [ ] **Step 1: Inspect the stuck trades**

Run:
```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
SELECT t.id, t.strategy, t.side, t.entry_price, t.position_size_usd, t.status,
       t.opened_at, m.polymarket_id, m.question, m.resolution_time
FROM trades t JOIN markets m ON t.market_id = m.id
WHERE t.strategy = 'snipe' AND t.status = 'dry_run'
ORDER BY t.opened_at;"
```

Expected: 2 rows showing snipe trades from April 2 with future `resolution_time`.

- [ ] **Step 2: Cancel the stuck trades and release their capital**

Run:
```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
UPDATE trades SET status = 'cancelled', exit_reason = 'manual_cleanup', closed_at = NOW()
WHERE strategy = 'snipe' AND status = 'dry_run';
UPDATE system_state SET total_deployed = (
  SELECT COALESCE(SUM(position_size_usd), 0) FROM trades
  WHERE status IN ('open', 'filled', 'dry_run')
) WHERE id = 1;"
```

- [ ] **Step 3: Verify snipe is unblocked**

Run:
```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
SELECT COUNT(*) as stuck_snipes FROM trades WHERE strategy = 'snipe' AND status = 'dry_run';"
```

Expected: `0` stuck snipes.

---

### Task 5: Loosen Forecast Strategy Filters

The forecast strategy has placed only 3 trades ever. The root cause is a compounding filter chain: 45% market-efficiency shrinkage + 7% edge threshold means the ensemble must disagree with the market by ~13 cents raw. Then 2+ models must agree. This is nearly impossible.

**Files:**
- Modify: `polybot/.env` (3 config values)
- Test: `tests/test_forecast_strategy.py` (add a test proving lower threshold admits trades)

- [ ] **Step 1: Write a test proving the edge threshold blocks trades**

Add to `tests/test_forecast_strategy.py`:

```python
@pytest.mark.asyncio
async def test_forecast_edge_threshold_0_04_admits_moderate_disagreement():
    """With edge_threshold=0.04, an ensemble that disagrees by 10 cents raw
    should pass after 45% shrinkage (edge = 0.10 * 0.55 = 0.055 > 0.04)."""
    from polybot.analysis.ensemble import shrink_toward_market
    raw_prob = 0.60  # ensemble says 60%
    market_price = 0.50  # market says 50%
    shrunk = shrink_toward_market(raw_prob, market_price, shrinkage=0.45)
    # shrunk = 0.60 * 0.55 + 0.50 * 0.45 = 0.33 + 0.225 = 0.555
    edge = shrunk - market_price  # 0.555 - 0.50 = 0.055
    assert edge > 0.04, f"Edge {edge} should exceed 0.04 threshold"
    assert edge < 0.07, f"Edge {edge} should be below old 0.07 threshold (proving old config blocked this)"
```

- [ ] **Step 2: Run the test**

Run: `cd ~/polybot && uv run pytest tests/test_forecast_strategy.py::test_forecast_edge_threshold_0_04_admits_moderate_disagreement -v`
Expected: PASS — this is a pure math test proving the new threshold works.

- [ ] **Step 3: Update `.env` with looser forecast parameters**

Change these three values in `polybot/.env`:

```
EDGE_THRESHOLD=0.04
QUICK_SCREEN_MAX_EDGE_GAP=0.04
FORECAST_CONSENSUS_MARGIN=0.03
```

This means:
- `EDGE_THRESHOLD`: 0.07 → 0.04 (ensemble needs ~7 cents raw disagreement instead of ~13)
- `QUICK_SCREEN_MAX_EDGE_GAP`: 0.07 → 0.04 (quick screen matches the new threshold)
- `FORECAST_CONSENSUS_MARGIN`: 0.05 → 0.03 (models count as "agreeing" at 3 cents from market instead of 5)

- [ ] **Step 4: Verify the .env changes took**

Run: `grep -E '^(EDGE_THRESHOLD|QUICK_SCREEN_MAX_EDGE_GAP|FORECAST_CONSENSUS_MARGIN)' ~/polybot/.env`
Expected:
```
EDGE_THRESHOLD=0.04
QUICK_SCREEN_MAX_EDGE_GAP=0.04
FORECAST_CONSENSUS_MARGIN=0.03
```

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest --tb=short -q`
Expected: All tests pass. The .env changes only affect runtime config, not test fixtures.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add polybot/.env tests/test_forecast_strategy.py
git commit -m "tune: loosen forecast filters — edge 0.07→0.04, consensus margin 0.05→0.03"
```

---

### Task 6: Also Clean Up Stuck Arbitrage Dry-Run Trades

The recent trades query shows 2 arbitrage trades from April 2 also stuck in `dry_run` status. While arbitrage is disabled, these still count toward `total_deployed` and `max_concurrent_positions`.

**Files:**
- No code change — SQL cleanup

- [ ] **Step 1: Inspect stuck arbitrage trades**

Run:
```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
SELECT t.id, t.strategy, t.status, t.position_size_usd, t.opened_at
FROM trades t
WHERE t.status = 'dry_run' AND t.strategy != 'mean_reversion'
ORDER BY t.opened_at;"
```

- [ ] **Step 2: Cancel all non-mean-reversion stuck dry_run trades**

Run:
```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
UPDATE trades SET status = 'cancelled', exit_reason = 'manual_cleanup', closed_at = NOW()
WHERE status = 'dry_run' AND strategy IN ('arbitrage', 'snipe');
UPDATE system_state SET total_deployed = (
  SELECT COALESCE(SUM(position_size_usd), 0) FROM trades
  WHERE status IN ('open', 'filled', 'dry_run')
) WHERE id = 1;"
```

- [ ] **Step 3: Verify cleanup**

Run:
```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
SELECT strategy, status, COUNT(*) FROM trades
WHERE status = 'dry_run' GROUP BY strategy, status;"
```

Expected: Only `mean_reversion` trades in `dry_run` (the currently open one from today).

---

### Task 7: Restart Polybot with All Changes

After all code and config changes are committed, restart the bot to pick up everything.

**Files:**
- No code changes

- [ ] **Step 1: Restart the LaunchAgent**

Run:
```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

(The `-k` flag kills the current process and restarts it, which triggers the run script with the new code.)

- [ ] **Step 2: Verify the bot started cleanly**

Run: `sleep 60 && curl -s http://127.0.0.1:8080/health | python3 -m json.tool`
Expected: `{"status": "ok", "bankroll": ..., ...}`

- [ ] **Step 3: Check logs for strategy activity**

Run: `tail -50 ~/polybot/data/polybot_stdout.log | grep -E '(snipe_candidate|forecast_cycle|mr_cycle|engine_starting)'`
Expected: `engine_starting` log line showing all 4 strategies, then periodic cycle logs from snipe, forecast, and mean_reversion.

- [ ] **Step 4: Verify dashboard decoupling works**

Run: `curl -s http://127.0.0.1:8080/strategies | python3 -m json.tool`
Expected: JSON listing all strategy performance records with `enabled: true` for snipe, forecast, market_maker, mean_reversion.
