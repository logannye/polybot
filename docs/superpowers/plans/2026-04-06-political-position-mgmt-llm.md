# Political Position Management + LLM Confirmation — Plan A

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the position management gap where political trades get time-stopped/early-exited despite intending hold-to-resolution, and add optional LLM confirmation for high-edge political trades.

**Architecture:** Add strategy-aware exit logic to position_manager.py (political trades skip time-stop and early-exit, relying only on take-profit/stop-loss). Add an LLM confirmation gate to PoliticalStrategy for trades above the `pol_llm_confirm_edge` threshold.

**Tech Stack:** Python 3.13, asyncpg, structlog, pytest

---

## File Structure

| File | Responsibility |
|------|---------------|
| `polybot/trading/position_manager.py` | **Modify**: Add political strategy exit rules (hold-to-resolution) |
| `polybot/strategies/political.py` | **Modify**: Add optional LLM confirmation for high-edge trades |
| `tests/test_position_manager.py` | **Modify**: Add test for political hold-to-resolution |
| `tests/test_political_strategy.py` | **Modify**: Add test for LLM confirmation gate |

---

### Task 1: Political hold-to-resolution in position manager

Political trades should hold to resolution. They should NOT be time-stopped or early-exited. Only take-profit and stop-loss should apply.

**Files:**
- Modify: `polybot/trading/position_manager.py`
- Modify: `tests/test_position_manager.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_position_manager.py` a test that creates a political trade that's been held for 12 hours (underwater) and verifies it is NOT time-stopped or early-exited:

```python
@pytest.mark.asyncio
async def test_political_trades_skip_time_stop():
    """Political trades should hold to resolution — no time-stop, no early-exit."""
    # Create a political trade that's been open 12h, underwater, 30 days to resolution
    # The forecast time-stop would fire at ~90min. Verify political skips it entirely.
    ...
```

The test should mock a trade with `strategy='political'`, opened 12h ago, resolution 30 days out, currently at -5% unrealized. Assert the position manager does NOT close it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py -k political -v`
Expected: FAIL (no political-specific logic exists)

- [ ] **Step 3: Add political exit logic to position_manager.py**

In `check_positions()`, add a guard early in the per-position loop (after fetching current price, before any exit checks):

```python
            # Political strategy: hold to resolution.
            # Only stop-loss applies — skip time-stop and early-exit.
            if pos["strategy"] == "political":
                # Stop-loss still applies as a safety net
                if should_stop_loss(current_yes_price, float(pos["entry_price"]),
                                    pos["side"], stop_loss_threshold):
                    # ... execute stop-loss same as generic path ...
                    pass
                # Take-profit also applies
                elif should_take_profit(current_yes_price, float(pos["entry_price"]),
                                        pos["side"], take_profit_threshold):
                    # ... execute take-profit same as generic path ...
                    pass
                continue  # Skip all other exit logic (time-stop, early-exit)
```

Insert this block after the mean-reversion custom exit block (after line ~233) and before the generic take-profit/stop-loss checks.

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_position_manager.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/trading/position_manager.py tests/test_position_manager.py
git commit -m "feat: political trades hold to resolution — skip time-stop and early-exit"
```

---

### Task 2: LLM confirmation for high-edge political trades

When calibration edge exceeds `pol_llm_confirm_edge` (default 10%), use the LLM ensemble's quick_screen to validate before trading. This filters out false positives from calibration alone.

**Files:**
- Modify: `polybot/strategies/political.py`
- Modify: `tests/test_political_strategy.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_political_strategy.py`:

```python
@pytest.mark.asyncio
async def test_llm_confirmation_blocks_disagreeing_trade():
    """When LLM quick_screen disagrees with calibration, trade should be skipped."""
    ...

@pytest.mark.asyncio
async def test_llm_confirmation_allows_agreeing_trade():
    """When LLM confirms calibration edge, trade proceeds."""
    ...

@pytest.mark.asyncio
async def test_no_llm_when_below_confirm_threshold():
    """Trades below pol_llm_confirm_edge should proceed without LLM call."""
    ...
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Add LLM confirmation to PoliticalStrategy._execute_trade**

In `_execute_trade()`, after computing Kelly but before the risk check, add:

```python
        # Optional LLM confirmation for high-edge trades
        if self._ensemble and edge >= self._llm_confirm_edge:
            quick_prob = await self._ensemble.quick_screen(
                m["question"], m["yes_price"],
                m["resolution_time"].isoformat() if hasattr(m["resolution_time"], "isoformat") else str(m["resolution_time"]))
            if quick_prob is not None:
                llm_agrees = (side == "YES" and quick_prob > m["yes_price"]) or \
                             (side == "NO" and quick_prob < m["yes_price"])
                if not llm_agrees:
                    log.info("pol_llm_disagrees", market=m["polymarket_id"],
                             side=side, calibration_edge=round(edge, 4),
                             llm_prob=round(quick_prob, 4))
                    return
                log.info("pol_llm_confirms", market=m["polymarket_id"],
                         side=side, llm_prob=round(quick_prob, 4))
```

Also add `self._llm_confirm_edge` to `__init__` (read from `settings.pol_llm_confirm_edge`, default 0.10).

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_political_strategy.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest -x -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add polybot/strategies/political.py tests/test_political_strategy.py
git commit -m "feat: optional LLM confirmation for high-edge political trades"
```

---

### Task 3: Restart and verify

- [ ] **Step 1: Restart Polybot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 2: Verify political trades are not being time-stopped**

Wait 15 minutes, then check:

```bash
grep "political.*time_stop\|political.*early_exit" ~/polybot/data/polybot_stdout.log
```

Expected: No matches (political trades should never trigger these exits).

- [ ] **Step 3: Verify LLM confirmation logs**

```bash
grep "pol_llm" ~/polybot/data/polybot_stdout.log | tail -5
```

Expected: `pol_llm_confirms` or `pol_llm_disagrees` entries (if any trades exceed the 10% edge threshold).
