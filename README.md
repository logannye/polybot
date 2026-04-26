# Polybot

Single-strategy autonomous trading bot for [Polymarket](https://polymarket.com), focused on one edge: **LLM-verified resolution arbitrage**.

Buy markets at ≥$0.96 (or sell ≤$0.04) when a structured Gemini Flash check confirms the underlying outcome is mechanically locked. Hold to resolution. Collect the last 3-4 cents of price drift between "everyone knows" and "the market has settled."

This is the only Polymarket strategy whose edge does not depend on tight order-book spreads — the trade has no exit transaction, so the spread tax is paid once at entry and bounded by the entry price.

> **Status**: v12 (snipe-only). Migrated 2026-04-25 from a multi-strategy architecture (live sports + pregame sharp + snipe T0/T1) after a one-week post-v10 observation window with zero closed trades and 100% executor rejections on `spread_too_wide`. See `docs/superpowers/plans/2026-04-25-v12-snipe-only-implementation.md` for the migration record.

## Why this and only this

Across the realistic Polymarket edge candidates we could build, snipe T1 is the **only one** where:

1. The edge is **verificative, not predictive.** We don't bet on "Trump probably wins this state." We verify "AP called the race at 8:14pm; market hasn't moved." The LLM does concrete work checking real-world state vs. market state.
2. The spread tax is **structurally small.** At a 0.97 entry, the per-trade max loss in cents is small even when the percentage spread is wide. There is no exit transaction.
3. **Hit rate is observable in days.** Every trade resolves in <12 hours. After 50 trades (≈1 week), we have statistically meaningful feedback. Predictive strategies need months to calibrate.
4. **Failure is bounded and detectable.** A rolling-50 hit rate <97% trips an automatic killswitch and demotes the deployment stage. The April 2026 blowup mode is structurally prevented.

We deleted 3,400 lines of strategy code (live sports, pregame sharp, snipe T0, sport-specific WP models, calibrators, kelly-scaler) to focus exclusively on this one edge. The codebase is now ~4,100 LOC.

## How it works

Six modules, one async process, one strategy:

```
[Scanner] → [Filter] → [LLM Verifier] → [Sizer] → [Maker Executor] → [Resolution Logger]
   30s        sync        Gemini         Kelly      post_only         on resolution
                                                                              ↓
                                                                  [Hit-Rate Killswitch]
```

### 1. Scanner — `polybot/markets/scanner.py`

Polls Polymarket Gamma `/events` every 30 seconds. Keeps markets where:
- `time_to_resolution ∈ [5 minutes, 12 hours]`
- `yes_price ≥ 0.96` (buy YES) **or** `yes_price ≤ 0.04` (buy NO via mirror)

The 5-minute lower bound prevents fills that won't clear before resolution. The 12-hour upper bound matches the spec's edge thesis — past 12 hours, news drift dominates the lock.

### 2. Filter — `polybot/markets/filters.py`

Rejects candidates where:
- We already have a position in this market
- Book depth on the entry side < $1,000
- Daily Gemini spend cap is exhausted

### 3. LLM Verifier — `polybot/analysis/gemini_client.py`

Sends each surviving candidate to Gemini 2.5 Flash with a structured response schema:

```json
{
  "verified": true,
  "reason": "AP called the race for X at 8:14pm ET; concession speech delivered.",
  "confidence": 0.99
}
```

Rejects if:
- `confidence < 0.95`
- `reason` length < 30 characters
- `reason` matches hand-wavy regex (`/seems|likely|probably|possibly/i`) without concrete grounding (a date, name, score, or other verifiable fact)

The grounding requirement is the difference between v10 T1 and a structurally negative-EV trade. The LLM must articulate *why* the market is locked, not just feel that it is.

### 4. Sizer — `polybot/trading/kelly.py`

```
fraction = ((p × b) - q) / b
size_usd = bankroll × min(fraction × 0.25, 0.05)
```

Where `p` = verifier-implied probability, `b` = (1 - buy_price) / buy_price, `q` = 1 - p.

Caps:
- 0.25× Kelly multiplier (quarter-Kelly)
- 5% bankroll per trade
- 20% bankroll deployed across all open snipe positions

No dynamic Kelly scaler. No edge-decay tracker. Static parameters; the killswitch is the only adaptive component.

### 5. Maker Executor — `polybot/trading/executor.py`

Posts a limit order at `min(buy_price, best_ask - 1 tick)`. `post_only=True` (never crosses the book). Cancels if unfilled in 60 seconds.

This is the inverse of crossing wide books and paying the spread tax. We ask to be filled at our fair price; if no maker comes to us, no trade.

### 6. Resolution Logger & Hit-Rate Killswitch — `polybot/learning/`

When a market resolves, the recorder writes a `trade_outcome` row including the verifier's confidence and reason. The killswitch then updates a rolling-50 hit rate gauge persisted in `system_state`. If the rate falls below 97%, the bot:
1. Halts all entries immediately
2. Demotes `live_deployment_stage` by one tier (e.g., `ramp` → `micro_test`)
3. Notifies via email
4. Persists the trip; only a manual operator action clears it

## Architecture

Single Python 3.13 async process. The engine (`polybot/core/engine.py`) registers exactly one strategy coroutine plus reconciliation, fill-monitor, resolution-monitor, and killswitch loops. Shared resources (DB, scanner, executor) are passed via a `TradingContext` dataclass.

PostgreSQL stores: `markets`, `trades`, `trade_outcome`, `shadow_signal`, `strategy_performance`, `system_state`. The `shadow_signal` table records every entry candidate — even rejected ones — with its hypothetical outcome, so we can answer counterfactual questions like "what would EV have been if the spread cap were 0.20?" without ever taking the risk.

## Layered safeguards (from v10, unchanged)

`polybot/safeguards/`:

- **DrawdownHalt** — 30-second cached check, halts on 15% peak-to-trough drawdown
- **CapitalDivergenceMonitor** — 3-OK self-heal cycle, halts if measured wallet ≠ tracked bankroll
- **DeploymentStageGate** — enforces capital cap by stage:
  - `dry_run`: 70% simulated only
  - `micro_test`: 5% real
  - `ramp`: 25% real
  - `full`: 70% real

The killswitch sits one layer above all of these and demotes the stage automatically.

## Promotion ladder

Promotion between deployment stages is **manual and evidence-gated**:

| From → To | Min closed trades | Min hit rate | Required signal |
|---|---|---|---|
| `dry_run` → `micro_test` | 30 (real or shadow) | ≥97% | Verifier reasons reviewed by operator |
| `micro_test` → `ramp` | 100 real fills | ≥97% rolling-50 | Net P&L > 0 after fees |
| `ramp` → `full` | 200 real fills | ≥97% rolling-50 | Net P&L > +5% bankroll |

Demotion is **automatic** on killswitch trip. There is no auto-promotion.

## What this bot deliberately does NOT do

- Predict outcomes of unresolved games or events
- Run live sports moneyline / totals trading
- Run pregame sharp-line arbitrage
- Maintain inventory or quote two-sided markets
- Apply per-sport calibration models
- Trade snipe T0 (price ≥0.96 with no LLM verification) — pre-v10 evidence: 6 wins / 214 trades, structurally negative EV
- Act on "edges" derived from already-locked totals where the line is settled by game state (e.g., MLB Total 15.5 in the 6th inning) — these are not alpha, they are arithmetic, and the books correctly price them as such

## Running it

```bash
# One-time setup
git clone https://github.com/logannye/polybot ~/polybot
cd ~/polybot
uv sync
cp .env.example .env  # edit with your keys
psql -d polybot -f scripts/sql/schema.sql

# Run in dry-run (default)
uv run python -m polybot

# Or launch as a LaunchAgent (macOS)
launchctl load ~/Library/LaunchAgents/ai.polybot.trader.plist
```

Required environment variables (see `.env.example`):
- `POLYMARKET_PRIVATE_KEY` — for CLOB authentication
- `GEMINI_API_KEY` — for T1 verification
- `POSTGRES_URL` — local Postgres connection
- `LIVE_DEPLOYMENT_STAGE` — `dry_run` (default), `micro_test`, `ramp`, or `full`
- `DRY_RUN` — `true` (default) for simulated fills

## Repo layout

```
polybot/
├── analysis/
│   └── gemini_client.py       # T1 verifier with structured schema
├── core/
│   ├── config.py              # Pydantic settings (~30 keys total)
│   └── engine.py              # Async loop, single strategy registration
├── db/
│   └── connection.py          # asyncpg pool
├── learning/
│   ├── hit_rate_killswitch.py # Rolling-50 kill switch
│   ├── recorder.py            # trade_outcome writer
│   ├── shadow_log.py          # Records every signal regardless of fill
│   └── trade_outcome.py       # Outcome model
├── markets/
│   ├── filters.py             # Position / depth / spend gates
│   └── scanner.py             # Gamma /events poller
├── notifications/
│   └── email.py               # Killswitch + halt alerts
├── safeguards/
│   ├── capital_divergence.py
│   ├── deployment_stage.py
│   └── drawdown_halt.py
├── strategies/
│   ├── base.py                # Strategy ABC
│   └── snipe.py               # T1 only (T0 deleted in v12)
└── trading/
    ├── clob.py                # py-clob-client wrapper
    ├── executor.py            # post_only=True maker executor
    ├── fees.py                # Polymarket fee model
    ├── kelly.py               # Static 0.25× quarter-Kelly
    ├── position_manager.py
    ├── risk.py                # Position cap enforcement
    └── wallet.py
```

## History

- **2026-03**: Initial multi-strategy bot — forecast, mean reversion, market making, cross-venue, political, arbitrage
- **2026-04-10**: Went live with $669.85
- **2026-04-14**: Lost 93% (-$623.54). Root causes: market-maker adverse selection, invisible spreads, no total-drawdown protection
- **2026-04-14**: Safeguards shipped (drawdown halt + capital divergence + preflight + deployment stages)
- **2026-04-19**: v10 rebuild — scorched-earth deletion of 6 strategies; consolidation to live_sports + snipe T0/T1; new safeguards module
- **2026-04-24**: v11 added pregame sharp-line strategy and totals model
- **2026-04-25 morning**: v11.0e — pregame soccer + per-cycle visibility logs
- **2026-04-25 evening**: One-week observation window post-v10 showed zero closed trades, 100% executor rejection on spread cap. **v12 migration begins** — see `docs/superpowers/plans/2026-04-25-v12-snipe-only-implementation.md`

## License

Private.
