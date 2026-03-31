# Snipe Factory — 48-Hour Profit Maximization Strategy

**Date:** 2026-03-31
**Status:** Approved
**Goal:** Maximize short-term returns (24-48h) on a $500 bankroll by fixing capital leaks, supercharging the proven snipe strategy, rehabilitating forecast, and removing capital drag from arbitrage.

## Context

A 22.6-hour dry run produced 188 trades with +$3.42 net P&L on $10,114 total deployed — a 0.034% return on capital. The audit identified five specific problems:

1. **Snipe churn:** 140 of 151 resolved snipe trades had zero P&L — entering and exiting the same market at the same price repeatedly. 75% of all snipe trades went into a single market (Arctic sea ice).
2. **Forecast stop-losses destroying gains:** 44% win rate with nearly symmetric TP/SL (20%/25%) nets negative. Trades held >6h averaged -$3.69.
3. **Arb capital lockup:** 14 positions, $124 deployed, zero resolved. Resolution dates range Apr–Dec. 25% of bankroll earning nothing.
4. **Capital underutilization:** 67% of bankroll sitting idle.
5. **Repeated losing entries:** Bot re-entered the same thesis (treasury yield, USD.AI) after stop-losses without updating beliefs.

## Risk Profile

Full-send compound mode for 48 hours:
- Circuit breaker disabled (daily_loss_limit_pct → 1.0)
- Capital utilization raised to 90%
- No arb capital lockup on new positions
- Aggressive position sizing on high-edge snipes
- Worst-case scenario: a coordinated bad streak across multiple markets could draw down 30-40% before the bot self-corrects via reduced Kelly at lower bankroll levels

## Design

### 1. Snipe Overhaul

#### 1a. Per-Market Cooldown

**Problem:** Snipe re-enters the same market every 2-minute cycle if the price still passes the tier threshold. 114 of 151 snipe trades went into Arctic sea ice at $0.9445 — zero edge, zero P&L.

**Fix:** After any snipe trade closes on a market, block re-entry for `snipe_cooldown_hours` (default: 4). Implementation:

- In-memory dict `_market_cooldowns: dict[str, CooldownEntry]` in `ResolutionSnipeStrategy`
- `CooldownEntry = {exit_time: datetime, exit_price: float, entries_24h: int}`
- Populated from DB on first `run_once()` (query recent closed snipe trades)
- Updated by engine when snipe trades close (new `on_trade_closed` callback, or simpler: check closed trades at start of each `run_once()` cycle)
- Checked before entering: if `polymarket_id` in cooldowns and `now - exit_time < cooldown_hours`, skip

**Simpler approach chosen:** Rather than a callback, at the top of each `run_once()` cycle, query recently closed snipe trades and refresh the cooldown dict. This adds one lightweight DB query per 2-minute cycle but avoids wiring callbacks through the engine.

```python
# At top of run_once(), refresh cooldowns from DB
recent_exits = await ctx.db.fetch(
    """SELECT m.polymarket_id, t.closed_at, t.exit_price
       FROM trades t JOIN markets m ON t.market_id = m.id
       WHERE t.strategy = 'snipe'
         AND t.status = 'dry_run_resolved'
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

Then before entering a market:
```python
if pid in self._market_cooldowns:
    elapsed = (now - self._market_cooldowns[pid]["exit_time"]).total_seconds() / 3600
    if elapsed < self._cooldown_hours:
        # Check re-entry exception
        price_delta = abs(current_price - self._market_cooldowns[pid]["exit_price"])
        if price_delta < self._reentry_threshold:
            continue  # Still in cooldown, no significant price move
        # Price moved enough — allow re-entry (counted below)
```

**Note on status filter:** The cooldown query must also include `'closed'` status for live mode (not just `'dry_run_resolved'`). Use `status IN ('dry_run_resolved', 'closed')`.

#### 1b. Tiered Edge Sizing

**Problem:** Flat Kelly multiplier treats a 2.1% edge snipe the same as a 5%+ edge snipe. The 7 winning trades had higher edge and should have been sized larger.

**Fix:** Multiply the effective Kelly by an edge-dependent factor:

```python
# After computing net_edge and kelly_adj
if net_edge >= 0.05:
    kelly_adj *= 2.0
elif net_edge >= 0.03:
    kelly_adj *= 1.5
# else: base kelly (1.0x)
```

3 lines in `snipe.py`, inserted after the existing `tier_kelly_scale` multiplication.

#### 1c. Wider Hunting Window

Config change only: `snipe_hours_max`: 72 → 120.

Allows Tier 2 snipes on markets 3-5 days from resolution. The position manager's early exit system handles capital recycling — positions don't need to be held to resolution.

#### 1d. Snipe Re-Entry on Price Movement

**Problem:** Good snipe targets that move further in our direction after exit represent a second opportunity. The treasury yield trades showed this pattern — multiple profitable entries on the same market as price converged.

**Fix:** The cooldown check (1a above) includes a re-entry exception: if the market price has moved ≥ `snipe_reentry_threshold` (default: 0.03) since our exit, bypass the cooldown. Track 24h entry count per market and cap at `snipe_max_entries_per_market` (default: 3) to prevent loops.

```python
# Count entries in last 24h for this market
entries_24h = await ctx.db.fetchval(
    """SELECT COUNT(*) FROM trades t JOIN markets m ON t.market_id = m.id
       WHERE m.polymarket_id = $1 AND t.strategy = 'snipe'
         AND t.opened_at > NOW() - INTERVAL '24 hours'""", pid)
if entries_24h >= self._max_entries_per_market:
    continue  # Hard cap
```

### 2. Forecast Rehabilitation

#### 2a. Asymmetric Exits

Config change: `take_profit_threshold`: 0.20 → 0.30.

With the observed 44% win rate, this shifts reward:risk from 0.8:1 to 1.2:1 — crossing the breakeven threshold. Stop-loss stays at 25%.

#### 2b. 2-Hour Time-Stop

**Problem:** Forecast trades held >6h averaged -$3.69. Short holds (<1h) averaged +$1.18. Capital locked in stale forecast positions isn't recycling through profitable snipe trades.

**Fix:** In `ActivePositionManager.check_positions()`, add a time-based exit for forecast trades:

```python
# After existing take-profit/stop-loss checks, before edge-erosion:
if trade["strategy"] == "forecast":
    hold_minutes = (now - trade["opened_at"]).total_seconds() / 60
    if hold_minutes > self._forecast_time_stop_minutes:
        # Exit at current market price
        await self._exit_position(trade, current_price, "time_stop")
        continue
```

New `exit_reason` value `'time_stop'` added to the trades table CHECK constraint.

New config key: `forecast_time_stop_minutes` (default: 120).

#### 2c. Market Loss Blacklist

**Problem:** Bot entered treasury yield 7 times (-$0.30 net), repeatedly betting the same thesis after stop-losses. USD.AI entered twice (-$3.89 total).

**Fix:** In-memory dict in `EnsembleForecastStrategy`:

```python
_loss_blacklist: dict[str, list[datetime]]  # {polymarket_id: [stop_loss_times]}
```

Refreshed from DB at the start of each `run_once()` cycle (same pattern as snipe cooldown):

```python
recent_losses = await ctx.db.fetch(
    """SELECT m.polymarket_id, t.closed_at
       FROM trades t JOIN markets m ON t.market_id = m.id
       WHERE t.strategy = 'forecast' AND t.exit_reason = 'stop_loss'
         AND t.closed_at > NOW() - INTERVAL '12 hours'""")
self._loss_blacklist = {}
for row in recent_losses:
    pid = row["polymarket_id"]
    self._loss_blacklist.setdefault(pid, []).append(row["closed_at"])
```

Before entering a market:
```python
if self._loss_blacklist.get(candidate.polymarket_id, []) and \
   len(self._loss_blacklist[candidate.polymarket_id]) >= 2:
    log.info("forecast_blacklisted", market=candidate.polymarket_id)
    return
```

2 stop-losses within 12 hours = blacklisted. Simple, no new config needed (could add config keys later if tuning is needed).

#### 2d. Faster Cycles

Config change: `forecast_interval_seconds`: 300 → 180.

### 3. Capital Allocation & Risk

#### 3a. Disable Arb Below $2K

New config key: `arb_min_bankroll` (default: 2000).

At the top of `ArbitrageStrategy.run_once()`:

```python
state = await ctx.db.fetchrow("SELECT bankroll FROM system_state WHERE id = 1")
if state and float(state["bankroll"]) < self._min_bankroll:
    log.debug("arb_bankroll_gate", bankroll=float(state["bankroll"]),
              min_required=self._min_bankroll)
    return
```

Existing open arb positions are unaffected — they stay open until resolution. No new arb capital gets locked.

#### 3b. Remove Circuit Breaker

Config change: `daily_loss_limit_pct`: 0.15 → 1.0.

The `RiskManager.check_circuit_breaker()` method already handles this — at 100% threshold, it effectively never triggers. No code change needed.

#### 3c. Raise Utilization

Config changes:
- `max_total_deployed_pct`: 0.70 → 0.90
- `max_concurrent_positions`: 12 → 20
- `snipe_max_single_pct`: 0.25 → 0.30

### 4. Schema Change

Add `'time_stop'` to the `exit_reason` CHECK constraint:

```sql
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;
ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IN ('resolution', 'early_exit', 'stop_loss', 'take_profit', 'time_stop'));
```

### 5. New Config Keys Summary

| Key | Default | Hot-reloadable | Purpose |
|-----|---------|----------------|---------|
| `arb_min_bankroll` | 2000.0 | Yes (checked each cycle) | Gate arb below this bankroll |
| `snipe_cooldown_hours` | 4.0 | Yes | Per-market re-entry cooldown |
| `snipe_reentry_threshold` | 0.03 | Yes | Price move to bypass cooldown |
| `snipe_max_entries_per_market` | 3 | Yes | Hard cap on 24h entries per market |
| `forecast_time_stop_minutes` | 120.0 | Yes | Auto-exit forecast after N minutes |

### 6. Files Modified

| File | Change | Lines |
|------|--------|-------|
| `polybot/strategies/snipe.py` | Cooldown dict, tiered sizing, re-entry logic | ~50 |
| `polybot/strategies/forecast.py` | Loss blacklist check | ~20 |
| `polybot/strategies/arbitrage.py` | Bankroll gate | ~8 |
| `polybot/trading/position_manager.py` | Time-stop for forecast | ~12 |
| `polybot/core/config.py` | 5 new config keys | ~8 |
| `polybot/db/schema.sql` | exit_reason constraint update | ~3 |
| `tests/test_snipe.py` | Cooldown, tiered sizing, re-entry tests | ~80 |
| `tests/test_forecast_strategy.py` | Blacklist tests | ~30 |
| `tests/test_position_manager.py` | Time-stop tests | ~25 |

### 7. Expected Impact

| Fix | Mechanism | Estimated Daily Impact |
|-----|-----------|----------------------|
| Snipe cooldown (kills churn) | Eliminates ~140 zero-value trades/day, saves ~$176 in live fees | +$5-8/day |
| Tiered edge sizing | 2x position on high-edge snipes (5%+) | +$3-5/day |
| Wider snipe window (120h) | More snipe candidates in the funnel | +$2-4/day |
| Snipe re-entry on movement | Captures second-leg convergence | +$1-3/day |
| Asymmetric forecast exits | Flips forecast from -EV to +EV | +$4-6/day |
| 2-hour time-stop | Prevents forecast capital lockup | +$3-5/day |
| Market loss blacklist | Prevents repeated losing entries | +$2-4/day |
| Disable arb <$2K | Frees $124 for active strategies | +$3-5/day |
| Raise utilization to 90% | More capital working at any time | +$2-4/day |
| **Combined** | | **$25-44/day (5-9% daily return)** |

### 8. Rollout

1. Stop LaunchAgent: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist`
2. Apply schema migration
3. Deploy code changes
4. Update config values in `.env`
5. Run tests
6. Restart: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.polybot.trader.plist`
7. Monitor first 30 minutes of logs for correct behavior

Total downtime: ~5 minutes. All open positions preserved in Postgres.
