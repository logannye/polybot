# Capital Turnover: Universal Time-Stops + Category Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure all positions exit within 12 hours and raise the per-category deployment limit to 50%, so capital recycles daily and the bot can concentrate on high-conviction opportunities.

**Architecture:** Add a `universal_max_hold_hours` config key (12h) and a `pol_max_hold_hours` key (12h). Tighten `snipe_max_hold_hours` from 6→8h and `mr_max_hold_hours` from 1→2h. Add a universal time-stop check at the top of `check_positions()` that fires before any strategy-specific logic. Raise `max_per_category_pct` from 0.25→0.50.

**Tech Stack:** Python 3.13, pytest, asyncio, pydantic-settings

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/core/config.py` | Modify | Add `universal_max_hold_hours`, `pol_max_hold_hours`; change `max_per_category_pct` default |
| `polybot/trading/position_manager.py` | Modify | Add universal time-stop check at top of position loop |
| `polybot/trading/risk.py` | No change | `max_per_category_pct` flows in via constructor from config |
| `tests/test_position_manager.py` | Modify | Add tests for universal time-stop and political time-stop |
| `tests/test_risk.py` | Modify | Update category limit test to reflect new 0.50 default |

---

### Task 1: Config Changes

**Files:**
- Modify: `polybot/core/config.py:66-68` (portfolio limits), `polybot/core/config.py:106` (snipe hold), `polybot/core/config.py:203` (MR hold)
- Modify: `polybot/core/config.py:229-236` (political strategy section)

- [ ] **Step 1: Update config defaults**

In `polybot/core/config.py`, make these changes:

1. Change `max_per_category_pct` default from `0.25` to `0.50` (line 68)
2. Change `snipe_max_hold_hours` default from `6.0` to `8.0` (line 106)
3. Change `mr_max_hold_hours` default from `1.0` to `2.0` (line 203)
4. Add `universal_max_hold_hours` and `pol_max_hold_hours` to the political strategy section:

```python
# Political calibration strategy
pol_enabled: bool = True
pol_interval_seconds: float = 600.0        # 10 min scan cycle
pol_kelly_mult: float = 0.40               # aggressive — high-conviction calibration edge
pol_max_single_pct: float = 0.20           # up to 20% bankroll per position
pol_min_edge: float = 0.04                 # min 4% calibration-adjusted edge
pol_min_liquidity: float = 50000.0         # only liquid markets
pol_llm_confirm_edge: float = 0.10         # use LLM to confirm edges above 10% (future)
pol_max_positions: int = 5                 # max concurrent political positions
pol_max_hold_hours: float = 12.0           # time-stop: free capital from stale political positions

# Universal position hold limit
universal_max_hold_hours: float = 12.0     # hard ceiling — no position held longer than this
```

- [ ] **Step 2: Verify config loads**

Run: `cd ~/polybot && uv run python -c "from polybot.core.config import Settings; s = Settings(); print(s.universal_max_hold_hours, s.pol_max_hold_hours, s.max_per_category_pct, s.snipe_max_hold_hours, s.mr_max_hold_hours)"`

Expected: `12.0 12.0 0.5 8.0 2.0`

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py
git commit -m "config: add universal/political time-stop, raise category cap to 50%

- universal_max_hold_hours=12 (hard ceiling on all positions)
- pol_max_hold_hours=12 (political positions were unlimited)
- max_per_category_pct 0.25→0.50 (let bot concentrate on high-conviction)
- snipe_max_hold_hours 6→8, mr_max_hold_hours 1→2 (data-driven adjustments)"
```

---

### Task 2: Tests for Universal and Political Time-Stops

**Files:**
- Modify: `tests/test_position_manager.py` (append new tests)
- Modify: `tests/test_risk.py` (update category limit test)

- [ ] **Step 1: Write failing test — universal time-stop fires on any strategy**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_universal_time_stop_fires():
    """Any trade held longer than universal_max_hold_hours should be time-stopped."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    opened_13h_ago = datetime.now(timezone.utc) - timedelta(hours=13)
    resolves_72h = datetime.now(timezone.utc) + timedelta(hours=72)
    db.fetch = AsyncMock(return_value=[{
        "id": 100, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-universal", "question": "Universal time-stop test?",
        "ensemble_probability": 0.65, "opened_at": opened_13h_ago,
        "resolution_time": resolves_72h, "kelly_inputs": None,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-0.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-universal": {"yes_price": 0.48, "no_price": 0.52},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 90.0
    settings.forecast_time_stop_fraction = 0.15
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0
    settings.universal_max_hold_hours = 12.0
    settings.snipe_max_hold_hours = 8.0
    settings.forecast_stop_loss_threshold = 0.10

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=100, exit_price=0.48, exit_reason="time_stop")
```

- [ ] **Step 2: Write failing test — universal time-stop does NOT fire within limit**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_universal_time_stop_does_not_fire_within_limit():
    """Trade held 11h (under 12h universal limit) should not be universally time-stopped."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    opened_11h_ago = datetime.now(timezone.utc) - timedelta(hours=11)
    resolves_72h = datetime.now(timezone.utc) + timedelta(hours=72)
    db.fetch = AsyncMock(return_value=[{
        "id": 101, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "political", "status": "dry_run",
        "polymarket_id": "mkt-ok", "question": "Within universal limit?",
        "ensemble_probability": None, "opened_at": opened_11h_ago,
        "resolution_time": resolves_72h, "kelly_inputs": None,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-ok": {"yes_price": 0.50, "no_price": 0.50},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.universal_max_hold_hours = 12.0
    settings.pol_max_hold_hours = 12.0
    settings.snipe_max_hold_hours = 8.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_not_called()
```

- [ ] **Step 3: Write failing test — political time-stop fires**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_political_time_stop_fires():
    """Political trade held longer than pol_max_hold_hours should be time-stopped."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    opened_13h_ago = datetime.now(timezone.utc) - timedelta(hours=13)
    resolves_7d = datetime.now(timezone.utc) + timedelta(days=7)
    db.fetch = AsyncMock(return_value=[{
        "id": 102, "side": "NO", "entry_price": 0.80, "shares": 50.0,
        "position_size_usd": 40.0, "strategy": "political", "status": "dry_run",
        "polymarket_id": "mkt-pol", "question": "Political time-stop test?",
        "ensemble_probability": None, "opened_at": opened_13h_ago,
        "resolution_time": resolves_7d, "kelly_inputs": None,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-1.20)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-pol": {"yes_price": 0.82, "no_price": 0.18},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.universal_max_hold_hours = 12.0
    settings.pol_max_hold_hours = 12.0
    settings.snipe_max_hold_hours = 8.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=102, exit_price=0.18, exit_reason="time_stop")
```

- [ ] **Step 4: Write failing test — universal time-stop skips profitable positions**

Append to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_universal_time_stop_skips_profitable():
    """Trade past universal limit but profitable should NOT be time-stopped (let TP handle it)."""
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    opened_13h_ago = datetime.now(timezone.utc) - timedelta(hours=13)
    resolves_72h = datetime.now(timezone.utc) + timedelta(hours=72)
    db.fetch = AsyncMock(return_value=[{
        "id": 103, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "political", "status": "dry_run",
        "polymarket_id": "mkt-profit-univ", "question": "Profitable past universal?",
        "ensemble_probability": None, "opened_at": opened_13h_ago,
        "resolution_time": resolves_72h, "kelly_inputs": None,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    # 10% gain — profitable but below 20% TP
    scanner.get_all_cached_prices.return_value = {
        "mkt-profit-univ": {"yes_price": 0.55, "no_price": 0.45},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.universal_max_hold_hours = 12.0
    settings.pol_max_hold_hours = 12.0
    settings.snipe_max_hold_hours = 8.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Profitable: universal time-stop skipped, below TP threshold, no exit
    executor.exit_position.assert_not_called()
```

- [ ] **Step 5: Update risk test for new category cap default**

In `tests/test_risk.py`, update `TestStrategyAwareRiskLimits::test_updated_risk_defaults` (line 156-163). The `RiskManager()` default constructor will now have `max_per_category_pct=0.50` instead of `0.25`. However, the existing fixture at line 8-12 hardcodes `max_per_category_pct=0.25` so those tests are unaffected. Only update the defaults test:

```python
def test_updated_risk_defaults(self):
    rm = RiskManager()
    assert rm.max_total_deployed_pct == 0.70
    assert rm.max_per_category_pct == 0.50  # was 0.25
    assert rm.max_concurrent == 12
    assert rm.daily_loss_limit_pct == 0.15
    assert rm.circuit_breaker_hours == 6
    assert rm.min_trade_size == 1.0
```

- [ ] **Step 6: Run tests — all new tests should FAIL**

Run: `cd ~/polybot && uv run python -m pytest tests/test_position_manager.py::test_universal_time_stop_fires tests/test_position_manager.py::test_universal_time_stop_does_not_fire_within_limit tests/test_position_manager.py::test_political_time_stop_fires tests/test_position_manager.py::test_universal_time_stop_skips_profitable tests/test_risk.py::TestStrategyAwareRiskLimits::test_updated_risk_defaults -v`

Expected: 5 failures (new settings attributes not read, universal check not implemented, RiskManager default unchanged)

- [ ] **Step 7: Commit failing tests**

```bash
cd ~/polybot && git add tests/test_position_manager.py tests/test_risk.py
git commit -m "test: add failing tests for universal time-stop and category cap change"
```

---

### Task 3: Implement Universal Time-Stop in Position Manager

**Files:**
- Modify: `polybot/trading/position_manager.py:39-57` (constructor), `polybot/trading/position_manager.py:94` (top of position loop)

- [ ] **Step 1: Add universal_max_hold_hours and pol_max_hold_hours to constructor**

In `polybot/trading/position_manager.py`, update `__init__` to read the new settings:

After line 57 (`self._snipe_max_hold_hours = ...`), add:

```python
self._universal_max_hold_hours = getattr(settings, 'universal_max_hold_hours', 12.0)
self._pol_max_hold_hours = getattr(settings, 'pol_max_hold_hours', 12.0)
```

- [ ] **Step 2: Add universal time-stop check at top of position loop**

In `check_positions()`, insert a universal time-stop block immediately after the `if not market_data: continue` check (after line 97) and before any strategy-specific logic. This fires before forecast/snipe/MR/political checks:

```python
            # Universal time-stop: hard ceiling on all positions.
            # Skips profitable positions — let TP handle those.
            if pos.get("opened_at") is not None:
                hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                if hold_hours > self._universal_max_hold_hours:
                    unrealized = compute_unrealized_return(side, entry_price, current_yes_price)
                    if unrealized <= 0:
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
                            log.info("universal_time_stop", trade_id=trade_id,
                                     hold_hours=round(hold_hours, 1),
                                     pnl=round(pnl, 4),
                                     market=pos["question"][:60])
                            await self._email.send(
                                f"[POLYBOT] Universal time-stop ({hold_hours:.0f}h)",
                                f"<p><b>Market:</b> {pos['question']}</p>"
                                f"<p><b>Strategy:</b> {pos['strategy']} | "
                                f"Held: {hold_hours:.0f}h (limit: "
                                f"{self._universal_max_hold_hours:.0f}h) | "
                                f"P&L: ${pnl:+.2f}</p>")
                        continue
```

- [ ] **Step 3: Add political time-stop to the political strategy block**

In the political strategy block (currently starting at line 240 `if pos["strategy"] == "political":`), add a time-stop check before the TP/SL checks. Insert after the `if pos["strategy"] == "political":` line:

```python
            if pos["strategy"] == "political":
                # Political time-stop: free capital from stale positions
                if pos.get("opened_at") is not None:
                    hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                    if hold_hours > self._pol_max_hold_hours:
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
                            log.info("pol_time_stop", trade_id=trade_id,
                                     hold_hours=round(hold_hours, 1),
                                     pnl=round(pnl, 4),
                                     market=pos["question"][:60])
                            await self._email.send(
                                f"[POLYBOT] Political time-stop ({hold_hours:.0f}h)",
                                f"<p><b>Market:</b> {pos['question']}</p>"
                                f"<p><b>Held:</b> {hold_hours:.0f}h (limit: "
                                f"{self._pol_max_hold_hours:.0f}h) | "
                                f"P&L: ${pnl:+.2f}</p>")
                        continue

                # Existing TP/SL logic follows unchanged...
                if should_take_profit(side, entry_price, current_yes_price,
                                      self._take_profit):
```

- [ ] **Step 4: Update RiskManager default**

In `polybot/trading/risk.py`, change the `__init__` signature default for `max_per_category_pct` from `0.25` to `0.50`:

```python
def __init__(self, max_single_pct=0.15, max_total_deployed_pct=0.70,
             max_per_category_pct=0.50, max_concurrent=12,
             daily_loss_limit_pct=0.15, circuit_breaker_hours=6,
             min_trade_size=1.0, book_depth_max_pct=0.10):
```

- [ ] **Step 5: Run all tests**

Run: `cd ~/polybot && uv run python -m pytest tests/test_position_manager.py tests/test_risk.py -v`

Expected: All tests pass (64 existing + 4 new = 68 position manager tests, 24 risk tests)

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/trading/position_manager.py polybot/trading/risk.py
git commit -m "feat: universal 12h time-stop + political time-stop + category cap 50%

- Universal time-stop at top of position loop (skips profitable)
- Political positions now time-stopped at 12h (were unlimited)
- max_per_category_pct 0.25→0.50 in RiskManager default
- Data-driven: winners resolve fast, long holds are dead capital"
```

---

### Task 4: Update .env and Verify End-to-End

**Files:**
- Modify: `~/polybot/.env` (add new keys)

- [ ] **Step 1: Add new config keys to .env**

Append to `.env`:

```
# Capital turnover — universal time-stop
UNIVERSAL_MAX_HOLD_HOURS=12.0
POL_MAX_HOLD_HOURS=12.0
```

Also update existing keys if they differ from new defaults:

```
SNIPE_MAX_HOLD_HOURS=8.0
MR_MAX_HOLD_HOURS=2.0
MAX_PER_CATEGORY_PCT=0.50
```

- [ ] **Step 2: Run full test suite**

Run: `cd ~/polybot && uv run python -m pytest tests/ -v --tb=short`

Expected: All tests pass.

- [ ] **Step 3: Commit .env changes**

```bash
cd ~/polybot && git add .env
git commit -m "env: configure capital turnover parameters"
```
