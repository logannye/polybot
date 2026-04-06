# Volatility Harvester Design Spec

## Core Thesis

Every dollar-minute deployed should earn the maximum possible return. Production data (7 days, 329 trades) shows exactly one pattern with positive expectancy: **MR trades that resolve in under 30 minutes** — 16 trades, +$26.28, 56% win rate. Trades held over 30 min: -$7.93, 38% win rate. The signal is unambiguous.

This spec tightens the entire system around the fast MR cycle: detect overreaction → enter → exit in 3-60 min → redeploy capital.

## Changes

### 1. MR Time-Stop: 3h → 1h

**Config:** `MR_MAX_HOLD_HOURS=1.0` (currently 3.0)

Production data:
- Under 1h holds: 22 trades, 13 wins, +$31.68
- Over 1h holds: 7 trades, 1 win, -$13.33

The 1h cutoff preserves all but one winner while eliminating 6 of 7 losers.

### 2. MR Max Single Position: 15% → 20%

**Config:** `MR_MAX_SINGLE_PCT=0.20` (currently 0.15 in .env, config default)

At $494 bankroll, this raises the position cap from $74 to $99. The biggest MR winner (#488) was $34 → +$22.54. At $99, that same trade would have returned +$65. The math supports this: 64% win rate with 2.3x win/loss ratio gives Kelly-optimal fraction of ~0.37 (37% of bankroll per bet). We're still well below Kelly-optimal at 20%.

### 3. MR Cooldown: 3h → 30min

**Config:** `MR_COOLDOWN_HOURS=0.5` (currently 3.0)

The Cooper Flagg market produced +$24.14 across 2 trades on the same market. With a 30min cooldown, the bot can catch multiple overreaction waves during a volatile session (e.g., NBA game night where the same market swings repeatedly as the game progresses).

### 4. Disable Market Maker

**Config:** `MM_ENABLED=false` (currently true)

Market maker has 0 real trades. Simulated cumulative PnL is +$0.33 after 700+ fills. It consumes engine cycles (5s heartbeat loop) and has no path to meaningful profit at $500 bankroll scale.

### 5. Snipe Time-Stop: 48h → 6h

**Config:** `SNIPE_MAX_HOLD_HOURS=6.0` (currently 48.0 in config default)

The two open snipe positions ($215) have been sitting since yesterday. 6h time-stop frees stale capital within hours. Snipe that hasn't converged in 6h is likely stuck waiting for resolution — better to redeploy that capital into MR at 7% ROI.

### 6. Cross-Venue: Short-Dated Events Only (≤7 days)

**Code change** in `polybot/strategies/cross_venue.py`: After matching a Polymarket market, check its `resolution_time`. Skip if resolution is more than 7 days away. This prevents capital from being locked in long-dated futures (e.g., NBA Finals at $0.001 resolving June 30).

Add config key `cv_max_days_to_resolution: float = 7.0` for hot-reload flexibility.

## What's NOT Changing

- MR trigger threshold (10%) — working well
- MR Kelly boost (1.6x on >15% moves) — working well
- MR reversion fraction (40%) — the exit target math is sound
- Snipe core logic — it's fine, just needs tighter capital management
- Cross-venue core logic — works, just needs time filtering
- Forecast — already disabled

## Expected Outcomes

With $494 bankroll, 3-5 MR trades/day at $50-100 positions, 56% win rate, 2:1 win/loss ratio:
- **Expected daily PnL: $10-25/day**
- **Capital velocity: 2-5 complete cycles/day** (enter → exit → redeploy)
- **Max capital lockup: 1h** (MR time-stop) or 6h (snipe)
