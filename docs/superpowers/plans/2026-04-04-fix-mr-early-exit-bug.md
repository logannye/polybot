# Fix Mean Reversion Early Exit Bug — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the position manager from immediately killing mean reversion trades via the generic `should_early_exit` path, which misinterprets the stored TP target price as an ensemble probability estimate.

**Architecture:** The fix adds a `continue` after the mean reversion custom exit block in `ActivePositionManager.check_positions()` so MR trades never fall through to the generic early exit check. MR trades already have their own TP/SL/time-stop logic via `kelly_inputs` — the generic path was never intended for them and produces a degenerate signal.

**Tech Stack:** Python 3.13, pytest, asyncio, AsyncMock

---

## Bug Summary

**Root cause:** Two thresholds are accidentally equal, creating an instant-kill condition.

1. Mean reversion's minimum `expected_reversion` = `trigger_threshold` (0.05) × `reversion_fraction` (0.40) = **0.02**
2. Position manager's `early_exit_edge` config = **0.02**

**Execution path for the bug:**

1. MR detects a 5% move (minimum trigger) → `expected_reversion = 0.02`
2. MR stores `tp_yes_price` as `ensemble_probability` in the analyses table (`mean_reversion.py:218`)
3. Position manager runs MR's custom TP/SL check (`position_manager.py:158-202`) — neither fires at entry
4. Code **falls through** to generic checks (`position_manager.py:204-225`)
5. `should_early_exit()` computes: `remaining_edge = tp_yes_price - current_price = 0.02`
6. `0.02 <= early_exit_edge (0.02)` → **True** → immediate exit with $0 PnL

**Impact:** 5 of 20 MR trades since v6 (25%) were killed within 19-56 seconds at $0 PnL. All had `expected_reversion = 0.02`. Trades with `expected_reversion > 0.02` were unaffected and produced +$3.14 net.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `polybot/trading/position_manager.py` | Modify (line ~202) | Add `continue` after MR custom exit block |
| `tests/test_position_manager.py` | Modify (append) | Add 3 new tests for MR early exit isolation |

No new files. Two files modified.

---

### Task 1: Write failing test — MR trade with min-edge should NOT early_exit

This test reproduces the exact bug: a mean reversion YES trade with `expected_reversion = 0.02` at a price unchanged from entry. Before the fix, this triggers `early_exit`. After the fix, no exit fires.

**Files:**
- Modify: `tests/test_position_manager.py` (append at end)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_mr_min_edge_no_early_exit():
    """MR trade with expected_reversion=0.02 must NOT trigger generic early_exit.

    Bug: MR stores tp_yes_price as ensemble_probability. The generic
    should_early_exit computes remaining_edge = tp - current = 0.02,
    which equals early_exit_edge (0.02), triggering immediate exit.
    """
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 100, "side": "YES", "entry_price": 0.595, "shares": 5.97,
        "position_size_usd": 3.55, "strategy": "mean_reversion",
        "status": "dry_run", "opened_at": datetime.now(timezone.utc),
        "polymarket_id": "mkt-mr-min", "question": "MR min edge test?",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=168),
        "ensemble_probability": 0.615,  # this is tp_yes_price, NOT ensemble prob
        "kelly_inputs": {
            "move": -0.05, "old_price": 0.645, "trigger_price": 0.595,
            "expected_reversion": 0.02, "tp_yes_price": 0.615,
            "sl_yes_price": 0.5825, "max_hold_hours": 24.0,
        },
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    # Price unchanged from entry — no TP or SL hit
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr-min": {"yes_price": 0.595, "no_price": 0.405},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Must NOT exit — the MR custom block found no TP/SL, and generic
    # early_exit should be skipped entirely for mean_reversion trades
    executor.exit_position.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py::test_mr_min_edge_no_early_exit -v`

Expected: FAIL — `executor.exit_position` was called with `exit_reason="early_exit"`, but we asserted `assert_not_called()`.

---

### Task 2: Write failing test — MR NO-side trade with min-edge

Same bug from the NO side. Ensures the fix handles both sides.

**Files:**
- Modify: `tests/test_position_manager.py` (append at end)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_mr_min_edge_no_side_no_early_exit():
    """MR NO-side trade with expected_reversion=0.02 must NOT early_exit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 101, "side": "NO", "entry_price": 0.445, "shares": 5.82,
        "position_size_usd": 2.59, "strategy": "mean_reversion",
        "status": "dry_run", "opened_at": datetime.now(timezone.utc),
        "polymarket_id": "mkt-mr-no", "question": "MR NO side test?",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=168),
        "ensemble_probability": 0.535,  # tp_yes_price, NOT ensemble prob
        "kelly_inputs": {
            "move": 0.05, "old_price": 0.505, "trigger_price": 0.555,
            "expected_reversion": 0.02, "tp_yes_price": 0.535,
            "sl_yes_price": 0.5675, "max_hold_hours": 24.0,
        },
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr-no": {"yes_price": 0.555, "no_price": 0.445},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py::test_mr_min_edge_no_side_no_early_exit -v`

Expected: FAIL — same generic early_exit bug fires for NO side.

---

### Task 3: Write failing test — MR TP/SL still works after fix

Ensures the fix doesn't break the MR custom exit path — trades that hit take-profit must still exit correctly.

**Files:**
- Modify: `tests/test_position_manager.py` (append at end)

- [ ] **Step 1: Write the failing test (this one should PASS even before the fix — it's a guardrail)**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_mr_custom_tp_still_fires():
    """MR trade that hits its tp_yes_price must still exit as take_profit."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 102, "side": "YES", "entry_price": 0.24, "shares": 18.32,
        "position_size_usd": 4.37, "strategy": "mean_reversion",
        "status": "dry_run", "opened_at": datetime.now(timezone.utc),
        "polymarket_id": "mkt-mr-tp", "question": "MR TP test?",
        "resolution_time": datetime.now(timezone.utc) + timedelta(hours=168),
        "ensemble_probability": 0.306,
        "kelly_inputs": {
            "move": -0.066, "old_price": 0.306, "trigger_price": 0.24,
            "expected_reversion": 0.0264, "tp_yes_price": 0.2664,
            "sl_yes_price": 0.2235, "max_hold_hours": 24.0,
        },
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=1.24)

    scanner = MagicMock()
    # Price reverted past the TP target (0.306 > 0.2664)
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr-tp": {"yes_price": 0.306, "no_price": 0.694},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=102, exit_price=0.306, exit_reason="take_profit")
```

- [ ] **Step 2: Run test to verify it passes (guardrail — confirms TP path works pre-fix)**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py::test_mr_custom_tp_still_fires -v`

Expected: PASS — the custom MR TP check fires before the generic path.

- [ ] **Step 3: Run all three new tests together to see the two failures**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py::test_mr_min_edge_no_early_exit tests/test_position_manager.py::test_mr_min_edge_no_side_no_early_exit tests/test_position_manager.py::test_mr_custom_tp_still_fires -v`

Expected: 2 FAIL, 1 PASS.

- [ ] **Step 4: Commit the tests**

```bash
cd ~/polybot
git add tests/test_position_manager.py
git commit -m "test: add failing tests for MR early exit bug

Three tests covering the mean reversion instant-kill bug where trades
with expected_reversion=0.02 are immediately exited by the generic
should_early_exit path. Tests cover YES side, NO side, and a guardrail
confirming the custom TP path still works.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Fix the bug — skip generic early_exit for mean reversion trades

**Files:**
- Modify: `polybot/trading/position_manager.py:158-202`

- [ ] **Step 1: Apply the fix**

In `polybot/trading/position_manager.py`, find this block (around line 158-202):

```python
            # Mean-reversion custom exit: use stored price targets from kelly_inputs
            if pos["strategy"] == "mean_reversion" and pos.get("kelly_inputs"):
                try:
                    ki = json.loads(pos["kelly_inputs"]) if isinstance(pos["kelly_inputs"], str) else pos["kelly_inputs"]
                    tp_yes = ki.get("tp_yes_price")
                    sl_yes = ki.get("sl_yes_price")
                    max_hold = ki.get("max_hold_hours", 24.0)

                    # Take-profit: price reverted toward target
                    if tp_yes is not None:
                        if (pos["side"] == "NO" and current_yes_price <= tp_yes) or \
                           (pos["side"] == "YES" and current_yes_price >= tp_yes):
                            exit_reason = "take_profit"

                    # Stop-loss: price moved further against us
                    if not exit_reason and sl_yes is not None:
                        if (pos["side"] == "NO" and current_yes_price >= sl_yes) or \
                           (pos["side"] == "YES" and current_yes_price <= sl_yes):
                            exit_reason = "stop_loss"

                    # Time-stop: held too long
                    if not exit_reason and pos.get("opened_at"):
                        hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                        if hold_hours > max_hold:
                            exit_reason = "time_stop"
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

                if exit_reason:
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    unrealized = compute_unrealized_return(side, entry_price, current_yes_price)
                    if self._portfolio_lock:
                        async with self._portfolio_lock:
                            pnl = await self._executor.exit_position(
                                trade_id=trade_id, exit_price=exit_price,
                                exit_reason=exit_reason)
                    else:
                        pnl = await self._executor.exit_position(
                            trade_id=trade_id, exit_price=exit_price,
                            exit_reason=exit_reason)
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("mr_position_exit", trade_id=trade_id,
                                 reason=exit_reason, pnl=round(pnl, 4),
                                 market=pos["question"][:60])
                    continue
```

Replace the last line (`continue`) and add a second `continue` for the no-exit case. The block should end with:

```python
                    if pnl is not None:
                        exits_triggered += 1
                        log.info("mr_position_exit", trade_id=trade_id,
                                 reason=exit_reason, pnl=round(pnl, 4),
                                 market=pos["question"][:60])
                # MR trades use custom TP/SL/time-stop above — skip generic
                # early_exit which misinterprets tp_yes_price as ensemble prob
                continue
```

The key change: move `continue` out of the `if exit_reason:` block to be unconditional. Whether or not the custom MR check found an exit reason, the code should skip the generic checks below and proceed to the next position.

- [ ] **Step 2: Run the two previously-failing tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py::test_mr_min_edge_no_early_exit tests/test_position_manager.py::test_mr_min_edge_no_side_no_early_exit -v`

Expected: 2 PASS.

- [ ] **Step 3: Run ALL position manager tests to verify no regressions**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py -v`

Expected: All 24 tests PASS (21 existing + 3 new).

- [ ] **Step 4: Run the full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`

Expected: All tests PASS.

- [ ] **Step 5: Commit the fix**

```bash
cd ~/polybot
git add polybot/trading/position_manager.py
git commit -m "fix: prevent generic early_exit from killing MR trades

Mean reversion trades store tp_yes_price as ensemble_probability in the
analyses table. The generic should_early_exit check misinterprets this
as an ensemble estimate, computing remaining_edge = tp - current = 0.02
which exactly equals early_exit_edge (0.02), causing immediate exit.

25% of MR trades (5/20 since v6) were killed within 19-56 seconds with
\$0 PnL due to this bug. All had expected_reversion = 0.02.

Fix: make the MR custom exit block unconditionally skip the generic
exit checks. MR trades already have their own TP/SL/time-stop logic
via kelly_inputs — the generic path was never intended for them.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Verification

After deploying, monitor the next few hours of MR trades in the DB:

```sql
-- Should see zero instant early_exits for min-edge trades going forward
SELECT id, exit_reason,
       (kelly_inputs::jsonb->>'expected_reversion')::float as exp_rev,
       ROUND(EXTRACT(EPOCH FROM (closed_at - opened_at))::numeric, 0) as hold_secs
FROM trades
WHERE strategy = 'mean_reversion'
  AND opened_at > NOW() - INTERVAL '4 hours'
ORDER BY opened_at DESC;
```

Expected: no `early_exit` with `exp_rev = 0.02` and `hold_secs < 60`. Trades should resolve as `take_profit`, `stop_loss`, or `time_stop` only.
