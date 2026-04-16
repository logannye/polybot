# Post-Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 issues found during the April 16 audit: remove broken MM simulation, re-enable profitable strategies for realistic dry-run, resolve an orphaned live trade, and fix 4 code quality issues in safeguards and CLOB encapsulation.

**Architecture:** Four independent groups (A-D) that can ship in any order. Group A removes dead simulation code. Group B flips two config defaults. Group C is a one-time script. Group D touches engine.py (divergence recovery + drawdown cache), clob.py (new method), executor.py (use new method), and market_maker.py (inventory reconciliation).

**Tech Stack:** Python 3.13, PostgreSQL, pytest, py-clob-client, structlog

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/strategies/market_maker.py` | Modify | Remove `_simulate_fills`, add dry-run log summary, add inventory reconciliation (Tasks 1, 7) |
| `tests/test_market_maker.py` | Modify | Remove `TestSimulateFills` class, add dry-run summary test, add inventory reconciliation test (Tasks 1, 7) |
| `polybot/core/config.py` | Modify | `mr_enabled=True`, `forecast_enabled=True` (Task 2) |
| `tests/test_config.py` | Modify | Update assertion for `mr_enabled` (Task 2) |
| `scripts/resolve_orphan_trade.py` | Create | One-time script to check/cancel trade #942 (Task 3) |
| `polybot/core/engine.py` | Modify | Divergence self-healing, drawdown cache (Tasks 4, 5) |
| `tests/test_safeguards.py` | Modify | Add divergence recovery + drawdown cache tests (Tasks 4, 5) |
| `polybot/trading/clob.py` | Modify | Add `get_order_book_summary()` (Task 6) |
| `polybot/trading/executor.py` | Modify | Use `get_order_book_summary()` instead of `_client` (Task 6) |
| `tests/test_clob.py` | Modify | Add `get_order_book_summary` test (Task 6) |
| `tests/test_realistic_dryrun.py` | Modify | Update mock to match new code path (Task 6) |

---

### Task 1: Remove MM Dry-Run Simulation (Group A)

**Files:**
- Modify: `polybot/strategies/market_maker.py:47-53` (init), `55-76` (run_once), `247-287` (_simulate_fills)
- Modify: `tests/test_market_maker.py:140-248` (TestSimulateFills class)

- [ ] **Step 1: Delete TestSimulateFills and add replacement test**

In `tests/test_market_maker.py`, replace the entire `class TestSimulateFills:` block (lines 140-248) with:

```python
class TestDryRunNoSimulation:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_simulate_fills(self):
        """Dry-run run_once should NOT call _simulate_fills (method removed)."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[])
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       dry_run=True)
        assert not hasattr(strategy, '_simulate_fills')
        assert not hasattr(strategy, '_sim_pnl')
        assert not hasattr(strategy, '_sim_fills')
        assert not hasattr(strategy, '_prev_prices')

    @pytest.mark.asyncio
    async def test_dry_run_logs_quote_summary(self):
        """Dry-run run_once should log active market count."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", price=0.50, volume=20000, depth=8000),
        ])
        scanner.get_cached_price = MagicMock(return_value={
            "yes_price": 0.50, "no_price": 0.50,
        })
        qm = AsyncMock()
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       dry_run=True, quote_manager=qm)
        ctx = MagicMock()
        ctx.db = AsyncMock()
        await strategy.run_once(ctx)
        # Should not crash; quoting still runs in dry-run
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_market_maker.py -v`
Expected: `test_dry_run_does_not_simulate_fills` FAILS because `_simulate_fills` still exists.

- [ ] **Step 3: Remove simulation from market_maker.py**

In `polybot/strategies/market_maker.py`:

Remove these three lines from `__init__` (lines 51-53):
```python
        self._sim_pnl: float = 0.0
        self._sim_fills: int = 0
        self._prev_prices: dict[str, float] = {}  # price when quotes were last placed
```

In `run_once()`, remove lines 74-76:
```python
        # 3. Simulate fills BEFORE requoting — compare new price vs old quotes
        if self._dry_run:
            self._simulate_fills()
```

And replace with a dry-run summary log after the quoting loop (after the `for market in list(...)` block, before the method ends):
```python
        # Dry-run observability: log quoting activity without simulating fills
        if self._dry_run and self._active_markets:
            log.info("mm_dry_run_cycle", active_markets=len(self._active_markets))
```

Delete the entire `_simulate_fills` method (lines 247-287).

Also remove this line from `_manage_quotes` (line 245) since `_prev_prices` no longer exists:
```python
        # Record the price used for this quote cycle (used by fill simulation)
        self._prev_prices[market.polymarket_id] = current_price
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_market_maker.py -v`
Expected: All pass.

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest tests/ --tb=short -q`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/strategies/market_maker.py tests/test_market_maker.py
git commit -m "fix: remove broken MM dry-run simulation (-$5,582 fake P&L)

_simulate_fills() counted every 5s quote cycle as a fill with negative
spread math, producing 156K fake fills. Market making can't be simulated
in dry-run — maker fill probability depends on queue position and order
flow. Replaced with a simple log summary of quoting activity.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Re-enable MR and Forecast (Group B)

**Files:**
- Modify: `polybot/core/config.py:202` (mr_enabled), `134` (forecast_enabled)
- Modify: `tests/test_config.py:112` (mr_enabled assertion)

- [ ] **Step 1: Update config defaults**

In `polybot/core/config.py`, change line 202:
```python
    mr_enabled: bool = True
```
(was `False`)

Change line 134:
```python
    forecast_enabled: bool = True
```
(was `False`)

- [ ] **Step 2: Update test assertion**

In `tests/test_config.py`, change line 112:
```python
    assert s.mr_enabled is True   # re-enabled for realistic dry-run validation
```
(was `assert s.mr_enabled is False`)

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_config.py -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py tests/test_config.py
git commit -m "config: re-enable MR and Forecast for realistic dry-run validation

Both strategies showed positive edge in pre-realistic dry-run (MR +$107,
Forecast +$24). They now flow through the realistic executor path with
real order book checks, spread filtering, and taker fee simulation.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Resolve Orphaned Live Trade (Group C)

**Files:**
- Create: `scripts/resolve_orphan_trade.py`

- [ ] **Step 1: Create the script**

Create `scripts/resolve_orphan_trade.py`:

```python
#!/usr/bin/env python3
"""Resolve orphaned live trades by checking CLOB order status.

Checks a specific trade's CLOB order, then either marks it as filled
(if matched) or cancels and frees deployed capital (if live/cancelled).

Usage:
    cd ~/polybot && uv run python scripts/resolve_orphan_trade.py
"""
import os
import sys
import asyncio
import asyncpg
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

TRADE_ID = 942
CLOB_ORDER_ID = "0x40233f8a95c106e8503171e0a358ea0aef6ef720420197dfa445c0c8a08908f7"


async def main():
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        print("FAIL: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    client = ClobClient(
        host="https://clob.polymarket.com", chain_id=137, key=pk,
        creds=ApiCreds(
            api_key=os.environ.get("POLYMARKET_API_KEY", ""),
            api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
            api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", "")))

    # 1. Check CLOB order status
    print(f"Checking CLOB order status for trade #{TRADE_ID}...")
    try:
        result = client.get_order(CLOB_ORDER_ID)
        status_raw = result.get("status", "UNKNOWN").upper()
        size_matched = float(result.get("size_matched", 0))
        print(f"  CLOB status: {status_raw}, size_matched: {size_matched}")
    except Exception as e:
        print(f"  CLOB API error: {e}")
        print("  Treating as CANCELLED (order may have expired)")
        status_raw = "CANCELLED"

    # 2. Connect to DB
    db_url = os.environ.get("DATABASE_URL")
    conn = await asyncpg.connect(db_url)

    trade = await conn.fetchrow("SELECT * FROM trades WHERE id = $1", TRADE_ID)
    if not trade:
        print(f"Trade #{TRADE_ID} not found in DB")
        await conn.close()
        sys.exit(1)

    position_size = float(trade["position_size_usd"])
    print(f"  DB status: {trade['status']}, size: ${position_size:.2f}")

    if trade["status"] not in ("open",):
        print(f"  Trade is already {trade['status']} — nothing to do")
        await conn.close()
        return

    # 3. Resolve based on CLOB status
    if status_raw == "MATCHED":
        await conn.execute(
            "UPDATE trades SET status = 'filled' WHERE id = $1", TRADE_ID)
        print(f"  -> Marked as FILLED. Position manager will manage TP/SL.")
    else:
        # LIVE, CANCELLED, or unknown — cancel and free capital
        if status_raw == "LIVE":
            print(f"  Order still live after 3 days — cancelling...")
            try:
                client.cancel(CLOB_ORDER_ID)
                print(f"  -> CLOB order cancelled")
            except Exception as e:
                print(f"  -> Cancel failed (may already be dead): {e}")

        await conn.execute(
            "UPDATE trades SET status = 'cancelled' WHERE id = $1", TRADE_ID)
        await conn.execute(
            "UPDATE system_state SET total_deployed = GREATEST(0, total_deployed - $1) WHERE id = 1",
            position_size)
        print(f"  -> Marked as CANCELLED. Freed ${position_size:.2f} deployed capital.")

    # 4. Verify
    updated = await conn.fetchrow("SELECT status FROM trades WHERE id = $1", TRADE_ID)
    state = await conn.fetchrow("SELECT total_deployed FROM system_state WHERE id = 1")
    print(f"\n  Verification: trade status={updated['status']}, "
          f"total_deployed=${float(state['total_deployed']):.2f}")

    await conn.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the script**

Run: `cd ~/polybot && uv run python scripts/resolve_orphan_trade.py`
Expected: Prints the CLOB order status and the action taken. Verify the output makes sense before proceeding.

- [ ] **Step 3: Verify DB state**

Run: `cd ~/polybot && /opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "SELECT id, status, position_size_usd FROM trades WHERE id = 942"`
Expected: Status is either `filled` or `cancelled`.

Run: `cd ~/polybot && /opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "SELECT total_deployed FROM system_state WHERE id = 1"`
Expected: `total_deployed` is 0 (if cancelled) or still 15.36 (if filled and position manager will handle).

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add scripts/resolve_orphan_trade.py
git commit -m "feat: orphan trade resolution script — checks CLOB and resolves trade #942

Queries CLOB order status for the Flyers NHL trade that's been in
'open' status since Apr 13. Cancels if unfilled, marks filled if
matched. Frees locked deployed capital.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Capital Divergence Self-Healing (Group D1)

**Files:**
- Modify: `polybot/core/engine.py:36` (init), `213-237` (_check_capital_divergence)
- Modify: `tests/test_safeguards.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_safeguards.py`:

```python
@pytest.mark.asyncio
async def test_capital_divergence_self_heals_after_3_ok_checks():
    """Should clear halt after 3 consecutive checks within threshold."""
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 500, "total_deployed": 50,
    })

    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=445.0)  # 445 vs 450 expected = 1%

    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._clob = clob
    engine._settings = settings
    engine._capital_divergence_halted = True  # previously halted
    engine._capital_divergence_ok_count = 0
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()

    # First two OK checks: still halted
    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True
    assert engine._capital_divergence_ok_count == 1

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True
    assert engine._capital_divergence_ok_count == 2

    # Third OK check: healed
    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is False
    assert engine._capital_divergence_ok_count == 0


@pytest.mark.asyncio
async def test_capital_divergence_resets_ok_count_on_new_divergence():
    """A new divergence during recovery should reset the OK counter."""
    from polybot.core.engine import Engine

    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10

    engine = Engine.__new__(Engine)
    engine._settings = settings
    engine._capital_divergence_halted = True
    engine._capital_divergence_ok_count = 2  # almost healed
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()

    # Divergent check: CLOB balance way off
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={"bankroll": 500, "total_deployed": 0})
    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=100.0)  # 80% divergence

    engine._db = db
    engine._clob = clob

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True
    assert engine._capital_divergence_ok_count == 0  # reset
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_safeguards.py::test_capital_divergence_self_heals_after_3_ok_checks -v`
Expected: FAIL — `_capital_divergence_ok_count` attribute doesn't exist.

- [ ] **Step 3: Implement self-healing**

In `polybot/core/engine.py`, add to `__init__` after line 36 (`self._capital_divergence_halted = False`):

```python
        self._capital_divergence_ok_count = 0
```

Replace the entire `_check_capital_divergence` method (lines 213-237) with:

```python
    async def _check_capital_divergence(self):
        """Compare CLOB balance vs DB bankroll. Halt if divergence > threshold.
        Self-heals after 3 consecutive OK checks."""
        if not self._clob or self._settings.dry_run:
            return
        try:
            state = await self._db.fetchrow("SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
            clob_balance = await self._clob.get_balance()
            expected_cash = float(state["bankroll"]) - float(state["total_deployed"])
            if expected_cash <= 0:
                return
            divergence = abs(clob_balance - expected_cash) / expected_cash
            max_div = getattr(self._settings, 'max_capital_divergence_pct', 0.10)
            if divergence > max_div:
                self._capital_divergence_halted = True
                self._capital_divergence_ok_count = 0
                log.critical("CAPITAL_DIVERGENCE_HALT", clob=clob_balance,
                             expected=expected_cash, divergence_pct=round(divergence * 100, 1))
                try:
                    await self._context.email_notifier.send(
                        "[POLYBOT CRITICAL] Capital divergence halt",
                        f"<p>CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}, "
                        f"Divergence: {divergence*100:.1f}%</p>")
                except Exception:
                    pass
            elif self._capital_divergence_halted:
                self._capital_divergence_ok_count += 1
                if self._capital_divergence_ok_count >= 3:
                    self._capital_divergence_halted = False
                    self._capital_divergence_ok_count = 0
                    log.info("CAPITAL_DIVERGENCE_RECOVERED",
                             clob=clob_balance, expected=expected_cash)
                    try:
                        await self._context.email_notifier.send(
                            "[POLYBOT INFO] Capital divergence recovered",
                            f"<p>CLOB balance back in sync after 3 consecutive OK checks. "
                            f"CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}</p>")
                    except Exception:
                        pass
        except Exception as e:
            log.error("capital_divergence_check_error", error=str(e))
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_safeguards.py -v`
Expected: All 7 tests pass (5 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/core/engine.py tests/test_safeguards.py
git commit -m "fix: capital divergence monitor self-heals after 3 consecutive OK checks

Previously, a transient CLOB API glitch permanently halted all trading
until restart. Now the monitor clears the halt after 3 consecutive 60s
checks show divergence is within threshold (3 minutes of healthy state).
OK counter resets on any new divergence to prevent flapping.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Cache Drawdown Check (Group D2)

**Files:**
- Modify: `polybot/core/engine.py:36` (init), `169-211` (_check_drawdown_halt)
- Modify: `tests/test_safeguards.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_safeguards.py`:

```python
@pytest.mark.asyncio
async def test_drawdown_check_uses_cache_within_30s():
    """Should return cached result without querying DB within 30s."""
    import time
    from polybot.core.engine import Engine

    db = AsyncMock()
    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._drawdown_cache = (False, time.monotonic())  # cached 'not halted' just now

    result = await engine._check_drawdown_halt()
    assert result is False
    db.fetchrow.assert_not_called()  # should NOT have queried DB


@pytest.mark.asyncio
async def test_drawdown_check_queries_db_after_cache_expires():
    """Should query DB when cache is older than 30s."""
    import time
    from polybot.core.engine import Engine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 400, "high_water_bankroll": 500,
        "drawdown_halt_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = Engine.__new__(Engine)
    engine._db = db
    engine._settings = settings
    engine._drawdown_cache = (False, time.monotonic() - 31)  # expired

    result = await engine._check_drawdown_halt()
    assert result is False
    db.fetchrow.assert_called_once()  # SHOULD have queried DB
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_safeguards.py::test_drawdown_check_uses_cache_within_30s -v`
Expected: FAIL — `_drawdown_cache` attribute doesn't exist.

- [ ] **Step 3: Implement drawdown caching**

In `polybot/core/engine.py`, add `import time` at the top of the file (after the existing imports).

Add to `__init__` after the `_capital_divergence_ok_count` line:

```python
        self._drawdown_cache: tuple[bool, float] | None = None
```

Replace the `_check_drawdown_halt` method (lines 169-211) with:

```python
    async def _check_drawdown_halt(self) -> bool:
        """Check if total drawdown halt is active or should be triggered.
        Returns True if trading should be halted. Caches result for 30s."""
        if self._drawdown_cache is not None:
            cached_result, cached_at = self._drawdown_cache
            if time.monotonic() - cached_at < 30:
                return cached_result

        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            self._drawdown_cache = (False, time.monotonic())
            return False

        bankroll = float(state["bankroll"])
        high_water = float(state.get("high_water_bankroll", bankroll) or bankroll)
        halt_until = state.get("drawdown_halt_until")

        # Already halted?
        if halt_until and halt_until > datetime.now(timezone.utc):
            self._drawdown_cache = (True, time.monotonic())
            return True

        # Update high-water mark
        if bankroll > high_water:
            await self._db.execute(
                "UPDATE system_state SET high_water_bankroll = $1 WHERE id = 1", bankroll)
            self._drawdown_cache = (False, time.monotonic())
            return False

        # Check drawdown
        if high_water > 0:
            drawdown = 1.0 - (bankroll / high_water)
            max_drawdown = getattr(self._settings, 'max_total_drawdown_pct', 0.30)
            if drawdown >= max_drawdown:
                halt_time = datetime.now(timezone.utc) + timedelta(days=365)
                await self._db.execute(
                    "UPDATE system_state SET drawdown_halt_until = $1 WHERE id = 1",
                    halt_time)
                log.critical("DRAWDOWN_HALT", bankroll=bankroll, high_water=high_water,
                             drawdown_pct=round(drawdown * 100, 1))
                try:
                    await self._context.email_notifier.send(
                        "[POLYBOT CRITICAL] DRAWDOWN HALT — ALL TRADING STOPPED",
                        f"<p>Bankroll ${bankroll:.2f} is {drawdown*100:.1f}% below "
                        f"high-water ${high_water:.2f}. Threshold: {max_drawdown*100:.0f}%.</p>"
                        f"<p>All trading halted. Manual DB reset required to resume.</p>")
                except Exception:
                    pass
                self._drawdown_cache = (True, time.monotonic())
                return True

        self._drawdown_cache = (False, time.monotonic())
        return False
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_safeguards.py -v`
Expected: All 9 tests pass (7 from Task 4 + 2 new).

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/core/engine.py tests/test_safeguards.py
git commit -m "perf: cache drawdown halt check for 30s to reduce DB queries

_check_drawdown_halt() was querying system_state before every
strategy.run_once() — 7+ DB round-trips per cycle. Now caches the
result for 30s. Drawdown state changes only on trade events, so 30s
staleness is acceptable for a circuit breaker requiring manual reset.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: ClobGateway Order Book Encapsulation (Group D3)

**Files:**
- Modify: `polybot/trading/clob.py:86-95` (after get_book_spread)
- Modify: `polybot/trading/executor.py:43-71` (realistic dry-run block)
- Modify: `tests/test_clob.py`
- Modify: `tests/test_realistic_dryrun.py`

- [ ] **Step 1: Write failing test for get_order_book_summary**

Append to `tests/test_clob.py`:

```python
@pytest.mark.asyncio
async def test_get_order_book_summary():
    """get_order_book_summary returns best bid, ask, and spread."""
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_ask = MagicMock()
    mock_ask.price = "0.5500"
    mock_bid = MagicMock()
    mock_bid.price = "0.5300"
    mock_book = MagicMock()
    mock_book.asks = [mock_ask]
    mock_book.bids = [mock_bid]
    mock_client.get_order_book.return_value = mock_book
    gw._client = mock_client
    result = await gw.get_order_book_summary("token123")
    assert result is not None
    assert abs(result["best_bid"] - 0.53) < 0.001
    assert abs(result["best_ask"] - 0.55) < 0.001
    assert abs(result["spread"] - 0.02) < 0.001


@pytest.mark.asyncio
async def test_get_order_book_summary_empty_book():
    """get_order_book_summary returns None when book is empty."""
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_book = MagicMock()
    mock_book.asks = []
    mock_book.bids = []
    mock_client.get_order_book.return_value = mock_book
    gw._client = mock_client
    result = await gw.get_order_book_summary("token123")
    assert result is None


@pytest.mark.asyncio
async def test_get_order_book_summary_error():
    """get_order_book_summary returns None on API error."""
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_order_book.side_effect = Exception("timeout")
    gw._client = mock_client
    result = await gw.get_order_book_summary("token123")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_clob.py::test_get_order_book_summary -v`
Expected: FAIL — `get_order_book_summary` doesn't exist.

- [ ] **Step 3: Implement get_order_book_summary**

In `polybot/trading/clob.py`, add this method after `get_book_spread` (after line 95):

```python
    async def get_order_book_summary(self, token_id: str) -> dict | None:
        """Fetch order book and return best bid, best ask, and spread.
        Returns None if book is empty or on error."""
        try:
            book = await asyncio.to_thread(self._client.get_order_book, token_id)
            if book.asks and book.bids:
                best_ask = float(book.asks[0].price)
                best_bid = float(book.bids[0].price)
                return {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": best_ask - best_bid,
                }
            return None
        except Exception as e:
            log.debug("get_order_book_summary_failed", token_id=token_id[:20],
                      error=str(e)[:60])
            return None
```

- [ ] **Step 4: Run clob tests**

Run: `cd ~/polybot && uv run pytest tests/test_clob.py -v`
Expected: All pass.

- [ ] **Step 5: Update executor to use get_order_book_summary**

In `polybot/trading/executor.py`, replace the realistic dry-run block (lines 43-71) with:

```python
        # Realistic dry-run: check order book before filling
        effective_price = price
        if (self._dry_run
                and getattr(self, '_settings', None)
                and getattr(self._settings, 'dry_run_realistic', False)
                and self._clob is not None
                and token_id):
            try:
                summary = await self._clob.get_order_book_summary(token_id)
                if summary is not None:
                    max_spread = getattr(self._settings, 'dry_run_max_spread', 0.15)
                    if summary["spread"] > max_spread:
                        log.info("dryrun_spread_reject", token_id=token_id[:20],
                                 spread=round(summary["spread"], 4), max=max_spread,
                                 strategy=strategy)
                        return None
                    # Fill at best ask (what you'd actually pay), with simulated fee
                    if side in ("YES", "NO"):
                        effective_price = summary["best_ask"]
                        fee_pct = getattr(self._settings, 'dry_run_taker_fee_pct', 0.02)
                        size_usd = size_usd * (1 - fee_pct)
                        shares = self._wallet.compute_shares(size_usd, effective_price)
                else:
                    log.info("dryrun_no_book", token_id=token_id[:20], strategy=strategy)
                    return None
            except Exception as e:
                log.debug("dryrun_book_check_failed", error=str(e)[:60])
```

- [ ] **Step 6: Update realistic dry-run tests**

In `tests/test_realistic_dryrun.py`, the tests mock `clob.get_order_book_sync` which was the old internal call. Replace the mock setup in each test.

In `test_realistic_dryrun_rejects_wide_spread`, replace the `clob` mock setup:
```python
    clob = AsyncMock()
    # Order book with 50% spread
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.25, "best_ask": 0.75, "spread": 0.50,
    })
```

In `test_realistic_dryrun_fills_at_best_ask`, replace the `clob` mock setup:
```python
    clob = AsyncMock()
    # Tight spread: bid 0.48, ask 0.52
    clob.get_order_book_summary = AsyncMock(return_value={
        "best_bid": 0.48, "best_ask": 0.52, "spread": 0.04,
    })
```

In `test_non_realistic_dryrun_fills_at_model_price`, no change needed — it doesn't use the CLOB mock.

- [ ] **Step 7: Run all affected tests**

Run: `cd ~/polybot && uv run pytest tests/test_clob.py tests/test_realistic_dryrun.py tests/test_executor.py --tb=short -v`
Expected: All pass.

- [ ] **Step 8: Commit**

```bash
cd ~/polybot && git add polybot/trading/clob.py polybot/trading/executor.py tests/test_clob.py tests/test_realistic_dryrun.py
git commit -m "refactor: add get_order_book_summary to ClobGateway, fix encapsulation

The realistic dry-run path in executor.py was reaching into
_clob._client.get_order_book (private attr). New method on ClobGateway
returns {best_bid, best_ask, spread} or None. Executor now calls this
instead. 3 new tests for the gateway method.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Seed InventoryTracker from Trades Table (Group D4)

**Files:**
- Modify: `polybot/strategies/market_maker.py:27-53` (init), `55-83` (run_once)
- Modify: `tests/test_market_maker.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_market_maker.py`:

```python
class TestInventoryReconciliation:
    @pytest.mark.asyncio
    async def test_reconcile_inventory_seeds_from_db(self):
        """First run_once should seed inventory from filled MM trades in DB."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[])
        inv = InventoryTracker(max_per_market=50.0, max_total=200.0, max_skew_bps=100)
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner,
                                       inventory=inv)

        ctx = MagicMock()
        ctx.db = AsyncMock()
        # YES fills -> BUY (adds to yes_shares), NO fills -> SELL (subtracts from yes_shares)
        ctx.db.fetch = AsyncMock(return_value=[
            {"polymarket_id": "m1", "side": "YES", "total_shares": 25.0},
            {"polymarket_id": "m1", "side": "NO", "total_shares": 10.0},
        ])

        assert strategy._inventory_reconciled is False
        await strategy.run_once(ctx)
        assert strategy._inventory_reconciled is True

        m1_inv = inv.get_inventory("m1")
        assert m1_inv is not None
        # record_fill("BUY", 0.50, 25) -> yes_shares=25, then
        # record_fill("SELL", 0.50, 10) -> yes_shares=15 (net long 15 YES)
        assert m1_inv.yes_shares == 15.0

    @pytest.mark.asyncio
    async def test_reconcile_inventory_runs_once(self):
        """Inventory reconciliation should only run on first run_once."""
        settings = _make_settings()
        clob = AsyncMock()
        clob.send_heartbeat = AsyncMock(return_value="hb1")
        scanner = MagicMock()
        scanner.fetch_markets = AsyncMock(return_value=[])
        strategy = MarketMakerStrategy(settings=settings, clob=clob, scanner=scanner)

        ctx = MagicMock()
        ctx.db = AsyncMock()
        ctx.db.fetch = AsyncMock(return_value=[])

        await strategy.run_once(ctx)
        await strategy.run_once(ctx)
        # fetch should be called once for reconciliation, not twice
        assert ctx.db.fetch.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_market_maker.py::TestInventoryReconciliation -v`
Expected: FAIL — `_inventory_reconciled` attribute doesn't exist.

- [ ] **Step 3: Implement inventory reconciliation**

In `polybot/strategies/market_maker.py`, add to `__init__` after the `_vol_blacklist` line (line 50):

```python
        self._inventory_reconciled = False
```

Add this method after `_send_heartbeat` (after line 96):

```python
    async def _reconcile_inventory(self, ctx) -> None:
        """Seed inventory from filled MM trades in DB. Called once on first run_once()."""
        try:
            rows = await ctx.db.fetch(
                """SELECT mk.polymarket_id, t.side,
                          SUM(t.shares) as total_shares
                   FROM trades t
                   JOIN markets mk ON t.market_id = mk.id
                   WHERE t.strategy = 'market_maker' AND t.status = 'filled'
                   GROUP BY mk.polymarket_id, t.side""")
            for row in rows:
                side = "BUY" if row["side"] == "YES" else "SELL"
                self._inventory.record_fill(
                    row["polymarket_id"], side, 0.50, float(row["total_shares"]))
            if rows:
                log.info("mm_inventory_reconciled", rows=len(rows))
        except Exception as e:
            log.error("mm_inventory_reconcile_failed", error=str(e))
        self._inventory_reconciled = True
```

In `run_once()`, add after the heartbeat call (after `await self._send_heartbeat()`, before the `# Ensure QuoteManager has DB access` comment):

```python
        # Seed inventory from DB on first cycle
        if not self._inventory_reconciled:
            await self._reconcile_inventory(ctx)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_market_maker.py -v`
Expected: All pass.

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest tests/ --tb=short -q`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/strategies/market_maker.py tests/test_market_maker.py
git commit -m "feat: seed MM InventoryTracker from trades table on startup

After a restart during live MM trading, inventory state was lost and
quote skewing started from zero. Now the first run_once() queries filled
MM trades from the DB and seeds the InventoryTracker. Harmless in
dry-run (query returns no rows).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `cd ~/polybot && uv run pytest tests/ --tb=short -q`
Expected: All pass with no regressions.

- [ ] **Step 2: Verify no leftover references**

Run: `cd ~/polybot && grep -rn '_simulate_fills\|_sim_pnl\|_sim_fills\|_prev_prices' polybot/ tests/`
Expected: No matches (all simulation references removed).

Run: `cd ~/polybot && grep -rn '_clob._client' polybot/trading/executor.py`
Expected: No matches (encapsulation fixed).

- [ ] **Step 3: Restart the bot**

The config changes (MR + Forecast re-enabled) take effect on restart.

Run: `launchctl kickstart -k gui/$(id -u)/ai.polybot.trader`

Then verify strategies are running:
Run: `sleep 10 && tail -50 ~/polybot/data/polybot_stdout.log | grep -E 'engine_starting|mr_|forecast_|mm_dry_run'`
Expected: Should see MR and Forecast strategies in the startup log and beginning to scan markets.
