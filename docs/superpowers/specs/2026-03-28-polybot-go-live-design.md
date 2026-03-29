# Polybot Go-Live — Exchange Integration & Deployment Design

**Date:** 2026-03-28
**Status:** Draft
**Scope:** Wire real CLOB exchange integration, fill monitoring, dry-run mode, bankroll sync, resolution polling — everything needed to go from passing tests to live trading.

## Overview

The v2 strategy engine (arb, snipe, forecast) is complete with 172 passing tests, but the execution layer is stubbed — orders are recorded in the DB but never submitted to Polymarket's CLOB API. This spec closes the gap: real order signing/submission via `py-clob-client`, fill monitoring, dry-run observation mode, bankroll synchronization, and continuous resolution polling.

## Design Constraints

Decided during brainstorming:

- **Pre-generated L2 credentials** — `POLYMARKET_API_SECRET` and `POLYMARKET_API_PASSPHRASE` stored in `.env`, derived once via a setup script. No auto-derivation at startup.
- **Dry-run mode on by default** — `DRY_RUN=true` runs the full pipeline but skips CLOB submission. Must explicitly set `false` for live trading.
- **30-second fill polling** — single interval for all strategies. Simple and sufficient.
- **Bankroll sync on trade events only** — wallet balance read on fill confirmation, position close, and startup. No periodic RPC polling.
- **Dry-run resolution** — the resolution monitor computes simulated P&L for dry-run trades, giving realistic observation data.

---

## 1. CLOB Client Integration

### New Module: `polybot/trading/clob.py`

Async wrapper around `py-clob-client`'s synchronous `ClobClient`. All SDK calls routed through `asyncio.to_thread()`.

```python
class ClobGateway:
    def __init__(self, host, chain_id, private_key, api_key, api_secret, api_passphrase):
        # Constructs ClobClient with L2 auth

    async def submit_order(self, token_id, side, price, size, order_type="GTC") -> str:
        # create_order() + post_order(). Returns clob_order_id.

    async def cancel_order(self, clob_order_id) -> bool:
        # Cancels by ID. Returns success.

    async def get_order_status(self, clob_order_id) -> dict:
        # Returns {"status": "live"|"matched"|"cancelled", "size_matched": float}

    async def get_balance(self) -> float:
        # Reads USDC balance from CLOB balance tracking. Returns float.
```

### Changes to Existing Modules

**`polybot/strategies/base.py`** — Add `clob` field to `TradingContext`:
```python
@dataclass
class TradingContext:
    db: Any
    scanner: Any
    risk_manager: Any
    portfolio_lock: asyncio.Lock
    executor: Any
    email_notifier: Any
    settings: Any
    clob: Any  # ClobGateway instance (None in tests)
```

**`polybot/trading/wallet.py`** — Remove the `sign_order()` stub method. Keep `compute_shares()`, `get_usdc_balance()`, and `address`.

**`polybot/trading/executor.py`** — `place_order()` updated:
1. DB insert (same as now) — returns `trade_id`
2. If `dry_run=True`: set `status='dry_run'`, skip CLOB call, return
3. If `dry_run=False`: call `clob.submit_order()`, store returned `clob_order_id` in trade row

The `OrderExecutor` constructor gains two new fields: `clob` (ClobGateway or None) and `dry_run` (bool). These are set once at construction in `__main__.py`, not passed per-call. This avoids every strategy needing to thread `ctx.clob` and `ctx.settings.dry_run` through.

```python
class OrderExecutor:
    def __init__(self, scanner, wallet, db, fill_timeout_seconds=120,
                 clob=None, dry_run=False):
        self._scanner = scanner
        self._wallet = wallet
        self._db = db
        self._fill_timeout_seconds = fill_timeout_seconds
        self._clob = clob
        self._dry_run = dry_run

    async def place_order(self, token_id, side, size_usd, price, market_id, analysis_id,
                          strategy="forecast"):
        shares = self._wallet.compute_shares(size_usd, price)
        if shares <= 0:
            return None

        status = "dry_run" if self._dry_run else "open"
        trade_id = await self._db.fetchval(
            """INSERT INTO trades (market_id, analysis_id, side, entry_price, position_size_usd,
               shares, kelly_inputs, status, strategy)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
            market_id, analysis_id, side, price, size_usd, shares, "{}", status, strategy)

        clob_order_id = None
        if not self._dry_run and self._clob is not None:
            try:
                clob_order_id = await self._clob.submit_order(
                    token_id=token_id, side=side, price=price, size=shares)
                await self._db.execute(
                    "UPDATE trades SET clob_order_id = $1 WHERE id = $2",
                    clob_order_id, trade_id)
            except Exception as e:
                log.error("clob_submit_failed", trade_id=trade_id, error=str(e))
                await self._db.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade_id)
                return None

        return {"trade_id": trade_id, "order_id": clob_order_id, "shares": shares}
```

`place_multi_leg_order()` unchanged — it calls `self.place_order()` which already has access to `self._clob` and `self._dry_run`.

Strategies continue calling `ctx.executor.place_order(token_id, side, ...)` exactly as before — no signature change from their perspective.

---

## 2. Dry-Run Mode

### Single flag: `DRY_RUN=true` (default: `true`)

Safe by default. Must explicitly set `DRY_RUN=false` for live trading.

### Behavior Matrix

| Component | Live (`DRY_RUN=false`) | Dry Run (`DRY_RUN=true`) |
|-----------|----------------------|--------------------------|
| Market scanning | Real API | Real API |
| LLM analysis | Real calls | Real calls |
| Risk checks | Real checks | Real checks |
| Position sizing | Real sizing | Real sizing |
| DB trade record | `status='open'` | `status='dry_run'` |
| CLOB order submission | `clob.submit_order()` | Skipped, logged |
| Fill monitoring | Polls CLOB | Skipped (dry_run trades have no clob_order_id) |
| Resolution | Real P&L, bankroll from wallet | Simulated P&L, simulated bankroll |
| Bankroll update | Wallet sync | Simulated: deduct on entry, add back ± pnl on resolution |
| Email subject | `[POLYBOT] ...` | `[POLYBOT DRY RUN] ...` |

### Implementation

The dry-run check lives in exactly one place: `OrderExecutor.place_order()`. The `dry_run` flag is set on the executor at construction time (from `settings.dry_run`), not passed per-call. Strategies call `ctx.executor.place_order()` with the same signature regardless of mode. All strategy logic runs identically in both modes.

### Dry-Run Bankroll Simulation

In dry-run mode, the executor still updates `system_state.total_deployed` on trade placement (capital is "locked"). On dry-run resolution, `total_deployed` is decremented and bankroll is adjusted by simulated P&L:

```python
# In resolution monitor, for dry_run trades:
await self._db.execute(
    """UPDATE system_state SET
       bankroll = bankroll + $1,
       total_deployed = total_deployed - $2,
       daily_pnl = daily_pnl + $1
       WHERE id = 1""",
    pnl, trade["position_size_usd"])
```

### Email Notifier Change

`EmailNotifier.send()` accepts an optional `dry_run` flag (read from settings during construction). When true, prepends `[DRY RUN]` after `[POLYBOT]` in the subject line. Implemented once in the notifier, not in every caller.

---

## 3. Fill Monitoring

### New engine coroutine: `_fill_monitor()`

Runs every 30 seconds. Polls `clob.get_order_status()` for all trades with `status='open'` and `clob_order_id IS NOT NULL`.

### State transitions per trade:

| CLOB Status | Action |
|-------------|--------|
| `matched` | Set trade `status='filled'`. Sync bankroll from wallet via `clob.get_balance()`. Log + email. |
| `cancelled` | Set trade `status='cancelled'`. Decrement `total_deployed`. Log. |
| `live` + elapsed > timeout | Call `clob.cancel_order()`. Set `status='cancelled'`. Decrement `total_deployed`. Log. |
| `live` + within timeout | No action, check again next cycle. |
| API error | Log warning, retry next cycle. |

### Timeout values

Read from the trade's `strategy` column:
- `strategy='arbitrage'` → `settings.arb_fill_timeout_seconds` (30s)
- All others → `settings.fill_timeout_seconds` (120s)

### Bankroll sync on fill

When a trade transitions to `'filled'`, the fill monitor calls `clob.get_balance()` and writes the result to `system_state.bankroll`. This is the sole sync point for live bankroll tracking (besides startup and position close).

### Engine wiring

```python
# In Engine.run_forever():
if not self._settings.dry_run:
    tasks.append(self._run_periodic(self._fill_monitor, 30))
```

Skipped entirely in dry-run mode (no CLOB orders to monitor).

---

## 4. Market Resolution Polling

### New engine coroutine: `_resolution_monitor()`

Runs every 60 seconds. Checks if any filled or dry-run positions have resolved.

### Flow

```
Fetch trades WHERE status IN ('filled', 'dry_run')
    |
    For each trade:
    |  Fetch market.resolution_time
    |
    ├── resolution_time > now → skip
    │
    └── resolution_time <= now
        → scanner.fetch_market_resolution(polymarket_id)
        |
        ├── Returns 1 (YES) or 0 (NO)
        │   |
        │   ├── Filled trade:
        │   │   → recorder.record_resolution(trade_id, outcome)
        │   │   → Sync bankroll from wallet
        │   │   → Decrement total_deployed
        │   │
        │   └── Dry-run trade:
        │       → Compute simulated P&L
        │       → Update trade: status='dry_run_resolved', pnl=simulated
        │       → Adjust simulated bankroll
        │
        └── Returns None → skip, retry next cycle
```

### Relationship to existing reconciliation

`_reconcile_on_startup()` handles the cold-start case (positions that resolved while the bot was down). `_resolution_monitor()` handles the steady-state case (positions resolving while the bot is running). Same underlying logic, different triggers. Both stay.

### Engine wiring

```python
tasks.append(self._run_periodic(self._resolution_monitor, 60))
```

Runs in both live and dry-run modes (dry-run needs it for simulated P&L).

---

## 5. Bankroll Sync & Deployed Capital

### Sync points (live mode only)

| Event | Source | Action |
|-------|--------|--------|
| Startup | `clob.get_balance()` | Write to `system_state.bankroll` |
| Fill confirmed | `clob.get_balance()` | Write to `system_state.bankroll` |
| Position resolved/closed | `clob.get_balance()` | Write to `system_state.bankroll` |
| Order cancelled/timed out | — | Decrement `total_deployed` (no wallet change) |

### Deployed capital tracking fixes

Currently `total_deployed` is incremented on trade placement but never decremented. Three decrement points:

1. **Position resolved/closed** — `total_deployed -= position_size_usd`
2. **Order cancelled (timeout/external)** — `total_deployed -= position_size_usd`
3. **Startup reconciliation** — if a trade resolved during downtime, decrement

### Daily P&L reset

`system_state.daily_pnl` resets to 0 at midnight UTC, executed inside `_maybe_self_assess()` after the daily report is sent:

```python
await self._db.execute("UPDATE system_state SET daily_pnl = 0 WHERE id = 1")
```

### Dry-run bankroll simulation

In dry-run mode:
- Trade placed → deduct `position_size_usd` from bankroll, increment `total_deployed`
- Trade resolved → compute simulated P&L, adjust bankroll by `pnl`, decrement `total_deployed`
- No wallet RPC calls

---

## 6. Configuration & Schema Changes

### Schema additions

```sql
-- v2.1: CLOB order tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS clob_order_id TEXT;

-- v2.1: Expand trade status for dry-run and fill tracking
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_status_check;
ALTER TABLE trades ADD CONSTRAINT trades_status_check
    CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'closed',
                      'dry_run', 'dry_run_resolved'));

CREATE INDEX IF NOT EXISTS idx_trades_clob_order_id ON trades(clob_order_id);
```

### New settings

```python
# CLOB L2 credentials (pre-derived via scripts/derive_creds.py)
polymarket_api_secret: str
polymarket_api_passphrase: str
polymarket_chain_id: int = 137  # Polygon mainnet

# Dry-run mode
dry_run: bool = True  # Safe default
```

### Updated .env.example

Add:
```bash
# Polymarket CLOB L2 Credentials (run: uv run python scripts/derive_creds.py)
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

# Dry-run mode (set to false for live trading)
DRY_RUN=true
```

### New files

| File | Purpose |
|------|---------|
| `polybot/trading/clob.py` | ClobGateway async wrapper |
| `scripts/derive_creds.py` | One-time credential derivation script |
| `tests/test_clob.py` | ClobGateway unit tests |
| `tests/test_fill_monitor.py` | Fill polling, timeout, cancel tests |
| `tests/test_resolution_monitor.py` | Resolution detection, dry-run resolution tests |
| `tests/test_dry_run.py` | Dry-run mode integration tests |

### Modified files

| File | Change |
|------|--------|
| `polybot/strategies/base.py` | Add `clob` field to TradingContext |
| `polybot/trading/executor.py` | Wire CLOB submission, dry-run status, clob_order_id storage |
| `polybot/trading/wallet.py` | Remove `sign_order()` stub |
| `polybot/core/engine.py` | Add `_fill_monitor()`, `_resolution_monitor()`, daily_pnl reset, startup bankroll sync |
| `polybot/core/config.py` | New settings (4 fields) |
| `polybot/notifications/email.py` | Dry-run subject prefix |
| `polybot/dashboard/app.py` | Label dry_run trades |
| `polybot/__main__.py` | Construct ClobGateway, inject into context |
| `polybot/db/schema.sql` | New column, expanded constraint, new index |
| `.env.example` | New env vars |

### Setup Script: `scripts/derive_creds.py`

One-time utility to derive CLOB L2 credentials from private key:

```python
#!/usr/bin/env python3
"""Derive Polymarket CLOB API credentials from your private key.

Usage:
    uv run python scripts/derive_creds.py

Reads POLYMARKET_PRIVATE_KEY from .env and prints credentials to add to .env.
"""
from py_clob_client.client import ClobClient
from dotenv import load_dotenv
import os

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=os.environ["POLYMARKET_PRIVATE_KEY"],
)

creds = client.create_or_derive_api_creds()
print(f"\nAdd these to your .env file:\n")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
```

Requires `python-dotenv` (add to dev dependencies).

---

## Go-Live Phases

### Phase 1: Deploy Dry Run (Day 1)
- Deploy to VPS with `DRY_RUN=true`
- All API keys configured, CLOB creds derived
- Bot scans, analyzes, sizes, records dry_run trades
- Monitor daily report emails for 24-48h
- Validate: arb opportunities found? snipe candidates? LLM costs on target?

### Phase 2: Micro Live (Day 3-5)
- Fund wallet with $20-50 USDC
- Set `DRY_RUN=false`, `STARTING_BANKROLL=20`
- Monitor fills, resolution, bankroll sync
- Watch for 3-5 days

### Phase 3: Scale Up (Day 8-14)
- If profitable: increase to $100
- Monitor 1-2 weeks
- Review per-strategy P&L in daily reports
