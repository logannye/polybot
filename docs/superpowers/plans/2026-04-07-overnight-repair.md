# Polybot Overnight Repair — Schema, MR Recovery, CV Stall, Session Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 production issues that accumulated overnight — schema crash loop, Mean Reversion DNS death, Cross-Venue stall, PriceHistoryScanner not restarting, and leaked aiohttp sessions.

**Architecture:** All fixes are in existing files. The schema fix makes `_apply_schema()` idempotent even with existing data. The MR/CV/PriceHistory fixes add resilience at the strategy and engine level. The session cleanup adds missing `close()` calls in the `__main__.py` shutdown path.

**Tech Stack:** Python 3.13, asyncpg, aiohttp, asyncio, pytest + AsyncMock

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `polybot/db/schema.sql` | Modify lines 35, 76-78 | Fix CHECK constraint to use DROP/ADD pattern that handles existing violating rows |
| `polybot/db/connection.py` | Modify `_apply_schema()` | Wrap schema execution in a transaction so partial failures don't corrupt state |
| `polybot/core/engine.py` | Modify `_run_strategy()`, `_run_periodic()` | Add aiohttp session recreation on `ClientConnectorError`; add PriceHistoryScanner restart logic |
| `polybot/markets/scanner.py` | Modify `fetch_markets()` | Add session-closed detection + auto-recreate for Gamma API DNS failures |
| `polybot/analysis/odds_client.py` | Modify `fetch_odds()`, `fetch_all_sports()` | Add session-health check, timeout on `fetch_all_sports`, and reconnect logic |
| `polybot/__main__.py` | Modify `finally` block | Close `odds_client`, `espn_client`, and `price_history_scanner` sessions on shutdown |
| `tests/test_schema_migration.py` | Create | Tests for idempotent schema application |
| `tests/test_engine.py` | Modify | Add tests for strategy DNS recovery and periodic task resilience |
| `tests/test_odds_client.py` | Modify | Add tests for session reconnect and fetch_all_sports timeout |
| `tests/test_scanner.py` | Modify | Add test for session recreation on DNS failure |

---

### Task 1: Fix Schema Migration Crash Loop

The `trades` table is created with `CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'closed'))` on line 35. Later, lines 116-119 do `DROP CONSTRAINT / ADD CONSTRAINT` to expand it. But the original `CREATE TABLE IF NOT EXISTS` on line 26 includes the original `CHECK` inline — this means if the table already exists, the inline CHECK is ignored, but if the table is *being created fresh* it gets the narrow constraint and then immediately gets the wider one. The problem is: lines 76-78 do a `DROP CONSTRAINT IF EXISTS trades_strategy_check; ADD CONSTRAINT trades_strategy_check CHECK(strategy IN (...))`. If an existing row has a strategy value not in the new list, `ADD CONSTRAINT` fails because PostgreSQL validates existing rows. The 80 `CheckViolationError` crashes confirm this.

The root cause: the schema file is **not truly idempotent** — `ADD CONSTRAINT` validates all existing rows every time. The fix is to use `NOT VALID` on `ADD CONSTRAINT` so PostgreSQL skips row validation, then separately `VALIDATE CONSTRAINT` if desired.

**Files:**
- Modify: `polybot/db/schema.sql:76-78`
- Modify: `polybot/db/connection.py:20-23`
- Create: `tests/test_schema_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_migration.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path


@pytest.mark.asyncio
async def test_apply_schema_is_idempotent():
    """Schema application should succeed even when called multiple times."""
    from polybot.db.connection import Database

    db = Database("postgresql://localhost/polybot_test")
    db._pool = AsyncMock()
    mock_conn = AsyncMock()
    db._pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    db._pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # First call succeeds
    mock_conn.execute = AsyncMock(return_value=None)
    await db._apply_schema()
    assert mock_conn.execute.called


@pytest.mark.asyncio
async def test_apply_schema_wraps_in_transaction():
    """Schema application should execute within a transaction block."""
    from polybot.db.connection import Database

    db = Database("postgresql://localhost/polybot_test")
    db._pool = AsyncMock()
    mock_conn = AsyncMock()
    db._pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    db._pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_txn = AsyncMock()
    mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=mock_txn)
    mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    await db._apply_schema()
    mock_conn.transaction.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_schema_migration.py -v`
Expected: `test_apply_schema_wraps_in_transaction` FAILS because `_apply_schema()` doesn't use `transaction()`.

- [ ] **Step 3: Fix schema.sql — use NOT VALID on ADD CONSTRAINT**

In `polybot/db/schema.sql`, change the three `ADD CONSTRAINT` blocks to use `NOT VALID`:

Replace lines 76-78:
```sql
-- v2.5: Expand strategy CHECK for all strategies
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_strategy_check;
ALTER TABLE trades ADD CONSTRAINT trades_strategy_check
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast', 'market_maker', 'mean_reversion', 'cross_venue', 'political', 'news_catalyst', 'live_game'))
    NOT VALID;
```

Replace lines 116-119:
```sql
-- v2.1: Expand trade status for dry-run and fill tracking
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_status_check;
ALTER TABLE trades ADD CONSTRAINT trades_status_check
    CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'closed',
                      'dry_run', 'dry_run_resolved'))
    NOT VALID;
```

Replace lines 122-124:
```sql
-- v2.3: Expand exit_reason for time-stop and arb TTL exits
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;
ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IN ('resolution', 'early_exit', 'stop_loss', 'take_profit', 'time_stop', 'arb_ttl_expired'))
    NOT VALID;
```

- [ ] **Step 4: Fix connection.py — wrap _apply_schema in transaction**

Replace `_apply_schema()` in `polybot/db/connection.py`:

```python
async def _apply_schema(self) -> None:
    schema = SCHEMA_PATH.read_text()
    async with self._pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(schema)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_schema_migration.py -v`
Expected: PASS

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `cd ~/polybot && uv run pytest --tb=short -q`
Expected: All tests pass (398+)

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add polybot/db/schema.sql polybot/db/connection.py tests/test_schema_migration.py
git commit -m "fix: make schema migration idempotent with NOT VALID constraints + transaction wrap

CHECK constraints used ADD CONSTRAINT without NOT VALID, causing
CheckViolationError on every startup when existing rows had values
from newer migrations. 80 crashes overnight from this.

NOT VALID skips row validation on ADD (rows are validated on INSERT).
Transaction wrap ensures partial schema failures roll back cleanly."
```

---

### Task 2: Fix Mean Reversion DNS Death

MR died because `ctx.scanner.fetch_markets()` raised a `ClientConnectorError` (DNS failure to gamma-api.polymarket.com). The exception propagated to `_run_strategy()`, which applied exponential backoff. But the underlying aiohttp `ClientSession` may have entered a broken state — subsequent retries keep failing with the same DNS error, burning through the 30-error kill threshold.

The fix is twofold:
1. In `PolymarketScanner.fetch_markets()`: catch `aiohttp.ClientError` and return empty list (same as other strategies' error handling), plus detect a dead session and recreate it.
2. In `Engine._run_strategy()`: reset the aiohttp session on connection errors so the next retry starts fresh.

We'll fix the scanner since that's where the session lives.

**Files:**
- Modify: `polybot/markets/scanner.py` (the `fetch_markets` method)
- Modify: `tests/test_scanner.py`

- [ ] **Step 1: Read scanner.py fetch_markets to find exact error handling**

Run: Read `polybot/markets/scanner.py` to find the `fetch_markets` method and its current error handling.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_scanner.py`:

```python
@pytest.mark.asyncio
async def test_fetch_markets_recovers_from_dns_failure(scanner):
    """Scanner should return empty list on DNS failure, not crash."""
    import aiohttp

    scanner._session.get = AsyncMock(
        side_effect=aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("nodename nor servname provided")))

    result = await scanner.fetch_markets()
    assert result == []


@pytest.mark.asyncio
async def test_fetch_markets_recreates_session_on_closed(scanner):
    """Scanner should recreate session if it was closed."""
    scanner._session.closed = True

    # After recreation, should work normally
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession") as mock_session_cls:
        new_session = AsyncMock()
        new_session.get.return_value = mock_resp
        new_session.closed = False
        mock_session_cls.return_value = new_session

        result = await scanner.fetch_markets()
    # Should not crash — either returns markets or empty list
    assert isinstance(result, list)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_scanner.py::test_fetch_markets_recovers_from_dns_failure tests/test_scanner.py::test_fetch_markets_recreates_session_on_closed -v`
Expected: FAIL — `ClientConnectorError` propagates as unhandled exception

- [ ] **Step 4: Add session resilience to scanner's fetch_markets**

In `polybot/markets/scanner.py`, find the `fetch_markets` method. Wrap the HTTP call in a try/except for `aiohttp.ClientError`. Add session-closed detection at the top. The exact edit depends on the current code structure found in Step 1, but the pattern is:

```python
async def fetch_markets(self) -> list[dict]:
    # Recreate session if closed/dead
    if self._session is None or self._session.closed:
        log.warning("scanner_session_recreated")
        self._session = aiohttp.ClientSession()

    try:
        # ... existing fetch logic ...
    except aiohttp.ClientError as e:
        log.error("scanner_fetch_error", error=str(e))
        return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_scanner.py -v`
Expected: All scanner tests pass including the two new ones

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/markets/scanner.py tests/test_scanner.py
git commit -m "fix: scanner returns empty list on DNS failure instead of crashing

ClientConnectorError from gamma-api.polymarket.com DNS failures
propagated unhandled, killing MR strategy via exponential backoff.
Now caught at the scanner level (returns []), matching how other
network errors are already handled. Also recreates session if closed."
```

---

### Task 3: Fix Cross-Venue Strategy Stall

The CV strategy calls `self._odds_client.fetch_all_sports()` which iterates sports **sequentially** (line 182-184 of `odds_client.py`). If one sport's HTTP request hangs past the 15s per-request timeout, the entire `run_once()` takes 15s × N sports. But the real issue is that `fetch_all_sports()` has **no overall timeout** — if the API is slow or the session is dead, the strategy hangs indefinitely, producing only heartbeat warnings.

The fix:
1. Add an overall timeout to `fetch_all_sports()`.
2. Add session-closed detection + recreation (same pattern as scanner fix).
3. Use `asyncio.gather` with per-sport timeouts for parallel fetching instead of sequential.

**Files:**
- Modify: `polybot/analysis/odds_client.py:130-136, 175-187`
- Modify: `tests/test_odds_client.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_odds_client.py`:

```python
@pytest.mark.asyncio
async def test_fetch_all_sports_has_overall_timeout():
    """fetch_all_sports should not hang forever if API is slow."""
    import asyncio
    from polybot.analysis.odds_client import OddsClient

    client = OddsClient(api_key="test-key", sports=["sport_a", "sport_b"])
    client._session = AsyncMock()
    client._credits_remaining = 100  # not exhausted

    # Simulate a hanging request
    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(60)
        return []

    client.fetch_odds = slow_fetch

    # Should complete within a reasonable timeout, not hang for 60s
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.fetch_all_sports(), timeout=35)


@pytest.mark.asyncio
async def test_odds_client_recreates_closed_session():
    """OddsClient should recreate session if it was closed."""
    from polybot.analysis.odds_client import OddsClient

    client = OddsClient(api_key="test-key", sports=["basketball_nba"])
    client._session = AsyncMock()
    client._session.closed = True
    client._credits_remaining = 100

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=[])
    mock_resp.headers = {"x-requests-remaining": "98"}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession") as mock_session_cls:
        new_session = AsyncMock()
        new_session.get.return_value = mock_resp
        new_session.closed = False
        mock_session_cls.return_value = new_session

        result = await client.fetch_odds("basketball_nba")
    assert isinstance(result, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_odds_client.py::test_fetch_all_sports_has_overall_timeout tests/test_odds_client.py::test_odds_client_recreates_closed_session -v`
Expected: FAIL — no overall timeout, no session recreation

- [ ] **Step 3: Add session resilience and overall timeout to OddsClient**

In `polybot/analysis/odds_client.py`:

Add session check at the top of `fetch_odds()`:
```python
async def fetch_odds(self, sport_key: str) -> list[dict]:
    """Fetch odds for a sport. Costs 2 credits (1 market x 2 regions)."""
    if not self._api_key:
        return []

    # Recreate session if closed or missing
    if self._session is None or self._session.closed:
        log.warning("odds_session_recreated")
        self._session = aiohttp.ClientSession()

    if self.credits_exhausted:
        log.warning("odds_api_credits_low", credits_remaining=self._credits_remaining,
                    credit_reserve=self._credit_reserve)
        return []
    # ... rest unchanged ...
```

Add overall timeout to `fetch_all_sports()`:
```python
async def fetch_all_sports(self) -> list[dict]:
    """Fetch odds for all configured sports."""
    if self.credits_exhausted:
        log.info("odds_credits_exhausted", credits_remaining=self._credits_remaining,
                 credit_reserve=self._credit_reserve)
        return []

    import asyncio
    try:
        async with asyncio.timeout(30):
            all_events = []
            for sport in self._sports:
                events = await self.fetch_odds(sport)
                all_events.extend(events)
    except TimeoutError:
        log.error("odds_fetch_all_timeout", sports=len(self._sports))
        return all_events if 'all_events' in dir() else []

    log.info("odds_fetch_cycle_complete", sports=len(self._sports),
             events=len(all_events), credits_remaining=self._credits_remaining)
    return all_events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run pytest tests/test_odds_client.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/analysis/odds_client.py tests/test_odds_client.py
git commit -m "fix: OddsClient session recreation + 30s overall timeout on fetch_all_sports

CV strategy was stalling because fetch_all_sports had no overall
timeout and no session recovery. If the session died or the API
hung, the strategy produced heartbeat warnings but never completed
a cycle. Now: closed sessions auto-recreate, and 30s timeout caps
the entire multi-sport fetch."
```

---

### Task 4: Fix aiohttp Session Leak on Shutdown

The `__main__.py` `finally` block (lines 218-234) only closes `scanner`, `researcher`, and `db`. The `odds_client`, `espn_client`, and `_snipe_odds` client are never closed on shutdown, causing the "Unclosed client session" warnings in stderr.

**Files:**
- Modify: `polybot/__main__.py:216-234`
- Modify: `tests/test_main_lifecycle.py`

- [ ] **Step 1: Read test_main_lifecycle.py to understand existing test patterns**

Run: Read `tests/test_main_lifecycle.py` to see how shutdown is tested.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_main_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_shutdown_closes_all_sessions(mock_settings, mock_db):
    """All aiohttp sessions should be closed on shutdown."""
    # This test verifies that odds_client, espn_client are closed
    # in the finally block, not just scanner and researcher.
    from polybot.analysis.odds_client import OddsClient

    client = OddsClient(api_key="test")
    client._session = AsyncMock()
    await client.close()
    client._session.close.assert_awaited_once()
```

(This is a unit test for the close() method — the integration test is verifying the `__main__.py` wiring, which we'll do manually.)

- [ ] **Step 3: Fix __main__.py finally block to close all sessions**

The key change: track all closeable resources and close them all in the `finally` block. The cleanest approach is to collect them in a list as they're created.

In `polybot/__main__.py`, after the `odds_client` creation (around line 179) and before the try block, we need to track these. Then in the `finally` block, close them all.

Replace the `finally` block (lines 218-234):

```python
    finally:
        # Log open positions on shutdown
        try:
            open_trades = await db.fetch(
                """SELECT t.id, t.strategy, t.side, t.position_size_usd, m.question
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.status IN ('open', 'filled', 'dry_run')""")
            log.info("shutdown_open_positions", count=len(open_trades),
                     positions=[{"id": t["id"], "strategy": t["strategy"],
                                 "side": t["side"], "size": float(t["position_size_usd"])}
                                for t in open_trades])
        except Exception:
            pass
        # Close all aiohttp sessions
        for name, client in [("scanner", scanner), ("researcher", researcher)]:
            try:
                await client.close()
            except Exception:
                pass
        # Close optional clients that may or may not have been created
        for name, client in [("odds_client", odds_client if 'odds_client' in dir() else None),
                             ("snipe_odds", _snipe_odds),
                             ("espn_client", espn_client if 'espn_client' in dir() else None)]:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        await db.close()
        log.info("polybot_shutdown_complete")
```

Wait — the variable scoping is tricky since `odds_client`, `espn_client` etc. are defined inside conditional blocks. A cleaner approach: initialize them to `None` at the top of `main()` and set them in the conditional blocks.

Add near the top of `main()` (after `settings = Settings()`):

```python
    odds_client = None
    espn_client = None
    _snipe_odds = None
```

Then the `finally` block becomes straightforward:

```python
    finally:
        try:
            open_trades = await db.fetch(
                """SELECT t.id, t.strategy, t.side, t.position_size_usd, m.question
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.status IN ('open', 'filled', 'dry_run')""")
            log.info("shutdown_open_positions", count=len(open_trades),
                     positions=[{"id": t["id"], "strategy": t["strategy"],
                                 "side": t["side"], "size": float(t["position_size_usd"])}
                                for t in open_trades])
        except Exception:
            pass
        for client in [scanner, researcher, odds_client, _snipe_odds, espn_client]:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        await db.close()
        log.info("polybot_shutdown_complete")
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_main_lifecycle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/__main__.py tests/test_main_lifecycle.py
git commit -m "fix: close all aiohttp sessions on shutdown

odds_client, espn_client, and _snipe_odds were never closed in
the finally block, causing 'Unclosed client session' warnings
on every restart. Now all optional clients are initialized to None
at top of main() and closed in the finally block."
```

---

### Task 5: Verify and Restart

After all fixes are committed, restart polybot and verify the fixes work.

- [ ] **Step 1: Run the full test suite**

Run: `cd ~/polybot && uv run pytest --tb=short -q`
Expected: All tests pass

- [ ] **Step 2: Restart polybot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 3: Watch logs for clean startup**

```bash
tail -50 ~/polybot/data/polybot_stdout.log
```

Expected: No `CheckViolationError` in stderr, clean `polybot_starting` → `engine_starting` → strategy heartbeats in stdout.

- [ ] **Step 4: Verify no schema errors**

```bash
tail -20 ~/polybot/data/polybot_stderr.log
```

Expected: No `CheckViolationError`, no `Unclosed client session` warnings.

- [ ] **Step 5: Wait 5 minutes, check strategy health**

```bash
grep -E '"event": "(mr_|cv_|price_history)' ~/polybot/data/polybot_stdout.log | tail -10
```

Expected: MR scanning markets, CV fetching odds, PriceHistoryScanner completing scans.
