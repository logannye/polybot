# Snipe Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Maximize 48-hour returns on a $500 bankroll by fixing capital leaks, supercharging snipe with tiered sizing and cooldowns, rehabilitating forecast with time-stops and blacklists, and gating arbitrage behind a bankroll minimum.

**Architecture:** Five focused code changes to existing strategy files + config, plus a schema migration. No new modules — all changes are additions to existing classes. Pure functions extracted for testability.

**Tech Stack:** Python 3.13, asyncpg, pytest, pytest-asyncio, structlog

**Spec:** `docs/superpowers/specs/2026-03-31-snipe-factory-design.md`

---

## File Map

**Modified files:**
- `polybot/core/config.py` — 5 new config keys
- `polybot/db/schema.sql` — exit_reason constraint update
- `polybot/strategies/snipe.py` — Per-market cooldown, tiered edge sizing, re-entry logic
- `polybot/strategies/forecast.py` — Market loss blacklist
- `polybot/strategies/arbitrage.py` — Bankroll gate
- `polybot/trading/position_manager.py` — 2-hour time-stop for forecast
- `tests/test_snipe.py` — Cooldown, tiered sizing tests
- `tests/test_position_manager.py` — Time-stop tests
- `tests/test_forecast_strategy.py` — Blacklist test
- `.env` — Config value updates

**No new files.**

---

### Task 1: Config Keys & Schema Migration

**Files:**
- Modify: `polybot/core/config.py:22-90`
- Modify: `polybot/db/schema.sql:115-118`

- [ ] **Step 1: Add 5 new config keys to Settings class**

In `polybot/core/config.py`, add after the `position_check_interval` field (line 89):

```python
    # Snipe cooldown & re-entry
    snipe_cooldown_hours: float = 4.0
    snipe_reentry_threshold: float = 0.03
    snipe_max_entries_per_market: int = 3

    # Arb bankroll gate
    arb_min_bankroll: float = 2000.0

    # Forecast time-stop
    forecast_time_stop_minutes: float = 120.0
```

- [ ] **Step 2: Update exit_reason CHECK constraint in schema.sql**

Replace lines 115-118 in `polybot/db/schema.sql`:

```sql
-- v2.3: Expand exit_reason for time-stop exits
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;
ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IN ('resolution', 'early_exit', 'stop_loss', 'take_profit', 'time_stop'));
```

- [ ] **Step 3: Apply schema migration to running database**

Run:
```bash
cd /Users/logannye/polybot && /opt/homebrew/Cellar/postgresql@16/16.12/bin/psql postgresql://logannye@localhost:5432/polybot -c "
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;
ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IN ('resolution', 'early_exit', 'stop_loss', 'take_profit', 'time_stop'));"
```

Expected: `ALTER TABLE` (twice)

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `cd /Users/logannye/polybot && uv run pytest -x -q`
Expected: `231 passed`

- [ ] **Step 5: Commit**

```bash
cd /Users/logannye/polybot && git add polybot/core/config.py polybot/db/schema.sql && git commit -m "feat: add config keys and schema for snipe factory strategy"
```

---

### Task 2: Snipe Per-Market Cooldown & Tiered Edge Sizing

**Files:**
- Modify: `polybot/strategies/snipe.py:54-200`
- Modify: `tests/test_snipe.py`

- [ ] **Step 1: Write failing tests for tiered edge sizing**

Append to `tests/test_snipe.py`:

```python
from polybot.strategies.snipe import compute_tiered_kelly_scale


def test_tiered_kelly_base_edge():
    """Edge 2-3% gets no boost (1.0x)."""
    assert compute_tiered_kelly_scale(0.025) == 1.0


def test_tiered_kelly_mid_edge():
    """Edge 3-5% gets 1.5x boost."""
    assert compute_tiered_kelly_scale(0.04) == 1.5


def test_tiered_kelly_high_edge():
    """Edge 5%+ gets 2.0x boost."""
    assert compute_tiered_kelly_scale(0.06) == 2.0


def test_tiered_kelly_boundary_3pct():
    """Exactly 3% gets the 1.5x boost."""
    assert compute_tiered_kelly_scale(0.03) == 1.5


def test_tiered_kelly_boundary_5pct():
    """Exactly 5% gets the 2.0x boost."""
    assert compute_tiered_kelly_scale(0.05) == 2.0


def test_tiered_kelly_below_min():
    """Edge below 2% still gets 1.0x (no penalty)."""
    assert compute_tiered_kelly_scale(0.01) == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_snipe.py::test_tiered_kelly_base_edge -v`
Expected: FAIL with `ImportError: cannot import name 'compute_tiered_kelly_scale'`

- [ ] **Step 3: Implement tiered kelly scale function**

In `polybot/strategies/snipe.py`, add after the `compute_snipe_edge` function (after line 51):

```python
def compute_tiered_kelly_scale(net_edge: float) -> float:
    """Scale Kelly multiplier based on edge magnitude. Higher edge = larger position."""
    if net_edge >= 0.05:
        return 2.0
    if net_edge >= 0.03:
        return 1.5
    return 1.0
```

- [ ] **Step 4: Run tiered sizing tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_snipe.py -k tiered -v`
Expected: 6 passed

- [ ] **Step 5: Write failing tests for cooldown logic**

Append to `tests/test_snipe.py`:

```python
from datetime import datetime, timezone, timedelta
from polybot.strategies.snipe import check_snipe_cooldown


def test_cooldown_blocks_recent_exit():
    """Market exited 1 hour ago with 4-hour cooldown should be blocked."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=1), "exit_price": 0.95},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.95, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is False


def test_cooldown_allows_after_expiry():
    """Market exited 5 hours ago with 4-hour cooldown should be allowed."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=5), "exit_price": 0.95},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.95, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is True


def test_cooldown_allows_unknown_market():
    """Market not in cooldowns should be allowed."""
    result = check_snipe_cooldown(
        "mkt-new", current_price=0.95, cooldowns={},
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is True


def test_cooldown_allows_reentry_on_price_move():
    """Market in cooldown but price moved 4% should be allowed (re-entry)."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=1), "exit_price": 0.92},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.96, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is True


def test_cooldown_blocks_small_price_move():
    """Market in cooldown with only 1% price move should still be blocked."""
    now = datetime.now(timezone.utc)
    cooldowns = {
        "mkt-1": {"exit_time": now - timedelta(hours=1), "exit_price": 0.95},
    }
    result = check_snipe_cooldown(
        "mkt-1", current_price=0.96, cooldowns=cooldowns,
        cooldown_hours=4.0, reentry_threshold=0.03)
    assert result is False
```

- [ ] **Step 6: Run cooldown tests to verify they fail**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_snipe.py::test_cooldown_blocks_recent_exit -v`
Expected: FAIL with `ImportError: cannot import name 'check_snipe_cooldown'`

- [ ] **Step 7: Implement cooldown check function**

In `polybot/strategies/snipe.py`, add after `compute_tiered_kelly_scale`:

```python
def check_snipe_cooldown(
    polymarket_id: str,
    current_price: float,
    cooldowns: dict[str, dict],
    cooldown_hours: float,
    reentry_threshold: float,
) -> bool:
    """Return True if the market is clear to enter, False if blocked by cooldown."""
    if polymarket_id not in cooldowns:
        return True
    entry = cooldowns[polymarket_id]
    elapsed_hours = (datetime.now(timezone.utc) - entry["exit_time"]).total_seconds() / 3600
    if elapsed_hours >= cooldown_hours:
        return True
    # Still in cooldown — check if price moved enough for re-entry
    price_delta = abs(current_price - entry["exit_price"])
    return price_delta >= reentry_threshold
```

- [ ] **Step 8: Run all cooldown tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_snipe.py -k cooldown -v`
Expected: 5 passed

- [ ] **Step 9: Wire cooldown + tiered sizing into ResolutionSnipeStrategy**

In `polybot/strategies/snipe.py`, update the `__init__` method of `ResolutionSnipeStrategy` (after line 65):

```python
    def __init__(self, settings, ensemble=None):
        self.interval_seconds = settings.snipe_interval_seconds
        self.kelly_multiplier = settings.snipe_kelly_mult
        self.max_single_pct = settings.snipe_max_single_pct
        self._min_net_edge = settings.snipe_min_net_edge
        self._min_confidence = settings.snipe_min_confidence
        self._max_hours = settings.snipe_hours_max
        self._fee_rate = settings.polymarket_fee_rate
        self._ensemble = ensemble
        self._cooldown_hours = settings.snipe_cooldown_hours
        self._reentry_threshold = settings.snipe_reentry_threshold
        self._max_entries_per_market = settings.snipe_max_entries_per_market
        self._market_cooldowns: dict[str, dict] = {}
```

At the top of `run_once()`, after the `enabled` check (after the `if enabled is False: return` block), add the cooldown refresh:

```python
        # Refresh per-market cooldowns from recently closed snipe trades
        recent_exits = await ctx.db.fetch(
            """SELECT m.polymarket_id, t.closed_at, t.exit_price
               FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.strategy = 'snipe'
                 AND t.status IN ('dry_run_resolved', 'closed')
                 AND t.closed_at > NOW() - INTERVAL '24 hours'
               ORDER BY t.closed_at DESC""")
        for row in recent_exits:
            pid = row["polymarket_id"]
            if pid not in self._market_cooldowns or row["closed_at"] > self._market_cooldowns[pid]["exit_time"]:
                self._market_cooldowns[pid] = {
                    "exit_time": row["closed_at"],
                    "exit_price": float(row["exit_price"]),
                }
```

Inside the market loop, after computing `net_edge` and before the `if tier in (1, 2)` LLM check, add the cooldown gate and 24h entry cap:

```python
            # Per-market cooldown check
            if not check_snipe_cooldown(
                m["polymarket_id"], buy_price, self._market_cooldowns,
                self._cooldown_hours, self._reentry_threshold,
            ):
                log.debug("snipe_cooldown_blocked", market=m["polymarket_id"])
                continue

            # 24h entry cap per market
            entries_24h = await ctx.db.fetchval(
                """SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE m.polymarket_id = $1 AND t.strategy = 'snipe'
                     AND t.opened_at > NOW() - INTERVAL '24 hours'""",
                m["polymarket_id"])
            if entries_24h and entries_24h >= self._max_entries_per_market:
                log.debug("snipe_entry_cap", market=m["polymarket_id"],
                          entries=entries_24h, max=self._max_entries_per_market)
                continue
```

After the existing `tier_kelly_scale` multiplication (line 138: `kelly_adj *= tier_kelly_scale.get(tier, 1.0)`), add tiered edge sizing:

```python
                kelly_adj *= tier_kelly_scale.get(tier, 1.0)
                # Tiered edge sizing: larger positions on higher edge
                kelly_adj *= compute_tiered_kelly_scale(net_edge)
```

- [ ] **Step 10: Run all snipe tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_snipe.py -v`
Expected: All tests pass (existing 22 + 11 new = 33 total)

- [ ] **Step 11: Run full test suite**

Run: `cd /Users/logannye/polybot && uv run pytest -x -q`
Expected: All pass (no regressions)

- [ ] **Step 12: Commit**

```bash
cd /Users/logannye/polybot && git add polybot/strategies/snipe.py tests/test_snipe.py && git commit -m "feat: snipe per-market cooldown, tiered edge sizing, and re-entry logic"
```

---

### Task 3: Forecast Time-Stop

**Files:**
- Modify: `polybot/trading/position_manager.py:49-131`
- Modify: `tests/test_position_manager.py`

- [ ] **Step 1: Write failing test for time-stop**

Append to `tests/test_position_manager.py`:

```python
from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_check_positions_time_stop_forecast():
    """Forecast trade held > 120 minutes should trigger time_stop exit."""
    db = AsyncMock()
    opened_3h_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    db.fetch = AsyncMock(return_value=[{
        "id": 10, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-time", "question": "Time stop test?",
        "ensemble_probability": 0.65, "opened_at": opened_3h_ago,
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=0.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-time": {"yes_price": 0.52, "no_price": 0.48},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 120.0

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=10, exit_price=0.52, exit_reason="time_stop")


@pytest.mark.asyncio
async def test_check_positions_no_time_stop_within_window():
    """Forecast trade held < 120 minutes should NOT trigger time_stop."""
    db = AsyncMock()
    opened_30m_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.fetch = AsyncMock(return_value=[{
        "id": 11, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-fresh", "question": "Fresh forecast?",
        "ensemble_probability": 0.65, "opened_at": opened_30m_ago,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-fresh": {"yes_price": 0.52, "no_price": 0.48},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 120.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_check_positions_no_time_stop_for_snipe():
    """Snipe trade held > 120 minutes should NOT trigger time_stop (forecast only)."""
    db = AsyncMock()
    opened_3h_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    db.fetch = AsyncMock(return_value=[{
        "id": 12, "side": "YES", "entry_price": 0.95, "shares": 100.0,
        "position_size_usd": 95.0, "strategy": "snipe", "status": "dry_run",
        "polymarket_id": "mkt-snipe-old", "question": "Old snipe?",
        "ensemble_probability": None, "opened_at": opened_3h_ago,
    }])

    executor = AsyncMock()
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-snipe-old": {"yes_price": 0.96, "no_price": 0.04},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.30
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 120.0

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=AsyncMock(), settings=settings)
    await mgr.check_positions()

    # Snipe at 0.95 → 0.96 is only 1% gain, below 30% TP. No exit should fire.
    executor.exit_position.assert_not_called()
```

- [ ] **Step 2: Run time-stop tests to verify they fail**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_position_manager.py::test_check_positions_time_stop_forecast -v`
Expected: FAIL (likely KeyError on `opened_at` or no time-stop logic exists)

- [ ] **Step 3: Implement time-stop in position manager**

In `polybot/trading/position_manager.py`, update `__init__` to store the time-stop setting:

```python
    def __init__(self, db, executor, scanner, email_notifier, settings,
                 portfolio_lock=None):
        self._db = db
        self._executor = executor
        self._scanner = scanner
        self._email = email_notifier
        self._take_profit = settings.take_profit_threshold
        self._stop_loss = settings.stop_loss_threshold
        self._early_exit_edge = settings.early_exit_edge
        self._forecast_time_stop_minutes = getattr(settings, 'forecast_time_stop_minutes', 120.0)
        self._portfolio_lock = portfolio_lock
```

Update the SQL query in `check_positions()` to include `t.opened_at`:

```sql
        positions = await self._db.fetch(
            """SELECT t.id, t.side, t.entry_price, t.shares,
                      t.position_size_usd, t.strategy, t.status,
                      t.opened_at,
                      m.polymarket_id, m.question,
                      a.ensemble_probability
               FROM trades t
               JOIN markets m ON t.market_id = m.id
               LEFT JOIN analyses a ON t.analysis_id = a.id
               WHERE t.status IN ('filled', 'dry_run')
                 AND t.strategy != 'arbitrage'""")
```

Add the `datetime` import at the top of the file:

```python
from datetime import datetime, timezone
```

Inside the position loop, after the `current_yes_price` assignment and before the existing `if should_take_profit(...)` block, add the time-stop check:

```python
            # Time-stop: auto-exit forecast trades exceeding hold limit
            if pos["strategy"] == "forecast" and pos["opened_at"] is not None:
                hold_minutes = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 60
                if hold_minutes > self._forecast_time_stop_minutes:
                    exit_price = current_yes_price if side == "YES" else (1.0 - current_yes_price)
                    unrealized = compute_unrealized_return(side, entry_price, current_yes_price)
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
                        log.info("position_time_stop",
                                 trade_id=trade_id, hold_minutes=round(hold_minutes, 1),
                                 pnl=round(pnl, 4), market=pos["question"][:60])
                        await self._email.send(
                            f"[POLYBOT] Position time-stopped",
                            f"<p><b>Market:</b> {pos['question']}</p>"
                            f"<p><b>Held:</b> {hold_minutes:.0f}min | "
                            f"P&L: ${pnl:+.2f}</p>")
                    continue
```

- [ ] **Step 4: Run time-stop tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_position_manager.py -k time_stop -v`
Expected: 3 passed

- [ ] **Step 5: Run all position manager tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_position_manager.py -v`
Expected: All pass (existing 6 + 3 new = 9 total)

- [ ] **Step 6: Commit**

```bash
cd /Users/logannye/polybot && git add polybot/trading/position_manager.py tests/test_position_manager.py && git commit -m "feat: add 2-hour time-stop for forecast trades"
```

---

### Task 4: Forecast Market Loss Blacklist

**Files:**
- Modify: `polybot/strategies/forecast.py:29-95`
- Modify: `tests/test_forecast_strategy.py`

- [ ] **Step 1: Write failing test for blacklist**

Append to `tests/test_forecast_strategy.py`:

```python
from polybot.strategies.forecast import check_forecast_blacklist
from datetime import datetime, timezone, timedelta


def test_blacklist_blocks_after_two_losses():
    """Market with 2 stop-losses in 12h should be blacklisted."""
    now = datetime.now(timezone.utc)
    blacklist = {
        "mkt-bad": [now - timedelta(hours=2), now - timedelta(hours=1)],
    }
    assert check_forecast_blacklist("mkt-bad", blacklist) is True


def test_blacklist_allows_one_loss():
    """Market with only 1 stop-loss should not be blacklisted."""
    now = datetime.now(timezone.utc)
    blacklist = {
        "mkt-ok": [now - timedelta(hours=1)],
    }
    assert check_forecast_blacklist("mkt-ok", blacklist) is False


def test_blacklist_allows_unknown_market():
    """Market not in blacklist should be allowed."""
    assert check_forecast_blacklist("mkt-new", {}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_forecast_strategy.py::test_blacklist_blocks_after_two_losses -v`
Expected: FAIL with `ImportError: cannot import name 'check_forecast_blacklist'`

- [ ] **Step 3: Implement blacklist check function**

In `polybot/strategies/forecast.py`, add after the imports (before the `_STRATEGY_DISABLED_REASON` line):

```python
def check_forecast_blacklist(
    polymarket_id: str,
    blacklist: dict[str, list],
) -> bool:
    """Return True if market is blacklisted (2+ stop-losses in recent history)."""
    losses = blacklist.get(polymarket_id, [])
    return len(losses) >= 2
```

- [ ] **Step 4: Run blacklist tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_forecast_strategy.py -k blacklist -v`
Expected: 3 passed

- [ ] **Step 5: Wire blacklist into EnsembleForecastStrategy.run_once()**

In `polybot/strategies/forecast.py`, add `_loss_blacklist` to `__init__`:

```python
    def __init__(self, settings, ensemble, researcher):
        self.interval_seconds: float = settings.forecast_interval_seconds
        self.kelly_multiplier: float = settings.forecast_kelly_mult
        self.max_single_pct: float = settings.forecast_max_single_pct
        self._settings = settings
        self._ensemble = ensemble
        self._researcher = researcher
        self._loss_blacklist: dict[str, list] = {}
```

At the top of `run_once()`, after the enabled check and the `state_row` read (after line 59), add the blacklist refresh:

```python
        # Refresh forecast loss blacklist
        recent_losses = await ctx.db.fetch(
            """SELECT m.polymarket_id, t.closed_at
               FROM trades t JOIN markets m ON t.market_id = m.id
               WHERE t.strategy = 'forecast'
                 AND t.exit_reason IN ('stop_loss', 'time_stop')
                 AND t.closed_at > NOW() - INTERVAL '12 hours'""")
        self._loss_blacklist = {}
        for row in recent_losses:
            pid = row["polymarket_id"]
            self._loss_blacklist.setdefault(pid, []).append(row["closed_at"])
```

In the `_full_analyze_and_trade` method, add the blacklist check at the very top (after the method signature, before web research):

```python
        # Market loss blacklist: skip markets with 2+ recent stop-losses
        if check_forecast_blacklist(candidate.polymarket_id, self._loss_blacklist):
            log.info("forecast_blacklisted", market=candidate.polymarket_id,
                     losses=len(self._loss_blacklist.get(candidate.polymarket_id, [])))
            return
```

- [ ] **Step 6: Run all forecast tests**

Run: `cd /Users/logannye/polybot && uv run pytest tests/test_forecast_strategy.py -v`
Expected: All pass (existing 2 + 3 new = 5 total)

- [ ] **Step 7: Commit**

```bash
cd /Users/logannye/polybot && git add polybot/strategies/forecast.py tests/test_forecast_strategy.py && git commit -m "feat: add forecast market loss blacklist"
```

---

### Task 5: Arbitrage Bankroll Gate

**Files:**
- Modify: `polybot/strategies/arbitrage.py:128-155`

- [ ] **Step 1: Add bankroll gate to ArbitrageStrategy**

In `polybot/strategies/arbitrage.py`, update `__init__` to store the min bankroll:

```python
    def __init__(self, settings):
        self.interval_seconds = float(getattr(settings, "arb_interval_seconds", 60.0))
        self.kelly_multiplier = float(getattr(settings, "arb_kelly_multiplier", 0.20))
        self.max_single_pct = float(getattr(settings, "arb_max_single_pct", 0.40))
        self._seen_arbs: set[str] = set()
        self._dedup_loaded: bool = False
        self._settings = settings
        self._min_bankroll = float(getattr(settings, "arb_min_bankroll", 2000.0))
```

At the top of `run_once()` (before the dedup cache warm, line 142), add:

```python
        # Bankroll gate: don't lock capital in arb at small bankrolls
        state = await ctx.db.fetchrow("SELECT bankroll FROM system_state WHERE id = 1")
        if state and float(state["bankroll"]) < self._min_bankroll:
            log.debug("arb_bankroll_gate", bankroll=float(state["bankroll"]),
                      min_required=self._min_bankroll)
            return
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `cd /Users/logannye/polybot && uv run pytest -x -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
cd /Users/logannye/polybot && git add polybot/strategies/arbitrage.py && git commit -m "feat: gate arbitrage behind $2K minimum bankroll"
```

---

### Task 6: Config Value Updates & Deploy

**Files:**
- Modify: `.env`

- [ ] **Step 1: Update .env config values**

Add/update these values in `/Users/logannye/polybot/.env`:

```
# Snipe Factory v1 — aggressive 48-hour config
SNIPE_HOURS_MAX=120
FORECAST_INTERVAL_SECONDS=180
TAKE_PROFIT_THRESHOLD=0.30
MAX_TOTAL_DEPLOYED_PCT=0.90
MAX_CONCURRENT_POSITIONS=20
SNIPE_MAX_SINGLE_PCT=0.30
DAILY_LOSS_LIMIT_PCT=1.0
```

- [ ] **Step 2: Run full test suite one final time**

Run: `cd /Users/logannye/polybot && uv run pytest -x -q`
Expected: All pass (231 existing + ~17 new = ~248 total)

- [ ] **Step 3: Commit config changes**

```bash
cd /Users/logannye/polybot && git add .env && git commit -m "config: snipe factory 48-hour aggressive settings"
```

- [ ] **Step 4: Stop the running bot**

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist
```

Verify stopped: `ps aux | grep polybot | grep -v grep` should return nothing.

- [ ] **Step 5: Restart the bot**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist
```

- [ ] **Step 6: Verify bot is running with new config**

```bash
sleep 10 && tail -30 ~/polybot/data/polybot_stdout.log
```

Verify:
- `polybot_starting` log line appears
- Snipe cycles show `snipe_cooldown_blocked` for recently-traded markets
- Arb cycle shows `arb_bankroll_gate` (bankroll is ~$503, below $2K threshold)
- No errors in stderr: `tail -10 ~/polybot/data/polybot_stderr.log`

- [ ] **Step 7: Commit the launcher script if not already tracked**

```bash
cd /Users/logannye/polybot && git add scripts/run_polybot.sh && git commit -m "ops: add LaunchAgent launcher script" 2>/dev/null || true
```
