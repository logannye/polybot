# Profitability Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the daily bleeding from forecast (-$4-6/day), free locked snipe capital, increase MR capacity, and stay aggressive during compounding — turning Polybot from net-negative (-$129 over 7 days) to consistently profitable.

**Architecture:** Four changes: (1) add `forecast_enabled` config gate so forecast can be disabled, (2) add snipe position time-stop in position_manager.py to free stale capital, (3) raise MR concurrent cap via .env, (4) raise bankroll growth threshold via .env to stay aggressive longer.

**Tech Stack:** Python 3.13, pytest, pydantic Settings (.env)

---

### Task 1: Add forecast_enabled Config Gate and Disable Forecast

**Files:**
- Modify: `polybot/core/config.py:121` (add config key)
- Modify: `polybot/__main__.py:149-150` (add guard)
- Modify: `.env` (add FORECAST_ENABLED=false)

Forecast has -$27.96 all-time PnL (31.5% win rate, 19 stop-losses at -$100.85 wiping out 12 take-profits at +$87.44). Currently it's always-on — `__main__.py` unconditionally adds `EnsembleForecastStrategy` to the engine (line 149). MR and MM already have `if settings.mr_enabled` / `if settings.mm_enabled` guards. Forecast needs the same pattern.

- [ ] **Step 1: Add `forecast_enabled` config key**

In `polybot/core/config.py`, find the line:

```python
    forecast_category_filter_enabled: bool = True      # disable to skip category filtering
```

Add above it:

```python
    forecast_enabled: bool = True
```

- [ ] **Step 2: Add guard in __main__.py**

In `polybot/__main__.py`, change lines 149-150 from:

```python
    engine.add_strategy(EnsembleForecastStrategy(
        settings=settings, ensemble=ensemble, researcher=researcher))
```

To:

```python
    if getattr(settings, 'forecast_enabled', True):
        engine.add_strategy(EnsembleForecastStrategy(
            settings=settings, ensemble=ensemble, researcher=researcher))
```

- [ ] **Step 3: Disable forecast in .env**

Add to `.env`:

```
FORECAST_ENABLED=false
```

- [ ] **Step 4: Run full test suite**

```bash
cd ~/polybot && .venv/bin/python -m pytest -x -q
```

Expected: All tests pass (no tests depend on forecast being registered).

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py polybot/__main__.py && git commit -m "$(cat <<'EOF'
feat: add forecast_enabled config gate, disable forecast

Forecast has -$27.96 all-time PnL (31.5% win rate). 19 stop-losses
(-$100.85) wipe out 12 take-profits (+$87.44). Disabling until
positive expectation is demonstrated over 100+ more trades.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add Snipe Position Time-Stop (48h)

**Files:**
- Modify: `polybot/core/config.py` (add config key)
- Modify: `polybot/trading/position_manager.py:80` (add snipe time-stop block)
- Test: `tests/test_position_manager.py`

Snipe positions lock up capital waiting for resolution. The two currently open snipe positions ($215 total) have been sitting since yesterday earning nothing. Adding a 48h time-stop frees capital for MR trades. The position_manager already has time-stop logic for forecast (lines 108-153) and MR (lines 158-204) — snipe needs the same pattern.

The snipe time-stop should fire on ALL snipe positions held >48h regardless of P&L (unlike forecast's time-stop which skips profitable positions). Snipe positions that haven't resolved in 48h are unlikely to converge — better to free the capital.

- [ ] **Step 1: Add config key**

In `polybot/core/config.py`, after the existing snipe settings (find `snipe_max_market_exposure_pct`), add:

```python
    snipe_max_hold_hours: float = 48.0
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_snipe_time_stop_48h():
    """Snipe position held >48h should trigger time_stop exit."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    db.fetch = AsyncMock(return_value=[{
        "id": 55, "side": "NO", "entry_price": 0.96, "shares": 100.0,
        "position_size_usd": 107.50, "strategy": "snipe", "status": "dry_run",
        "polymarket_id": "mkt-snipe-stale", "question": "Stale snipe?",
        "ensemble_probability": None, "resolution_time": None,
        "opened_at": datetime.now(timezone.utc) - timedelta(hours=49),
        "kelly_inputs": None,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-0.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-snipe-stale": {"yes_price": 0.04, "no_price": 0.96},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.snipe_max_hold_hours = 48.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=55, exit_price=0.96, exit_reason="time_stop")
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_position_manager.py::test_snipe_time_stop_48h -v
```

Expected: FAIL — no snipe time-stop logic exists yet.

- [ ] **Step 4: Add snipe time-stop logic to position_manager.py**

In `polybot/trading/position_manager.py`, in the `__init__` method, after line 54 (`self._portfolio_lock = portfolio_lock`), add:

```python
        self._snipe_max_hold_hours = getattr(settings, 'snipe_max_hold_hours', 48.0)
```

Then in the `check_positions` method, after the forecast time-stop block (after line 153 `continue`) and before line 155 (`exit_reason = None`), add a snipe time-stop block:

```python
            # Snipe time-stop: free capital from stale positions
            if pos["strategy"] == "snipe" and pos.get("opened_at") is not None:
                hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                if hold_hours > self._snipe_max_hold_hours:
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    if self._portfolio_lock:
                        async with self._portfolio_lock:
                            pnl = await self._executor.exit_position(
                                trade_id=trade_id, exit_price=exit_price,
                                exit_reason="time_stop")
                    else:
                        pnl = await self._executor.exit_position(
                            trade_id=trade_id, exit_price=exit_price,
                            exit_reason="time_stop")
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("snipe_time_stop", trade_id=trade_id,
                                 hold_hours=round(hold_hours, 1),
                                 pnl=round(pnl, 4),
                                 market=pos["question"][:60])
                        await self._email.send(
                            f"[POLYBOT] Snipe time-stopped ({hold_hours:.0f}h)",
                            f"<p><b>Market:</b> {pos['question']}</p>"
                            f"<p><b>Held:</b> {hold_hours:.0f}h (limit: "
                            f"{self._snipe_max_hold_hours:.0f}h) | "
                            f"P&L: ${pnl:+.2f}</p>")
                    continue

```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_position_manager.py::test_snipe_time_stop_48h -v
```

Expected: PASS

- [ ] **Step 6: Run all position manager tests**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_position_manager.py -v
```

Expected: All tests pass. Important: verify `test_check_positions_no_time_stop_for_snipe` still passes — it tests a 3h-old snipe (below 48h threshold).

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py polybot/trading/position_manager.py tests/test_position_manager.py && git commit -m "$(cat <<'EOF'
feat: add 48h time-stop for snipe positions

Snipe positions lock up capital waiting for resolution. Two open
positions ($215) have been sitting since yesterday. 48h time-stop
frees capital for MR trades which earn 7% ROI vs snipe's 0.04%.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Raise MR Concurrent Cap and Growth Threshold

**Files:**
- Modify: `.env`

Two config changes to increase MR capacity and maintain aggression during compounding.

- [ ] **Step 1: Raise MR max concurrent from 5 to 8**

Add to `.env`:

```
MR_MAX_CONCURRENT=8
```

With $494 bankroll, 15% max single position ($74), and 3h time-stop freeing capital fast, 8 positions won't over-deploy. The real limiter is the 70% total deployment cap ($346).

- [ ] **Step 2: Raise bankroll growth threshold from $500 to $1000**

Add to `.env`:

```
BANKROLL_GROWTH_THRESHOLD=1000
```

Currently at $494 bankroll, we're about to hit the $500 growth threshold which reduces all Kelly multipliers by 15%. That's premature — we want to stay aggressive through the compounding phase up to $1000.

- [ ] **Step 3: Verify .env changes**

```bash
grep -E "MR_MAX_CONCURRENT|BANKROLL_GROWTH" ~/polybot/.env
```

Expected:
```
MR_MAX_CONCURRENT=8
BANKROLL_GROWTH_THRESHOLD=1000
```

---

### Task 4: Restart and Verify

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

- [ ] **Step 3: Verify forecast is disabled**

```bash
sleep 15 && grep "engine_starting" ~/polybot/data/polybot_stdout.log | tail -1
```

Expected: The `strategies` list should NOT include `"forecast"`. Should show: `["snipe", "market_maker", "mean_reversion", "cross_venue"]`.

- [ ] **Step 4: Verify snipe time-stop will fire on stale positions**

The two open snipe positions (ids 486, 487) were opened ~24h ago. They won't hit the 48h time-stop yet, but will within the next 24h. Monitor with:

```bash
grep "snipe_time_stop" ~/polybot/data/polybot_stdout.log | tail -3
```

- [ ] **Step 5: Push to GitHub**

```bash
cd ~/polybot && git push origin main
```
