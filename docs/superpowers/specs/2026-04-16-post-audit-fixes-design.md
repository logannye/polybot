# Post-Audit Fixes — Design Spec

## Context

After Polybot lost $623 (93% of capital) going live on April 10, safeguards were shipped on April 14: drawdown halt, capital divergence monitor, live preflight, realistic dry-run, and graduated deployment stages. An audit on April 16 found 8 issues — some in the new safeguards, some in the bot's operational state.

**Current state:** Bot is running in dry-run mode. MM simulation has recorded 156K fake fills at -$5,582 cumulative P&L. Zero trades have been placed since April 14 because MR and Forecast (the only strategies with positive dry-run edge) are disabled. One orphaned live trade ($15.36) sits in the DB with an unchecked CLOB order. Several code quality issues in the safeguards need fixing.

## Scope: 4 Groups

### Group A: Remove MM Dry-Run Simulation

**Problem:** `_simulate_fills()` in `MarketMakerStrategy` counts every 5-second quote cycle as a fill, producing -$5,582 in fake P&L across 156K fills. The spread math (`prev_price - bid.price`) goes negative when the scanner's cached price drifts even slightly. Market making can't be realistically simulated in dry-run because maker fill probability depends on queue position, adverse selection, and order flow — none of which are observable without real orders on the book.

**Fix:** Delete the simulation. Keep quoting logic for observability.

**Changes to `polybot/strategies/market_maker.py`:**
- Delete `_simulate_fills()` method
- Remove `if self._dry_run: self._simulate_fills()` call in `run_once()`
- Remove `_sim_pnl`, `_sim_fills`, `_prev_prices` instance variables from `__init__`
- Add a summary log line at the end of `run_once()` when in dry-run: log the number of active markets and average bid-ask spread being quoted, so there's still observability without fake P&L

**What stays:** `InventoryTracker`, `QuoteManager`, market selection, heartbeat (no-ops in dry-run). These are needed for live mode.

---

### Group B: Re-enable MR and Forecast for Realistic Dry-Run

**Problem:** The 72-hour realistic dry-run validation period (from Spec 3.4, April 14) cannot proceed because the two strategies with demonstrated edge are disabled in config. MR showed +$107 on 43 trades (47% win rate) and Forecast showed +$24 on 87 trades (23% win rate, but large winners) in pre-realistic dry-run.

**Fix:** Re-enable both strategies. They will now flow through the realistic dry-run path in the executor (order book fetch, spread filter at 15%, fill at best ask with 2% simulated taker fee). This effectively restarts the 72-hour clock.

**Changes to `polybot/core/config.py`:**
- `mr_enabled: bool = True` (was `False`)
- `forecast_enabled: bool = True` (was `False`)

All other strategies remain at their current enabled/disabled state.

---

### Group C: Resolve Orphaned Live Trade

**Problem:** Trade #942 (Forecast, NO on "Will the Philadelphia Flyers make the NHL Playoffs?", entry $0.35, $15.36 deployed) has been in `open` status since April 13 with CLOB order ID `0x40233f...`. The bot is now in dry-run mode so the fill monitor (live-only) isn't checking it. The capital is locked.

**Fix:** A one-time script that checks the CLOB order status and resolves accordingly.

**New file `scripts/resolve_orphan_trade.py`:**
1. Load credentials from `.env`
2. Instantiate `ClobClient` directly (no async needed for one call)
3. Call `client.get_order(order_id)` for trade #942's CLOB order ID
4. Branch on status:
   - `MATCHED`: Update trade status to `filled` in DB. The position manager will pick it up and manage it (TP/SL/time-stop).
   - `LIVE` (unfilled after 3 days): Cancel the CLOB order via `client.cancel(order_id)`, update trade to `cancelled`, free deployed capital (`total_deployed -= 15.36`).
   - `CANCELLED`/other: Update trade to `cancelled`, free deployed capital.
5. Print the action taken for operator verification.

This script is run once manually, then kept as a utility for future orphan resolution.

---

### Group D: Code Quality Fixes

**D1: Capital Divergence Self-Healing**

**Problem:** `_capital_divergence_halted` in `engine.py` is a one-way flag. Once a CLOB API glitch or transient network error causes divergence > 10%, all trading halts permanently until process restart.

**Fix in `polybot/core/engine.py`:**
- Add `_capital_divergence_ok_count: int = 0` to `__init__`
- In `_check_capital_divergence()`:
  - When divergence exceeds threshold: set `_halted = True`, reset `_ok_count = 0`, send alert (existing behavior)
  - When divergence is within threshold AND `_halted` is `True`: increment `_ok_count`. If `_ok_count >= 3` (3 consecutive 60s checks = 3 minutes of healthy state), set `_halted = False` and log/email recovery
  - When divergence is within threshold AND `_halted` is `False`: reset `_ok_count = 0` (normal state)

**D2: Cache Drawdown Check**

**Problem:** `_check_drawdown_halt()` queries `system_state` before every `strategy.run_once()` call. With 7+ strategies at various intervals, this is 7+ DB round-trips per cycle for a value that changes only on trade events.

**Fix in `polybot/core/engine.py`:**
- Add `_drawdown_cache: tuple[bool, float] | None = None` to `__init__` (stores `(is_halted, cache_time)`)
- At the top of `_check_drawdown_halt()`: if cache exists and `time.monotonic() - cache_time < 30`, return the cached boolean
- After the DB query and computation, update the cache before returning
- 30 seconds of staleness is acceptable for a circuit breaker that requires manual DB reset to clear

**D3: ClobGateway Order Book Encapsulation**

**Problem:** The realistic dry-run path in `executor.py:51` accesses `self._clob._client.get_order_book(token_id)`, reaching through the gateway's private attribute.

**Fix:**
- Add to `polybot/trading/clob.py`:
  ```
  async def get_order_book_summary(self, token_id: str) -> dict | None:
      """Fetch order book and return best bid, best ask, and spread.
      Returns None if book is empty or on error."""
  ```
  Returns `{"best_bid": float, "best_ask": float, "spread": float}` or `None`.
- In `polybot/trading/executor.py`: replace the `_client.get_order_book` call with `self._clob.get_order_book_summary(token_id)` and destructure the result.
- The existing `get_market_price()` and `get_book_spread()` methods on ClobGateway can be kept for backward compatibility or refactored to call `get_order_book_summary` internally — implementer's choice.

**D4: Seed InventoryTracker from Trades Table on Startup**

**Problem:** `InventoryTracker` is purely in-memory. After a restart during live MM trading, inventory state is lost and quote skewing starts from zero — potentially posting quotes that accumulate inventory in the wrong direction.

**Fix in `polybot/strategies/market_maker.py`:**
- Add `_inventory_reconciled: bool = False` to `__init__`
- Add method:
  ```
  async def _reconcile_inventory(self, ctx):
      """Seed inventory from filled MM trades in DB. Called once on first run_once()."""
  ```
  Queries `SELECT mk.polymarket_id, t.side, SUM(t.shares) FROM trades t JOIN markets mk ON t.market_id = mk.id WHERE t.strategy='market_maker' AND t.status='filled' GROUP BY mk.polymarket_id, t.side`, then calls `self._inventory.record_fill()` for each row.
- In `run_once()`, before the quoting loop: if `not self._inventory_reconciled`, call `_reconcile_inventory(ctx)` and set the flag.
- This only matters for live mode but is harmless in dry-run (query returns no rows).

---

## Ship Order

1. **Group A** (MM simulation removal) — stops the -$5,582 bleed immediately
2. **Group B** (re-enable strategies) — starts the realistic dry-run clock
3. **Group C** (orphan trade) — frees locked capital, cleans DB state
4. **Group D** (code quality) — D1-D4 in any order, all independent

## Testing

- Existing tests for safeguards and realistic dry-run should still pass
- New tests needed:
  - Capital divergence self-healing (trip, then 3 OK checks → recovery)
  - Drawdown cache (returns cached result within 30s, queries DB after 30s)
  - `get_order_book_summary` returns correct dict structure
  - MM `run_once` in dry-run no longer calls `_simulate_fills` (verify no `mm_sim_*` log events)
  - Inventory reconciliation seeds from trades table
