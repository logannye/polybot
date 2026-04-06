# Cross-Venue Strategy DB Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock the cross-venue strategy by adding `cross_venue` to the trades table CHECK constraint, fixing `open_count=0` bypass bugs in cross_venue and mean_reversion, and adding test coverage for the fix.

**Architecture:** The `trades.strategy` CHECK constraint in `schema.sql` needs to include all 6 strategy names. The schema is applied idempotently on startup via `Database._apply_schema()`. We also need to fix the `open_count=0` hardcode in two strategies that silently bypasses max-concurrent-position checks — both should query actual open trade count like forecast/snipe do.

**Tech Stack:** PostgreSQL, Python 3.13, asyncpg, pytest, asyncio

---

### Task 1: Update Schema CHECK Constraint

**Files:**
- Modify: `polybot/db/schema.sql:73-74`

The schema file defines the strategy column with an inline CHECK that only allows `('arbitrage', 'snipe', 'forecast')`. The live DB has a named constraint `trades_strategy_check` that was manually expanded to also include `market_maker` and `mean_reversion`, but `cross_venue` was never added. We need to:
1. Update the schema.sql to use a named DROP/ADD constraint pattern (matching how `trades_status_check` and `trades_exit_reason_check` are already done on lines 110-118)
2. Include all 6 strategies

- [ ] **Step 1: Update schema.sql to replace inline CHECK with named constraint**

Replace lines 73-74 in `polybot/db/schema.sql`:

```sql
-- v2: Strategy column on trades
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast')) NOT NULL DEFAULT 'forecast';
```

With:

```sql
-- v2: Strategy column on trades
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'forecast';

-- v2.5: Expand strategy CHECK for all strategies
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_strategy_check;
ALTER TABLE trades ADD CONSTRAINT trades_strategy_check
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast', 'market_maker', 'mean_reversion', 'cross_venue'));
```

Note: The inline CHECK on the `ADD COLUMN` must be removed because PostgreSQL won't let us `DROP CONSTRAINT` on an unnamed inline check by the auto-generated name reliably. The named constraint on the next lines takes over.

- [ ] **Step 2: Apply the constraint to the live database immediately**

Run this directly against the live DB so the bot doesn't need a restart to unblock:

```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_strategy_check;
ALTER TABLE trades ADD CONSTRAINT trades_strategy_check
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast', 'market_maker', 'mean_reversion', 'cross_venue'));
"
```

Expected: `ALTER TABLE` (twice, no errors)

- [ ] **Step 3: Verify constraint is correct**

```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -c "
SELECT constraint_name, check_clause FROM information_schema.check_constraints
WHERE constraint_name = 'trades_strategy_check';
"
```

Expected: One row showing all 6 strategies in the check_clause.

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add polybot/db/schema.sql && git commit -m "fix: add cross_venue to trades strategy CHECK constraint

The cross_venue strategy was blocked from inserting trades because the
trades_strategy_check constraint didn't include 'cross_venue'. Also
converted from inline CHECK to named DROP/ADD pattern for consistency
with trades_status_check and trades_exit_reason_check."
```

---

### Task 2: Fix open_count=0 Bypass in CrossVenueStrategy

**Files:**
- Modify: `polybot/strategies/cross_venue.py:124-129`
- Test: `tests/test_cross_venue.py`

The `PortfolioState` constructed in `cross_venue.py` hardcodes `open_count=0` and `category_deployed={}`, which means the max-concurrent-positions check and per-category limit checks always pass. The forecast strategy does this correctly by querying open trades first. We should do the same.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cross_venue.py`:

```python
@pytest.mark.asyncio
async def test_run_once_respects_max_concurrent_positions():
    """Should reject trades when open_count >= max_concurrent."""
    s = _make_settings()
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
    ctx.db.fetchval = AsyncMock(side_effect=[
        True,    # enabled check
        1,       # market upsert (we may not reach this)
    ])
    ctx.db.fetchrow = AsyncMock(return_value={
        "bankroll": 500.0, "total_deployed": 0.0, "daily_pnl": 0.0,
        "post_breaker_until": None, "circuit_breaker_until": None,
    })
    # Return 12 open trades (max_concurrent default)
    ctx.db.fetch = AsyncMock(return_value=[{"position_size_usd": 10, "category": "sports"}] * 12)
    ctx.executor = AsyncMock()
    ctx.settings = s
    ctx.scanner = MagicMock()
    ctx.scanner.get_all_cached_prices.return_value = {
        "m1": {"polymarket_id": "0xabc", "question": "Will the Los Angeles Lakers win?",
               "yes_price": 0.45, "category": "sports", "book_depth": 5000,
               "resolution_time": "2026-04-10T00:00:00Z", "volume_24h": 10000,
               "yes_token_id": "tok1", "no_token_id": "tok2"},
    }
    ctx.risk_manager = RiskManager()
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()

    await strategy.run_once(ctx)
    ctx.executor.place_order.assert_not_called()
```

Add these imports at the top of the test file:

```python
import asyncio
from polybot.trading.risk import RiskManager
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd ~/polybot && python -m pytest tests/test_cross_venue.py::test_run_once_respects_max_concurrent_positions -v
```

Expected: FAIL — because `open_count=0` means risk check passes even with 12 open trades.

- [ ] **Step 3: Fix open_count and category_deployed in cross_venue.py**

Replace lines 124-129 in `polybot/strategies/cross_venue.py`:

```python
                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=0, category_deployed={},
                    circuit_breaker_until=state_row.get("circuit_breaker_until"))
```

With:

```python
                open_trades = await ctx.db.fetch(
                    """SELECT t.position_size_usd, m.category
                       FROM trades t JOIN markets m ON t.market_id = m.id
                       WHERE t.status IN ('open', 'filled', 'dry_run')""")
                cat_deployed: dict[str, float] = {}
                for t in open_trades:
                    cat = t["category"]
                    cat_deployed[cat] = cat_deployed.get(cat, 0.0) + float(t["position_size_usd"])
                portfolio = PortfolioState(
                    bankroll=bankroll,
                    total_deployed=float(state_row["total_deployed"]),
                    daily_pnl=float(state_row["daily_pnl"]),
                    open_count=len(open_trades), category_deployed=cat_deployed,
                    circuit_breaker_until=state_row.get("circuit_breaker_until"))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd ~/polybot && python -m pytest tests/test_cross_venue.py::test_run_once_respects_max_concurrent_positions -v
```

Expected: PASS

- [ ] **Step 5: Run all cross-venue tests**

```bash
cd ~/polybot && python -m pytest tests/test_cross_venue.py -v
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/strategies/cross_venue.py tests/test_cross_venue.py && git commit -m "fix: query actual open trade count in cross_venue strategy

open_count was hardcoded to 0, bypassing the max-concurrent-positions
risk check. Now queries open trades like forecast strategy does."
```

---

### Task 3: Fix open_count=0 Bypass in MeanReversionStrategy

**Files:**
- Modify: `polybot/strategies/mean_reversion.py:~226`
- Test: `tests/test_mean_reversion.py`

Same bug as cross_venue — `open_count=0` and `category_deployed={}` are hardcoded.

- [ ] **Step 1: Read the mean_reversion.py file to find exact line numbers**

Read `polybot/strategies/mean_reversion.py` and locate the `PortfolioState(` construction with `open_count=0`.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_mean_reversion.py` (adapt imports and fixture patterns from the existing tests in that file):

```python
@pytest.mark.asyncio
async def test_run_once_respects_max_concurrent_positions():
    """Should reject trades when open_count >= max_concurrent."""
    # Build a scenario where MR finds a signal but should be rejected
    # because 12 positions are already open.
    # Setup: mock ctx with 12 open trades in db.fetch return
    # Assert: executor.place_order not called
    # (Exact mock setup depends on existing test patterns in this file)
```

Note to implementer: Mirror the test pattern from Task 2 but adapted for MR's `run_once` signature and mock expectations. Read the existing tests in `test_mean_reversion.py` first to follow established patterns.

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/polybot && python -m pytest tests/test_mean_reversion.py::test_run_once_respects_max_concurrent_positions -v
```

Expected: FAIL

- [ ] **Step 4: Apply the same fix to mean_reversion.py**

Replace the `PortfolioState(... open_count=0, category_deployed={} ...)` block with the same open-trade-query pattern used in Task 2 Step 3.

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ~/polybot && python -m pytest tests/test_mean_reversion.py::test_run_once_respects_max_concurrent_positions -v
```

Expected: PASS

- [ ] **Step 6: Run all mean reversion tests**

```bash
cd ~/polybot && python -m pytest tests/test_mean_reversion.py -v
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add polybot/strategies/mean_reversion.py tests/test_mean_reversion.py && git commit -m "fix: query actual open trade count in mean_reversion strategy

Same open_count=0 bypass as cross_venue — now queries open trades
for accurate max-concurrent-positions risk check."
```

---

### Task 4: Restart Bot and Verify Cross-Venue Trades

**Files:** None (operational)

- [ ] **Step 1: Restart the bot to pick up the code changes**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 2: Watch logs for cross-venue activity**

```bash
tail -f ~/polybot/data/polybot_stdout.log | grep --line-buffered "cross_venue\|cv_"
```

Wait for a scan cycle (~5 min). Expected: `cv_divergences_found`, then either `cv_trade` (success) or `cv_risk_rejected` / `cv_no_matching_market` (legitimate rejections, not DB errors).

- [ ] **Step 3: Verify no more constraint violation errors**

```bash
grep "trades_strategy_check\|strategy_error.*cross_venue" ~/polybot/data/polybot_stdout.log | tail -5
```

Expected: Only old errors from before the fix, no new ones.

- [ ] **Step 4: Run full test suite**

```bash
cd ~/polybot && python -m pytest -x -q
```

Expected: All tests pass.

- [ ] **Step 5: Commit any remaining changes**

If no further changes needed, skip this step.
