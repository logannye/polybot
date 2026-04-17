# Polybot v10 — "Information Arb Specialist" Design Spec

**Date:** 2026-04-16
**Status:** Approved for implementation planning
**Supersedes:** all prior strategy configurations; kills 6 existing strategies

---

## Context

Polybot went live on 2026-04-10 with $669.85 and lost 93% ($623.54) by 2026-04-14. Root causes were untested CLOB code paths, no live DB tracking for the Market Maker strategy, thin order books with 50–98% spreads invisible in dry-run, and no total-drawdown protection. Post-mortem safeguards (drawdown halt, capital divergence monitor, preflight, deployment stage ladder, MM DB tracking, realistic dry-run) were shipped on 2026-04-14. An Apr 16 audit found 8 follow-up issues pending fixes.

v10 is a first-principles rebuild. Instead of refining seven strategies that collectively produced marginal P&L, we concentrate capital on **two** strategies with structurally defensible edges, delete the rest, and build a tight learning layer on top.

## Goals

1. Maximize risk-adjusted P&L growth on a $2,000 bankroll, compounding daily.
2. Earn from structural, latency-based, and convergence edges — not from predicting outcomes.
3. Learn from every trade via online calibration and Beta-Binomial Kelly scaling.
4. Survive live deployment: no repeat of the April wipe.

## Non-Goals

- Beating attention-weighted markets with LLM forecasts (literature and our own history say we won't).
- Liquidity provision / market making at retail scale (adverse selection eats the spread).
- Cross-venue arbitrage (external API credit fragility; most mispricings reflect Poly's 24/7 tradability, not arbitrage).
- Multi-outcome combinatorial arbitrage (execution risk at current book depth is ruinous — -$124 realized).
- Reinforcement learning meta-controllers or neural-net probability models (over-engineering at this capital).

---

## § 1 — Scope: Kill List and Keep List

### Strategies kept (2)

| Strategy | Role | Capital share |
|---|---|---|
| **Live Sports** (evolved Live Game Closer) | Primary engine — ESPN latency arbitrage | Up to 70% of deployable capital |
| **Snipe** | Capital recycler — near-certain resolution convergence | Up to 40% of deployable capital |

### Strategies deleted (6)

| Strategy | Reason |
|---|---|
| Combinatorial Arbitrage | -$124 realized; execution hell at current book depth |
| Political Calibration | Edge real on average, noisy in short horizons; slow capital |
| Ensemble Forecast | LLMs don't reliably beat markets on attention-weighted events |
| Market Maker | Live blow-up in April; adverse selection at $2K is a death sentence |
| Mean Reversion | +$100 dry-run unreliable (spread model was fake); revisit post v10 proof |
| Cross-Venue Arbitrage | Odds API credit death spiral; most mispricings are not arbitrage |

### Code deleted

- `polybot/strategies/arbitrage.py`, `political.py`, `forecast.py`, `market_maker.py`, `mean_reversion.py`, `cross_venue.py` (and tests)
- `polybot/analysis/calibration.py`, `odds_client.py`, LLM ensemble router, per-model Brier trust-weight machinery
- `polybot/trading/quote_manager.py`, `inventory.py`
- Related config keys (~60), DB tables, `.env` variables

### Safeguards preserved

Drawdown halt, capital divergence monitor, live preflight, deployment stage gate — all stay. Extracted from `engine.py` into a new `polybot/safeguards/` module and enhanced per the Apr 16 audit (self-healing divergence, cached drawdown, `get_order_book_summary` encapsulation, inventory reconciliation — the last becomes moot when MM is deleted but the `get_order_book_summary` and self-healing apply to the general system).

### Starting bankroll

`$2,000` (simulated for dry-run Stage 0, held constant through Stages 2–3 micro_test/ramp so Kelly sizing doesn't creep on unvalidated history, scaled only after 30 days of positive full-stage P&L).

---

## § 2 — System Architecture

### Unchanged

- Single Python 3.13 async process, `asyncio` event loop
- LaunchAgent `ai.polybot.trader`, PostgreSQL `polybot` DB, `py-clob-client`, `structlog`
- `TradingContext` dataclass shared across strategies; `asyncio.Lock` gating bankroll writes
- Existing safeguards layer (enhanced)

### New directory layout

```
polybot/
├── core/              # Engine, Config (pruned of dead keys)
├── strategies/        # ONLY: live_sports.py, snipe.py, base.py
├── markets/
│   ├── gamma.py       # existing
│   ├── clob.py        # existing
│   └── sports_matcher.py    # NEW
├── sports/            # NEW module
│   ├── espn_client.py
│   ├── win_prob.py           # sport-specific pure-function models
│   └── calibrator.py         # online isotonic regression
├── trading/           # Kelly, risk, executor, clob gateway — MM surface removed
├── learning/          # per-strategy brier, kelly scaler, edge decay
├── safeguards/        # NEW — extracted from engine.py
└── db/                # migrated schema (new tables: sport_calibration, trade_outcome)
```

### Key architectural decisions

1. **`sports/` is a top-level module**, not buried in `strategies/` or `analysis/`. ESPN polling, win-prob models, and the calibrator are data infrastructure consumed by the Live Sports strategy but independently testable and swappable.
2. **`sports_matcher.py` is isolated with exhaustive tests.** Matching is the failure mode most likely to silently trade the wrong market; quarantining it behind a confidence floor is deliberate over-engineering.
3. **Safeguards extracted from `engine.py`** for independent testability.
4. **Learning layer is small and per-strategy.** One Brier per strategy, one Beta-Binomial Kelly scaler per strategy, one calibration curve per sport × game-state bucket. Fits in one JSONB column on `system_state` plus two new tables.
5. **No new processes, no new languages, no new data stores.**

### Engine loop

```
engine.run():
  loop forever:
    check safeguards (drawdown, divergence, stage) → halt if any trip
    parallel: live_sports.run_once(ctx), snipe.run_once(ctx)
    every 60s:  capital_divergence_check()
    every 5min: bankroll_reconcile()
    every 1h:   learning_cycle(calibrator_refit, kelly_scaler, edge_decay)
    every 24h:  self_assessment(kill_switch, daily_report)
    every 7d:   weekly_reflection()
```

---

## § 3 — Live Sports Engine

### Edge thesis

During most live games, Polymarket prices lag the true win probability by 4%+ because retail traders don't re-price every play. ESPN's feed is public, free, and updates in ~5–15 seconds. We capture the retail-reaction lag.

### Data pipeline

- **ESPN polling cadence: 15 seconds** (down from 30s). Parallelized across 9 leagues via `asyncio.gather`.
- **Leagues:** MLB, NBA, NHL, NCAAB, UCL, EPL, La Liga, Bundesliga, MLS.
- **Poll freshness guard:** reject any data >60s old.

### Win probability — two-layer stack

**Layer 1: pure-function per-sport models.** Given current game state, return a baseline probability. Conservative by design — prefer miss-a-trade over overestimate-certainty.

**Layer 2: online isotonic calibrator** (`sports/calibrator.py`). For each `(sport, game_state_bucket)` pair, store `(model_prob, realized_outcome)` observations in `sport_calibration` table. Refit isotonic regression on rolling 90-day window, hourly. Calibrator output is the value we trade on.

- Buckets are pre-specified (not learned), ~30–80 per sport.
- <30 observations: fall back to raw model × shrinkage factor 0.9 toward 0.5.
- ≥30 observations: calibrator output overrides raw model.

### Market matching — `sports_matcher.py`

**Highest-risk component.** Dedicated module, ≥90% test coverage, strict confidence floor.

Three-pass matcher:

1. **Exact normalization** — per-league team-name dictionary (all nicknames, abbreviations, Unicode variants).
2. **Market-type classification** — regex on title: `"{A} vs. {B}"` = ML, `"Spread: {team} ({line})"` = spread, `"O/U {line}"` = total.
3. **Confidence score** — composite of name match + event-slug match + resolution-time proximity (game start ± 12h). **Trade only if confidence ≥ 0.95.**

### Entry rules (all must hold)

| Condition | Threshold |
|---|---|
| Calibrated win probability ≥ | 0.85 |
| Polymarket edge vs model ≥ | 0.04 (4%) |
| Book depth at my entry price ≥ | $10,000 |
| ESPN data freshness < | 60s |
| Matcher confidence ≥ | 0.95 |
| No existing open position in this market | — |

### Sizing

- Base Kelly: `lg_kelly_mult = 0.50` (half-Kelly)
- Applied scaler: `base × strategy_kelly_scaler` (clamped `[0.25, 2.0]`, § 5)
- Max single market: **20%** (down from 25%)
- Max concurrent live-game positions: 6

### Execution

- **Maker-first:** `post_only=True` at best bid + 1 tick → 0% fees.
- **One taker fallback:** if not filled in 30s, convert to taker at best ask. Edge must still be ≥4% post-fee.
- **Cancel-on-adverse-drift:** cancel and re-evaluate if book drifts >1% against us while our maker order rests.

### Exit rules

- **Default:** hold to market resolution.
- **Emergency exit:** calibrated win prob drops below **0.70** → close immediately (taker).
- **Take profit:** price reaches **0.97+** → close, recycle capital.
- **Time stop:** 6h hard maximum hold.
- Every exit writes `exit_reason` to `trade_outcome`.

### Exclusions

- No LLM in the hot path.
- No news/injury ingestion in v10 (injury effects surface indirectly via scoring-pace changes).
- No pre-game trading.
- No exotics (teasers, parlays).

---

## § 4 — Snipe (Capital Recycler)

### Edge thesis

Markets at $0.96+ with a few hours to resolution haven't converged yet because the last three cents of drift are below most traders' attention threshold. Very high win rate, small edge per trade. Uncorrelated with Live Sports.

### v10 simplification: 2 tiers (down from 4)

| Tier | Price | Time to resolve | Verification | Kelly | Max single |
|---|---|---|---|---|---|
| T0 | ≥ $0.96 | ≤ 12h | None (price alone) | 0.50× | 10% |
| T1 | $0.88–0.96 | ≤ 8h | Gemini Flash required | 0.30× | 7% |

Dropped T2/T3 ($0.75–0.85, 72–120h) — edge per trade was below realistic slippage + fees.

### Verification

- **LLM-only** (Gemini Flash). No sportsbook / Odds API dependency.
- Hard daily LLM spend cap: **$2/day**. Exceeding halts Snipe for the rest of the UTC day.
- Prompt returns JSON `{verdict: 'YES_LOCKED' | 'NO_LOCKED' | 'UNCERTAIN', confidence: 0-1}`. Trade only on `YES_LOCKED`/`NO_LOCKED` with confidence ≥ 0.85.

### Entry rules

- Price in T0 or T1 band, time to resolve ≤ tier max
- Book depth at entry ≥ $2,000
- Per-market cooldown: 90min after any prior exit
- Max 2 entries per market per 24h
- Cumulative per-market exposure: 20% of bankroll
- **Dedupe with Live Sports:** Snipe skips any market Live Sports already holds

### Exit rules

- Default: hold to resolution
- Take profit: price hits 0.99 → exit
- Stop loss: price drops 0.08 from entry → exit immediately
- Time stop: 14h max (oracle resolve padding)

---

## § 5 — Learning Layer

Four loops at increasing cadence, each with one job. No hidden state, no deep RL.

### Loop 1 — Per-trade (instant, on every close)

On every position close, write a `trade_outcome` row:

```
{strategy, market_id, market_category, entry_price, exit_price, pnl,
 predicted_prob, realized_outcome,
 game_state_bucket,       # live_sports only (sport + state key)
 tier,                    # snipe only
 kelly_inputs, exit_reason, duration_minutes}
```

This is the substrate; all other loops read from it.

### Loop 2 — Hourly (parameter adjustment)

Single DB transaction every hour:

1. **Refit Live Sports calibrators.** For each `(sport, bucket)` pair, refit isotonic on rolling 90-day window. Skip buckets with <30 obs.
2. **Per-strategy Beta-Binomial Kelly scaler.** Treat each strategy's win/loss history as `Beta(wins+1, losses+1)`. Compute posterior win prob vs predicted. Posterior 1σ below predicted → multiply Kelly by 0.5×; 1σ above → 1.5×. Clamp to `[0.25, 2.0]`. Store in `strategy_performance.kelly_scaler`.
3. **Per-(strategy, category) edge decay.** Compare realized edge on last 50 trades vs last 200. If short-window is negative while long-window is positive, disable that `(strategy, category)` for 48h, email alert.

### Loop 3 — Daily (midnight UTC)

1. **Kill-switch check.** Strategy with < -5% cumulative P&L over last 100 trades → pause 24h. Two paused days → disable; manual re-enable.
2. **Deployment-stage advancement check.** If in `micro_test` + 7 days positive P&L + no safeguard trips → log readiness for `full`. **Human approval required** (never auto-scale).
3. **Daily P&L report** emailed: strategy breakdown, calibrator drift, Kelly scalers.

### Loop 4 — Weekly (Sunday UTC)

1. **Calibrator drift audit.** Per bucket, compare last-7-days Brier vs prior 7 days. >30% degradation → flag.
2. **Bucket adequacy.** Buckets with >500 obs → log split recommendation. Buckets chronically <30 obs → log merge recommendation. Human decides.
3. **Reflection report** — markdown summary of sport-level P&L, predictive game states, recommended config tweaks. Saved to `data/reports/YYYY-MM-DD-weekly.md` and emailed.

### Excluded by design

- No neural net probability models (isotonic is the right tool for sparse per-bucket data)
- No RL over strategy selection (2 strategies, over-engineered)
- No LLM post-trade reflection (text out is not parameters out)
- No cross-strategy feature engineering ("market regime", "volatility context" — premature)

**Principle:** every mechanism must produce a number that directly sizes a trade or gates a strategy. If it can't, it doesn't ship.

---

## § 6 — Risk, Safety, Deployment Ladder

### 5-stage deployment ladder

Advancement requires explicit human approval; no auto-advance.

| Stage | Capital | Deployed cap | Min duration | Gates to advance |
|---|---|---|---|---|
| 0. Dry-run | $2,000 sim | 70% | 14 days | Positive P&L, ≥200 LG + ≥100 Snipe trades, no safeguard trips, ≥10 buckets per sport at ≥30 obs |
| 1. Preflight | — | — | single run | `live_preflight.py` passes (balance, submit+cancel, heartbeat ×3, approvals, collateral) |
| 2. Micro-test | $2,000 live | 5% ($100) | 7 days | Fills within 2% of dry-run predictions, CLOB↔DB reconciled every check, ≥30 live trades, no safeguard trips, P&L within 1σ of dry-run |
| 3. Ramp | $2,000 live | 25% ($500) | 7 days | Sharpe > 0, mean per-bucket Brier degradation < 20% vs the trailing dry-run baseline (same metric as §5 Loop 4), no strategy paused, LLM spend within cap |
| 4. Full | $2,000 live | 70% | ongoing | Scale bankroll only after 30 days positive full-stage P&L |

Stages 2–3 winnings stay in the CLOB account but `system_state.bankroll` stays pegged at $2,000 (excess swept manually) so Kelly sizing doesn't creep on unvalidated live history.

### Defense-in-depth (9 layers, outside-in)

1. **Per-trade gates** — book depth, matcher confidence, edge, freshness; sized `base_kelly × scaler` clamped to `max_single_pct`
2. **Per-strategy pause** — 5% cumulative loss over trailing 100 trades → 24h pause; two pauses → disable
3. **Per-strategy LLM spend cap** — Snipe halts if daily LLM spend > $2
4. **Daily loss circuit breaker** — 15% bankroll loss in UTC day → 6h halt; post-breaker 24h at 50% Kelly
5. **Capital divergence halt** — CLOB vs DB > 10% normally, **> 5% in stages 2–3**. Self-healing after 3 consecutive OK checks
6. **Total drawdown halt** — 30% from high-water → permanent halt, manual SQL reset required
7. **Heartbeat monitor** — no strategy cycle in 10min → warning; 30min → emergency SIGTERM
8. **Shutdown cleanup** — SIGTERM cancels all open CLOB orders
9. **Orphan sweeper** — daily scan of `trades` where `status='open'` and age > `max_hold_hours × 2`; reconciles against CLOB

### Recovery procedures

Three operator scripts (each gated behind explicit flags + audit logging):

- `scripts/audit_wipe_risk.py` — simulates every open position hitting worst-case exit simultaneously; flags if >30% drawdown
- `scripts/reconcile_capital.py` — one-shot ground-truth rewrite of `system_state.bankroll` and `total_deployed` from CLOB
- `scripts/reset_drawdown_halt.py` — requires `--confirm` + human reason; writes to `admin_log`

### Human-in-the-loop duties

- **Daily (5min):** morning report, P&L, calibrator drift, safeguard trips, orphans
- **Weekly (15min):** Sunday reflection report, approve/reject config tweaks, run `audit_wipe_risk.py`
- **Stage advancement (30min):** compare dry-run vs live fills, verify reconciliation, confirm gates, flip `live_deployment_stage` in `.env`

---

## § 7 — Testing

### Layer 1 — Unit tests (≥90% coverage of new code)

| Module | Coverage target |
|---|---|
| `sports/win_prob.py` | All sport models on fixture states: tied late game, overtime, blowouts, weather-shortened MLB, soccer ET+PK |
| `sports/calibrator.py` | Isotonic fit on synthetic data, shrinkage logic <30 obs, bucket-missing fallback, refit idempotency |
| `markets/sports_matcher.py` | **Highest priority.** Fuzzy name matching across 9 leagues; ambiguous → confidence floor rejects; market-type regex on real fixtures; O/U line parsing; spread sign handling |
| `strategies/live_sports.py` | Entry gate (all 6 conditions); emergency exit at 0.70; TP at 0.97; dedupe with Snipe |
| `strategies/snipe.py` | T0/T1 classification; LLM spend cap; dedupe with Live Sports; cooldown; exposure cap |
| `learning/*` | Kelly scaler math; edge decay; per-strategy pause |
| `safeguards/*` | Each halt trips correctly on crafted state; self-healing after N OK checks; cached drawdown within 30s |

### Layer 2 — Integration tests (real DB, mocked CLOB)

Per strategy, full `run_once` path against a Postgres test schema:

1. Live Sports happy path — stub ESPN late-game blowout → 3 markets matched → entry gate passes → order written
2. Live Sports emergency exit — open position + comeback poll → close as taker → `exit_reason` recorded
3. Snipe T0 happy path — no LLM, resolves, P&L recorded
4. Snipe T1 with LLM stub — Gemini returns `YES_LOCKED` → enters → spend cap incremented
5. Dedupe — Live Sports opens M → Snipe scans and skips M
6. Safeguard trip — inject over-drawdown state → `run_once` exits immediately

### Layer 3 — Realistic dry-run (end-to-end)

- Spread-aware fills + simulated taker fee + order-book fetch (Apr 14 work)
- **Shadow mode in stages 2–3:** run live signal through dry-run simulator in parallel. Log live vs simulated fill price; divergence >2% triggers alert.
- All dry-run fills persist to `trades` with `status='dry_run'`; calibrator trains on them.

### Layer 4 — Preflight (before every live start)

- Existing `live_preflight.py` checks (balance, submit+cancel, heartbeat ×3, approvals, collateral)
- **NEW: matcher fixture replay** — 100 canned ESPN/Polymarket pairs; ≥95 must match correctly
- **NEW: calibrator sanity** — every active bucket has either fitted curve or shrinkage fallback
- **NEW: bankroll sanity** — CLOB balance within 1% of `system_state.bankroll`

### Layer 5 — Production smoke tests (hourly, in-process)

Every hour, halt + email if any fails:

1. Kelly scalers in `[0.25, 2.0]`
2. `total_deployed / bankroll` ≤ stage cap
3. Every `status='open'` trade has a `MATCHED` or `LIVE` CLOB order

### Excluded from v10 testing

- Property-based / fuzz testing (nice-to-have, not essential at our size)
- Load / stress tests (<1,000 orders/day)
- LLM determinism tests (test retry/timeout/spend-cap, not LLM output)

### Discipline

Every new module lands with tests in the same PR. TDD via superpowers:test-driven-development skill. Dry-run is not a substitute for unit tests.

---

## § 8 — Milestones (gated, not calendar)

Advancement is gated on objective criteria, not calendar dates. Expected pacing follows; actual timeline is whatever the gates allow.

| Milestone | Gate |
|---|---|
| v10 code complete | Scorched-earth deletions merged; sports/ + strategies/ + learning/ + safeguards/ built; all unit + integration tests green |
| Dry-run validation start | v10 merged to main; bankroll reset to $2,000; `DRY_RUN=true`; LaunchAgent running |
| Dry-run validation complete | Positive cumulative P&L + ≥200 LG + ≥100 Snipe trades + no safeguard trips + ≥10 buckets per sport with ≥30 obs |
| Micro-test live start | Preflight passes; operator approves stage advance |
| Micro-test complete | 7 days + fills within 2% of dry-run predictions + no safeguard trips + ≥30 live trades + P&L within 1σ of dry-run |
| Ramp complete | 7 days + Sharpe > 0 + calibrator drift <20% + no strategy paused + LLM spend within cap |
| Full-stage → bankroll scaling | 30 days positive full-stage P&L |

**Expected pacing (may compress with dense trade flow):** code complete in ~2 weeks; dry-run validation concurrent with late build; live micro-test earliest ~3–4 weeks from start; full-stage earliest ~5–6 weeks from start. The user has indicated these dates will likely compress.

Known slippage risk: if the 14-day dry-run window ends with <200 Live Sports trades (sports-light stretch), the gate blocks advancement. Mitigation: gate is trade-count-based, not calendar-based.

---

## Open questions / future work (explicitly out of v10 scope)

- **News/catalyst event trading** — promising extension once core loop is profitable
- **Kalshi cross-venue** — regulated exchange, different market set, future optionality
- **Mean reversion revisit** — only after realistic dry-run validates spread modeling over a full month of live v10 data
- **Multi-bankroll scaling** — beyond $2K → $5K+ requires fresh risk analysis (Kelly + max drawdown at larger capital is not a linear scale-up)
- **WebSocket-driven microstructure signals** — interesting research direction, but crowded space and high infra bar; not for v10

---

## Design self-review

- Placeholder scan: clean (no TBDs, no "TODO", no vague requirements)
- Internal consistency: scope (§1) matches architecture (§2) matches strategy details (§3–4) matches learning (§5) matches risk (§6)
- Scope: single implementation plan is feasible (strategy deletions + two new strategies + learning + safeguards refactor); if plan balloons, decompose into (a) scorched earth + infra, (b) Live Sports + Snipe, (c) learning + tests
- Ambiguity: gates and thresholds are all numeric; no interpretive language

Ready for implementation planning.
