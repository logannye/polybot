# Post-Mortem Safeguards & Realistic Dry-Run — Design Spec

## Context

Polybot went live on April 10, 2026 with $669.85. By April 14, $623.54 was lost (93%).
Dry-run performance (+$107 MR, +$20 forecast over 8 days) did not transfer to live trading
due to: untested CLOB API code paths, no live fill tracking for MM, thin order books
with 50-98% spreads invisible in dry-run, and no total drawdown protection.

## Scope: 3 Independent Specs

### Spec 1: Safeguards (ship first)

**Goal:** Prevent catastrophic losses from ever happening again.

**1.1 Total Account Drawdown Halt**
- New config: `max_total_drawdown_pct: float = 0.30`
- New DB column: `system_state.high_water_bankroll` (updated when bankroll exceeds previous high)
- Check at start of every `run_once()` in engine's strategy loop:
  ```
  if current_bankroll < high_water * (1 - max_total_drawdown_pct):
      halt all strategies, send email alert, log critical
  ```
- Halt persists across restarts — stored as `system_state.drawdown_halt_until` (set to far future)
- Manual reset required: `UPDATE system_state SET drawdown_halt_until = NULL, high_water_bankroll = bankroll`

**1.2 Capital Divergence Monitor**
- New periodic task in engine (every 60s): compare `CLOB_balance + sum(open_position_sizes)` vs `system_state.bankroll`
- New config: `max_capital_divergence_pct: float = 0.10`
- If divergence > 10%, halt all strategies, email alert, log critical
- Prevents the MM scenario where $419 was spent without DB tracking

**1.3 Live Preflight Script (`scripts/live_preflight.py`)**
- Runs automatically on startup when `dry_run=false`
- Tests (all must pass to continue):
  - `get_balance_allowance` returns a sensible dollar amount (>$0, <$1M)
  - `submit_order` places a $0.01 buy on a liquid market, verifies it appears, cancels it
  - `post_heartbeat` sends 3 consecutive heartbeats successfully
  - All 3 exchange contracts have conditional token approval
  - USDC.e collateral balance matches expected bankroll (within 10%)
- On failure: logs exactly which check failed, refuses to start strategies

**1.4 Deployment Stage Gate**
- New config: `live_deployment_stage: str = "dry_run"` (values: `dry_run`, `micro_test`, `full`)
- `micro_test`: overrides `max_total_deployed_pct` to 0.05 (5% of bankroll)
- `full`: uses configured limits
- Preflight script refuses `DRY_RUN=false` unless `live_deployment_stage` is `micro_test` or `full`
- Preflight refuses `full` unless a `micro_test` session has completed (tracked via `system_state.micro_test_completed_at`)

---

### Spec 2: MM Live Fill Tracking (ship second)

**Goal:** MM must track every order and fill in the trades table, same as all other strategies.

**2.1 MM Orders Recorded in Trades Table**
- Every `place_two_sided()` call records both bid and ask in trades table with:
  - `strategy = 'market_maker'`
  - `status = 'open'` (live) or `dry_run` (simulation)
  - `clob_order_id` from the CLOB response
  - `side = 'YES'` (bid) or `side = 'NO'` (ask — selling YES tokens)
- Cancelled/replaced quotes update the trade status to `cancelled`

**2.2 Fill Monitor Tracks MM Orders**
- The existing `engine._fill_monitor` already polls `status='open'` trades — MM orders will be picked up automatically once they're in the trades table
- On fill: update status to `filled`, sync bankroll from CLOB
- On cancel: update status to `cancelled`, free deployed capital

**2.3 MM Inventory From Trades Table**
- Replace in-memory `InventoryTracker` with queries against the trades table
- `SELECT SUM(shares) FROM trades WHERE strategy='market_maker' AND status='filled' AND side='YES'` = YES inventory
- This is slower but ensures inventory is always consistent with the DB

**2.4 MM Stale Inventory Check**
- Before posting new quotes, MM checks: is `CLOB_cash + tracked_positions` consistent with expected bankroll?
- If not, halt MM and log error — don't post new orders on top of untracked inventory

---

### Spec 3: Realistic Dry-Run (ship third)

**Goal:** Dry-run results must approximate live trading conditions.

**3.1 Spread-Aware Dry-Run Fills**
- When `dry_run=true` and placing an order, fetch the real order book
- If order crosses spread: fill at best ask (buys) or best bid (sells), apply `dry_run_taker_fee_pct` (default 2%)
- If order does not cross: record as `dry_run_resting`, fill only when future price check shows market crossed our price
- If no order book available: reject the order (don't fill at model price)

**3.2 Spread Filter in Dry-Run**
- All spread/liquidity filters (mr_max_spread, mr_min_book_depth) apply in dry-run mode
- Currently these are skipped because dry-run doesn't fetch order books — change to always fetch

**3.3 MM Dry-Run Uses Trades Table**
- MM dry-run records quotes in trades table (same as Spec 2 live mode)
- Fill simulation checks real order book, not just price crossings
- Inventory tracked via DB, not in-memory

**3.4 Graduated Deployment Process**
1. Run `DRY_RUN_REALISTIC=true` for minimum 72 hours
2. Review: are fills, P&L, and capital utilization realistic?
3. Switch to `micro_test` stage with real capital (5% deployed max, ~$25)
4. Run micro_test for 24 hours minimum
5. Review: did fills work? Did exits work? Is P&L tracking accurate?
6. Only then: switch to `full` stage

---

## Ship Order

1. **Safeguards** — protects remaining capital, blocks premature deployment
2. **MM Live Tracking** — required before MM can ever run live again
3. **Realistic Dry-Run** — ensures next live deployment is validated by realistic simulation
