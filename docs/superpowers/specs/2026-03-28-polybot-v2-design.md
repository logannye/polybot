# Polybot v2 — Ultimate Trading Bot Design

**Date:** 2026-03-28
**Status:** Draft
**Scope:** Full system redesign for maximum autonomous profitability

## Overview

Redesign Polybot from a single-strategy ensemble forecasting bot into a multi-strategy autonomous trading system. Three concurrent strategies — arbitrage scanning, resolution sniping, and ensemble forecasting — run at independent frequencies within a single async process. The system targets aggressive compounding of a $100 bankroll through mathematically provable edge (arb), near-certain edge (snipe), and analytical edge (forecast), with strategy-specific Kelly sizing, fee-adjusted edge calculation, and comprehensive self-healing monitoring.

## Design Constraints

Decided during brainstorming:

- **Single process, single event loop** — all strategies as concurrent `asyncio` coroutines
- **No event-driven news feeds** — focus on arb + snipe + improved ensemble; add news later
- **No market making** — 100% capital available for high-edge strategies
- **Aggressive LLM cost cuts** — target <$2/day; LLMs only for final 2-3 forecast candidates
- **Aggressive Kelly on certain trades** — arb 0.80x, snipe 0.50x, forecast 0.25x
- **Email-only notifications** — all alerts to logan@galenhealth.org via Resend; no Twilio/SMS
- **Full scope** — all improvements in one implementation pass

---

## 1. Multi-Loop Engine Architecture

### Strategy Runner Framework

The current monolithic `Engine.run_cycle()` is replaced by a `Strategy` abstract base class. Each strategy is a self-contained coroutine with its own scan interval, analysis logic, sizing parameters, and risk profile.

**`polybot/strategies/base.py`:**

```python
class Strategy(ABC):
    name: str                    # 'arbitrage', 'snipe', 'forecast'
    interval_seconds: float      # seconds between cycles
    kelly_multiplier: float      # strategy-specific sizing aggression
    max_single_pct: float        # max single position as % of bankroll

    @abstractmethod
    async def run_once(self, ctx: TradingContext) -> list[TradeProposal]: ...
```

**`TradingContext`** — shared resources passed to all strategies:
- `db` — asyncpg connection pool
- `scanner` — Polymarket API client
- `risk_manager` — unified risk checks
- `portfolio_lock` — `asyncio.Lock` protecting bankroll reads/writes
- `executor` — order execution
- `email_notifier` — email alerts
- `settings` — configuration

### Bankroll Contention Protocol

Strategies run their expensive work (API calls, LLM analysis) fully concurrently. They only serialize at the moment of execution:

1. Strategy computes desired trade size (no lock held)
2. Acquires `portfolio_lock`
3. Reads current bankroll/deployed state from DB
4. Runs risk check against live state
5. If approved: writes trade to DB, updates deployed capital
6. Releases lock
7. Places the order

Lock is held ~5-10ms (DB read-check-write only).

### Engine.run_forever()

```python
await asyncio.gather(
    self._run_strategy(ArbitrageStrategy(...)),       # 45s interval
    self._run_strategy(ResolutionSnipeStrategy(...)),  # 120s interval
    self._run_strategy(EnsembleForecastStrategy(...)), # 300s interval
    self._position_manager.run(),                      # continuous WebSocket
    self._run_periodic(self._self_assess, 86400),      # daily
    self._run_periodic(self._health_check, 60),        # every 60s
    self._dashboard_server.serve(),                    # FastAPI
)
```

### Strategy Error Isolation

Each strategy's wrapper handles errors independently:

```python
async def _run_strategy(self, strategy: Strategy):
    consecutive_errors = 0
    while True:
        try:
            await strategy.run_once(self._context)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.error("strategy_error", strategy=strategy.name,
                      error=str(e), consecutive=consecutive_errors)
            if consecutive_errors >= 5:
                log.critical("strategy_disabled", strategy=strategy.name)
                await self._context.email_notifier.send(
                    f"CRITICAL: {strategy.name} disabled",
                    f"Strategy {strategy.name} disabled after 5 consecutive errors: {e}")
                return  # kill this strategy, others continue
        await asyncio.sleep(strategy.interval_seconds)
```

### Startup Reconciliation

On startup, before strategies begin, reconcile positions that may have resolved during downtime:

```python
async def _reconcile_on_startup(self):
    open_trades = await self._db.fetch("SELECT * FROM trades WHERE status = 'open'")
    for trade in open_trades:
        market = await self._db.fetchrow(
            "SELECT * FROM markets WHERE id = $1", trade["market_id"])
        if market["resolution_time"] < datetime.now(timezone.utc):
            resolved = await self._scanner.fetch_market_resolution(
                market["polymarket_id"])
            if resolved is not None:
                await self._recorder.record_resolution(trade["id"], resolved)
```

---

## 2. Arbitrage Scanner

**Module: `polybot/strategies/arbitrage.py`**

Exploits mathematically provable mispricings between related markets. No LLMs, no forecasting — pure math.

### Three Arbitrage Types

**Type 1 — Exhaustive Outcome Arbitrage:**
Multiple markets cover all possible outcomes of the same event (e.g., "Who wins?" with candidates A, B, C).

- Sum of YES prices > 1.0: buy all NO outcomes. Pay `N x (1 - price_i)`, collect `(N-1) x $1`.
- Sum of YES prices < 1.0: buy all YES outcomes. Pay `sum(price_i)`, collect $1.
- Edge = `|1.0 - sum| - total_fees`

Detection: Group markets by Polymarket's `group_id` / `condition_id` parent via `fetch_grouped_markets()`.

**Type 2 — Temporal Subset Arbitrage:**
"Will X happen by June?" and "Will X happen by July?" — the July market must be >= June's price.

Detection: Heuristic string matching on market questions — identical stems with different date cutoffs. No LLM needed.

**Type 3 — Complement Arbitrage:**
YES + NO prices on a single market should sum to ~$1.00. If YES=$0.55 and NO=$0.40 (sum $0.95), buy both for guaranteed $0.05 profit.

Detection: Check `yes_price + no_price` from data already returned by `parse_market_response()`.

### Execution

- **Sizing:** 0.80x Kelly. Max single position: 40% of bankroll.
- **Interval:** 45 seconds.
- **Min net edge:** 1% after fees.
- **Multi-leg execution:** All legs placed with aggressive limit orders. If any leg fails to fill within 30s, cancel all legs.
- **Fee-aware:** `net_edge = gross_edge - (fee_rate x expected_payout)`.

### Market Relationship Storage

```sql
CREATE TABLE IF NOT EXISTS market_relationships (
    id SERIAL PRIMARY KEY,
    group_id TEXT NOT NULL,
    market_id INT NOT NULL REFERENCES markets(id),
    relationship_type TEXT NOT NULL
        CHECK (relationship_type IN ('exhaustive_group', 'temporal_subset', 'complement')),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(group_id, market_id)
);
```

---

## 3. Resolution Sniping

**Module: `polybot/strategies/snipe.py`**

Targets markets within 1-6 hours of resolution where the outcome is effectively determined but price hasn't converged to $0.00 or $1.00.

### Analysis Tiers

**Tier 0 (no LLM):** Markets at $0.92-$0.98 (or $0.02-$0.08 for NO). Price itself signals near-certainty. Buy and collect the remaining convergence. This captures most snipe value.

**Tier 1 (single Gemini Flash):** Markets at $0.80-$0.92 within 3h of resolution. Single cheap LLM call with a focused prompt:

```
You are verifying whether a prediction market's outcome is already determined.

Question: {question}
Resolves at: {resolution_time} ({hours_remaining}h from now)
Current YES price: {price}

Is the outcome of this question ALREADY DETERMINED based on events
that have occurred? Answer ONLY with JSON:
{"determined": true/false, "outcome": "YES"/"NO"/"UNKNOWN",
 "confidence": 0.0-1.0, "reason": "..."}
```

Cost: ~$0.001 per call. Can screen 50 markets for $0.05.

### Risk Filters

Skip markets where:
- Outcome depends on a future event that hasn't happened yet
- Price has been volatile in the last hour (>5% swings = contested outcome)
- Book depth < $200

### Execution

- **Sizing:** 0.50x Kelly. Max single position: 25% of bankroll.
- **Interval:** 120 seconds.
- **Min net edge:** 2% after fees.
- **Min confidence:** 0.90 (from LLM assessment or implied by price >$0.92).
- **Edge calc:** `snipe_edge = (1.0 - buy_price) - fee` for YES snipe.

---

## 4. Tiered Analysis & LLM Cost Management

### The Funnel

```
All active markets (~200-500)
    |
    v  Filter (no LLM) — resolution, liquidity, price, cooldown
~30-60 markets
    |
    v  Arb Scanner takes its cut (no LLM)
    v  Resolution Sniper takes its cut (Tier 0 no LLM, Tier 1 single Flash)
    |
    v  Pre-score (no LLM) — rank by quant signals + book depth + category perf
~5 markets
    |
    v  Quick screen (single Gemini Flash) — "Is there plausible edge?"
       Discard if model probability within 3% of market price.
~2-3 markets
    |
    v  Full ensemble (Claude + GPT-4o + Gemini) — 3-model analysis + web research
~0-2 trades
```

### Cost Budget

| Source | Volume | Unit Cost | Daily Cost |
|--------|--------|-----------|------------|
| Resolution sniper (Flash) | 20-50 calls | $0.001 | $0.02-0.05 |
| Quick screen (Flash) | 20-30 calls | $0.001 | $0.02-0.03 |
| Full ensemble (3 models) | 5-10 markets | ~$0.15 | $0.75-1.50 |
| Brave Search | 5-10 searches | included in plan | $0 |
| **Total** | | | **~$1-2/day** |

### Pre-Scoring Function

**New module: `polybot/analysis/prescore.py`**

Ranks markets using only data from the scanner — no LLM calls:

```python
def prescore(candidate, category_stats, quant_signals) -> float:
    score = 0.0
    # Price distance from 0.50 — prices near extremes have less room for edge
    score += (0.5 - abs(candidate.current_price - 0.5)) * 2.0
    # Book depth — deeper books mean more reliable prices
    score += min(candidate.book_depth / 5000, 1.0) * 1.5
    # Category historical performance
    cat_bias = compute_category_bias(category_stats.get(candidate.category))
    score += cat_bias * 1.0
    # Quant composite
    score += max(quant_signals.composite, 0) * 1.5
    return score
```

Top `prescore_top_n` (default 5) advance to Gemini Flash quick screen.

### Quick Screen

Added to `EnsembleAnalyzer.quick_screen()`. Uses Gemini Flash only with a minimal prompt (no web research):

```
Prediction market question: {question}
Current YES price: ${price}
Resolves: {resolution_time}

Estimate the true probability this resolves YES.
Return ONLY: {"probability": <float>, "reasoning": "<1 sentence>"}
```

If `|probability - market_price| < quick_screen_max_edge_gap` (default 0.03), discard. Otherwise advance to full ensemble.

---

## 5. Strategy-Aware Kelly Sizing & Fee-Adjusted Edge

### Per-Strategy Parameters

| Strategy | Kelly Mult | Max Single Position | Min Net Edge |
|----------|-----------|-------------------|-------------|
| Arbitrage | 0.80 | 40% of bankroll | 1% |
| Resolution snipe | 0.50 | 25% of bankroll | 2% |
| Ensemble forecast | 0.25 | 15% of bankroll | 5% (edge_threshold) |

### Fee-Adjusted Edge Calculation

Current: `edge = ensemble_prob - market_price`

New: `net_edge = gross_edge - (fee_rate x win_probability)`

Polymarket charges fees only on winnings, so high-probability bets pay proportionally more in fees. Example: Ensemble says 80% YES, market at 70%. Gross edge = 10%. Fee drag = 2% x 80% = 1.6%. Net edge = 8.4%.

**Updated `compute_kelly()`:**

```python
def compute_kelly(ensemble_prob, market_price, fee_rate=0.02) -> KellyResult:
    yes_edge = ensemble_prob - market_price
    no_edge = (1 - ensemble_prob) - (1 - market_price)

    if yes_edge >= no_edge and yes_edge > 0:
        side, gross_edge, buy_price, win_prob = "YES", yes_edge, market_price, ensemble_prob
    elif no_edge > 0:
        side, gross_edge = "NO", no_edge
        buy_price, win_prob = 1 - market_price, 1 - ensemble_prob
    else:
        return KellyResult(side="YES", edge=0.0, odds=0.0, kelly_fraction=0.0)

    net_edge = gross_edge - (fee_rate * win_prob)
    if net_edge <= 0:
        return KellyResult(side=side, edge=0.0, odds=0.0, kelly_fraction=0.0)

    odds = (1 / buy_price) - 1
    kelly_fraction = net_edge / (1 - buy_price) if buy_price < 1.0 else 0.0
    return KellyResult(side=side, edge=net_edge, odds=odds, kelly_fraction=kelly_fraction)
```

**Updated `compute_position_size()`:**

```python
def compute_position_size(
    bankroll, kelly_fraction, kelly_mult, confidence_mult,
    max_single_pct, min_trade_size, fee_rate=0.02,
) -> float:
    if kelly_fraction <= 0:
        return 0.0
    adjusted_fraction = kelly_fraction - fee_rate
    if adjusted_fraction <= 0:
        return 0.0
    raw_size = bankroll * adjusted_fraction * kelly_mult * confidence_mult
    max_size = bankroll * max_single_pct
    size = min(raw_size, max_size)
    if size < min_trade_size:
        return 0.0
    return round(size, 2)
```

### Updated Portfolio Risk Limits

| Rule | Old | New | Rationale |
|------|-----|-----|-----------|
| Max total deployed | 50% | 70% | Aggressive growth. 30% always liquid for arb. |
| Max single position | 15% | Strategy-dependent (15/25/40%) | Arb can safely deploy more. |
| Max concurrent | 8 | 12 | More strategies = more concurrent positions. |
| Daily loss limit | 20% | 15% | Tighter breaker since sizing is more aggressive. |
| Circuit breaker pause | 12h | 6h | Resume sooner at reduced Kelly. |
| Min trade size | $2 | $1 | $2 excludes small arb opps at $100 bankroll. |

### Post-Breaker Cooldown

After a circuit breaker trips, the bot resumes after 6h but runs all strategies at 50% of their normal Kelly multiplier for 24h. After 24h with no further breaker trip, normal multipliers resume. Tracked via a `post_breaker_until` timestamp in `system_state`.

---

## 6. Improved Learning & Strategy-Level Tracking

### Strategy Performance Table

```sql
CREATE TABLE IF NOT EXISTS strategy_performance (
    id SERIAL PRIMARY KEY,
    strategy TEXT UNIQUE NOT NULL,
    total_trades INT NOT NULL DEFAULT 0,
    winning_trades INT NOT NULL DEFAULT 0,
    total_pnl NUMERIC NOT NULL DEFAULT 0,
    avg_edge NUMERIC(5,4) NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Updated after every trade resolution by `TradeRecorder`. Trades table gets a `strategy` column:

```sql
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast')) NOT NULL DEFAULT 'forecast';
```

### Enhanced Self-Assessment (Daily)

Additions to the existing daily self-assessment:

**Strategy kill switch:** If a strategy has negative total P&L over 50+ trades, set `enabled = FALSE` in `strategy_performance`. The engine checks this before launching each strategy loop. Prevents a broken strategy from bleeding money.

**Bankroll-adaptive aggression:** Checked at the start of every strategy cycle (not just daily):

| Bankroll Range | Kelly Adjustment | Max Deployed |
|---------------|-----------------|-------------|
| < $50 | All mults x 0.50 (survival mode) | 70% |
| $50 - $150 | Normal multipliers | 70% |
| $150 - $500 | Normal, loosen max deployed | 80% |
| > $500 | Reduce mults x 0.85 (preservation) | 60% |

**Category performance wiring:** `EnsembleForecastStrategy` pre-score incorporates category bias from `categories.py`. Categories with negative bias after 20+ trades get -0.5 penalty in prescoring.

**Calibration correction wiring:** After the ensemble produces a probability, look up the nearest calibration bucket and adjust:

```python
corrected_prob = raw_ensemble_prob + calibration_corrections.get(nearest_bucket, 0.0)
```

Only activates after 50+ resolved forecast trades. Stored in `system_state.calibration_corrections` (column already exists).

### Model Trust Weight Improvements

- **Faster adaptation:** Brier EMA alpha increased from 0.10 to 0.15.
- **Cold start enforcement:** For the first 30 trades, use equal weights (0.333) regardless of Brier updates. The `cold_start_trades` setting exists but isn't currently wired into `recorder.py` — this connects it.

---

## 7. Self-Healing Monitoring & Notifications

### Health Monitor (60s interval)

| Check | Condition | Action |
|-------|-----------|--------|
| Heartbeat | No strategy completed a cycle in 10 min | Email warning. If 30 min, restart strategy loops. |
| API health | Polymarket CLOB 3+ consecutive errors | Pause all strategies 5 min, retry. Email if still failing after 3 pauses. |
| Balance reconciliation | DB bankroll vs wallet USDC diverge >5% | Email warning. Don't auto-correct. |
| Stale positions | Open trade open >80% of market's resolution time | Log warning. Force REST price check if within 1h of resolution. |
| Process memory | RSS > 512MB | Log warning (informational). |

### Email Notification Tiers

All notifications to `logan@galenhealth.org` via Resend. No SMS.

| Priority | When | Subject |
|----------|------|---------|
| Critical | Strategy disabled, balance mismatch, unresponsive 30+ min | `[POLYBOT CRITICAL] {event}` |
| Warning | Circuit breaker, sustained API errors, stale position | `[POLYBOT WARNING] {event}` |
| Trade | Trade executed, trade resolved | `[POLYBOT] Trade {event}: {question}` |
| Daily | End-of-day summary at 00:00 UTC | `[POLYBOT] Daily Report — {date}` |

### End-of-Day Report

Generated as part of daily self-assessment. Sent at midnight UTC.

```
POLYBOT DAILY REPORT — 2026-03-28

BANKROLL
  Starting:     $104.52
  Ending:       $108.71
  Day P&L:      +$4.19 (+4.0%)

STRATEGY BREAKDOWN
  Arbitrage:    3 trades, +$1.82  (3W / 0L)
  Snipe:        5 trades, +$2.45  (5W / 0L)
  Forecast:     2 trades, -$0.08  (1W / 1L)

CUMULATIVE (since launch)
  Total trades: 47
  Win rate:     78.7%
  Total P&L:    +$8.71
  Days running: 4

MODEL PERFORMANCE
  claude-sonnet-4.6:  Brier 0.182, trust 0.41
  gpt-4o:             Brier 0.211, trust 0.31
  gemini-2.5-flash:   Brier 0.198, trust 0.28

OPEN POSITIONS (3)
  - "Will X happen?" — YES @ $0.62, $5.20 deployed
  - "Will Y happen?" — NO  @ $0.31, $3.10 deployed
  - [arb] Group Z — 2-leg, $8.00 deployed

SYSTEM HEALTH
  Uptime:       24h 0m
  API errors:   2 (recovered)
  Strategies:   all active
  LLM cost:     $1.43
```

Implemented as `format_daily_report()` in `polybot/notifications/email.py`.

### Twilio Removal

- Delete `polybot/notifications/sms.py`
- Remove from `Settings`: `twilio_account_sid`, `twilio_auth_token`, `twilio_from_number`, `alert_phone`
- Remove from `pyproject.toml`: `twilio>=9.0`
- Remove from `.env.example`: 4 Twilio variables
- Remove from `__main__.py`: SMS notifier initialization

---

## 8. Configuration & Schema Changes

### New Module Structure

```
polybot/
├── core/
│   ├── config.py          # MODIFIED — new settings, remove Twilio
│   └── engine.py          # REWRITTEN — thin orchestrator, strategy runner
├── strategies/            # NEW PACKAGE
│   ├── __init__.py
│   ├── base.py            # Strategy ABC, TradingContext, Opportunity types
│   ├── arbitrage.py       # ArbitrageStrategy
│   ├── snipe.py           # ResolutionSnipeStrategy
│   └── forecast.py        # EnsembleForecastStrategy (extracted from engine.py)
├── markets/
│   ├── scanner.py         # MODIFIED — add fetch_market_resolution(), fetch_grouped_markets()
│   ├── filters.py         # UNCHANGED
│   └── websocket.py       # UNCHANGED
├── analysis/
│   ├── ensemble.py        # MODIFIED — add quick_screen() method
│   ├── quant.py           # UNCHANGED
│   ├── research.py        # UNCHANGED
│   ├── prompts.py         # MODIFIED — add snipe prompt, quick screen prompt
│   └── prescore.py        # NEW — pre-LLM scoring function
├── trading/
│   ├── kelly.py           # MODIFIED — fee-adjusted edge, strategy-aware sizing
│   ├── risk.py            # MODIFIED — strategy-aware limits, post-breaker cooldown
│   ├── executor.py        # MODIFIED — multi-leg arb execution, strategy column
│   └── wallet.py          # UNCHANGED
├── learning/
│   ├── calibration.py     # UNCHANGED (wired up by recorder/self_assess)
│   ├── categories.py      # UNCHANGED (wired up by forecast strategy)
│   ├── recorder.py        # MODIFIED — strategy-level recording, wire calibration
│   └── self_assess.py     # MODIFIED — strategy kill switch, bankroll tiers, daily report
├── notifications/
│   ├── email.py           # MODIFIED — daily report, all notification tiers
│   └── sms.py             # DELETED
├── dashboard/
│   └── app.py             # MODIFIED — add /strategies and /arb endpoints
├── db/
│   ├── schema.sql         # MODIFIED — new tables and columns
│   └── connection.py      # UNCHANGED
├── __init__.py
└── __main__.py            # MODIFIED — remove Twilio, new strategy initialization
```

### Schema Additions

```sql
-- Strategy column on trades
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast')) NOT NULL DEFAULT 'forecast';

-- Strategy performance tracking
CREATE TABLE IF NOT EXISTS strategy_performance (
    id SERIAL PRIMARY KEY,
    strategy TEXT UNIQUE NOT NULL,
    total_trades INT NOT NULL DEFAULT 0,
    winning_trades INT NOT NULL DEFAULT 0,
    total_pnl NUMERIC NOT NULL DEFAULT 0,
    avg_edge NUMERIC(5,4) NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO strategy_performance (strategy) VALUES
    ('arbitrage'), ('snipe'), ('forecast')
ON CONFLICT (strategy) DO NOTHING;

-- Market relationships for arbitrage detection
CREATE TABLE IF NOT EXISTS market_relationships (
    id SERIAL PRIMARY KEY,
    group_id TEXT NOT NULL,
    market_id INT NOT NULL REFERENCES markets(id),
    relationship_type TEXT NOT NULL
        CHECK (relationship_type IN ('exhaustive_group', 'temporal_subset', 'complement')),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(group_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_market_relationships_group ON market_relationships(group_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);

-- Post-breaker cooldown tracking
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS post_breaker_until TIMESTAMPTZ;
```

### Settings Changes

**Removed:**
- `twilio_account_sid`, `twilio_auth_token`, `twilio_from_number`, `alert_phone`

**Added:**
```python
# Strategy intervals
arb_interval_seconds: int = 45
snipe_interval_seconds: int = 120
forecast_interval_seconds: int = 300

# Strategy Kelly multipliers
arb_kelly_mult: float = 0.80
snipe_kelly_mult: float = 0.50
forecast_kelly_mult: float = 0.25

# Strategy position limits
arb_max_single_pct: float = 0.40
snipe_max_single_pct: float = 0.25
forecast_max_single_pct: float = 0.15

# Fee
polymarket_fee_rate: float = 0.02

# Snipe thresholds
snipe_hours_max: float = 6.0
snipe_min_confidence: float = 0.90
snipe_min_net_edge: float = 0.02

# Arb thresholds
arb_min_net_edge: float = 0.01
arb_fill_timeout_seconds: int = 30

# Pre-scoring
prescore_top_n: int = 5
quick_screen_max_edge_gap: float = 0.03

# Risk — updated defaults
max_total_deployed_pct: float = 0.70
max_concurrent_positions: int = 12
daily_loss_limit_pct: float = 0.15
circuit_breaker_hours: int = 6
post_breaker_cooldown_hours: int = 24
post_breaker_kelly_reduction: float = 0.50
min_trade_size: float = 1.0

# Bankroll tiers
bankroll_survival_threshold: float = 50.0
bankroll_normal_low: float = 50.0
bankroll_normal_high: float = 150.0
bankroll_growth_threshold: float = 500.0

# Learning
brier_ema_alpha: float = 0.15
calibration_min_trades: int = 50
strategy_kill_min_trades: int = 50

# Monitoring
health_check_interval: int = 60
heartbeat_warn_seconds: int = 600
heartbeat_critical_seconds: int = 1800
balance_divergence_pct: float = 0.05

# Notifications
alert_email: str = "logan@galenhealth.org"
```

### Dependency Changes

**pyproject.toml:**
- Remove: `twilio>=9.0`
- No new dependencies added.

### .env.example Updates

Remove:
```
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
ALERT_PHONE=+14405638928
```

Update:
```
ALERT_EMAIL=logan@galenhealth.org
```
