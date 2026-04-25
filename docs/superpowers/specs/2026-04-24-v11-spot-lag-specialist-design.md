# Polybot v11 — "Spot-Lag Specialist" Design Spec

**Date:** 2026-04-24
**Status:** Approved for implementation planning
**Builds on:** v10 (2026-04-16-v10-information-arb-specialist-design.md). Does not supersede.

---

## Context

v10 shipped on 2026-04-19 across PRs #5–7, then was patched through PRs #8–15 (matcher coverage, Gamma `/events` discovery, spread-market trading). The bot has been alive in dry-run for 3+ days but has **placed zero trades since v10 went live**. Logs show why: the matcher finds MLB markets at >0.99 confidence but they're almost exclusively O/U (totals), which `_evaluate_game` explicitly skips. PR #15 added spread support but explicitly deferred totals.

Two structural gaps in v10:

1. **Sports market coverage is incomplete.** Without totals, MLB live trading is dead in current market conditions. Pregame sharp-line trading — the highest-EV trade in sports markets per CLV literature — isn't built either.
2. **No 24/7 trade flow.** Sports go quiet on off-days, weekends, and overnight. The bot has substantial idle time during which competitive prediction markets continue trading.

v11 closes both gaps. The strategic anchor is a new **Crypto Spot-Lag Specialist** strategy that runs 24/7 against deterministic-resolution price markets. The tactical predecessor is a v11.0 release that completes the v10 sports thesis with totals + pregame sharp-line + the deferred hourly learning loop.

## Goals

1. Generate observable trade flow within 24h of v11.0 deployment (currently zero).
2. Add a 24/7 strategy with structurally defensible edge — not retail-direction picking.
3. Validate the crypto spot-lag thesis empirically *before* committing strategy engineering, via a 48h-7d lag-measurement prework that gates v11.1 implementation.
4. Maintain v10's risk discipline: no relaxation of the deployment ladder, the safeguards, or the human-in-the-loop checkpoints.
5. Maximize risk-adjusted P&L growth on the existing $2,000 simulated bankroll.

## Non-Goals

- News-event reaction trading (speed war we cannot win at our infrastructure level).
- Bracket / sum-to-1 / combinatorial arbitrage (the -$124 from v10's `arbitrage` strategy was a structural warning about Polymarket book depth at multi-leg execution).
- Resolution-rules edge as a production strategy (research lane; revisit in v12).
- Implied-vol modeling from Deribit options (v2 enhancement once realized-vol baseline is profitable).
- Hourly crypto markets (too noisy, MM density too high) and monthly+ crypto markets (vol assumptions break down, low frequency).
- SOL and other non-BTC/ETH crypto in v1 (defer to v11.2 once BTC/ETH validated).

---

## § 1 — Scope

### Two-stage release

| Stage | Estimated effort | Status gate |
|---|---|---|
| **v11.0** Sports completion | ~1 week, 3 PRs | Ships unconditionally |
| **v11.1** Crypto Spot-Lag Specialist | 2–3 weeks, ~5 PRs | Gated on Layer-0 lag measurement (§ 7) |

v11.0 ships first to unblock live trade flow. v11.1 implementation is *gated* on a 48h–7d empirical lag-measurement script that must demonstrate a tradeable lag exists before strategy code is written.

### v11.0 components (sports completion)

| Component | Module | Adds |
|---|---|---|
| Totals Gaussian model | `polybot/sports/totals_model.py` (NEW) | Live Sports trades O/U markets |
| Pregame sharp-line strategy | `polybot/strategies/pregame_sharp.py` (NEW) | New strategy, ESPN BPI as truth signal |
| Hourly learning loop completion | `polybot/learning/` extensions | Calibrator refit + Kelly scaler refit + edge decay (deferred from v10) |

### v11.1 components (crypto spot-lag)

| Component | Module | Adds |
|---|---|---|
| Lag measurement (PRE-implementation gate) | `scripts/measure_crypto_lag.py` (NEW) + `crypto_lag_observations` table | Empirical edge-distribution + persistence + spot-source agreement data |
| Spot client | `polybot/crypto/spot_client.py` (NEW) | Coinbase Pro WebSocket primary, Binance fallback, REST fallback |
| Oracle resolver | `polybot/crypto/oracle_resolver.py` (NEW) | Parses Polymarket crypto market resolution rules with ≥0.95 confidence floor |
| Vol estimator | `polybot/crypto/vol_estimator.py` (NEW) | 30d realized vol primary, 7d-spike override, low-vol regime guard |
| Implied probability | `polybot/crypto/implied_prob.py` (NEW) | Pure-function lognormal/BS pricing |
| Crypto matcher | `polybot/markets/crypto_matcher.py` (NEW) | Strike/symbol/timestamp extraction |
| Crypto Spot-Lag strategy | `polybot/strategies/crypto_spot_lag.py` (NEW) | The strategy itself |
| Portfolio caps | `polybot/safeguards/portfolio_caps.py` (NEW) | Cross-strategy crypto-directional exposure cap |

### Excluded from v11

- LLM in any hot path (consistent with v10).
- News/injury/event-text ingestion (no news strategies).
- Pre-game trading for non-sports markets.
- Maker-first execution for crypto (taker-only — see § 3).
- Multi-asset correlation modeling beyond a fixed BTC/ETH 0.8 correlation factor.
- Cross-venue arbitrage (Kalshi etc. — explicitly killed in v10, still excluded).

### Capital allocation post-v11

| Strategy | Cap (% of bankroll) | Role |
|---|---|---|
| Crypto Spot-Lag (v11.1) | 50% | Primary engine, 24/7 |
| Live Sports — ML/spread/total (v10 + v11.0a) | 20% | Latency arb, sport-hours only |
| Pregame Sharp-Line (v11.0b) | 20% | Fat-pitch sports value |
| Snipe T0/T1 (v10) | 10% | Capital recycler |

Caps sum to 100% but expected concurrent deployment is ~70% (matching v10 stage-0 dry-run cap).

---

## § 2 — Architecture

### Unchanged from v10

- Single Python 3.13 async process; `asyncio` event loop
- LaunchAgent `ai.polybot.trader`, PostgreSQL `polybot` DB, `py-clob-client`, `structlog`
- `TradingContext` dataclass shared across strategies; `asyncio.Lock` gating bankroll writes
- All 9 v10 safeguard layers (drawdown halt, capital divergence, preflight, deployment stage gate, etc.)

### New top-level modules

```
polybot/
├── core/                    # unchanged (Engine, Config + new keys)
├── strategies/
│   ├── live_sports.py       # MODIFIED — adds _evaluate_total branch
│   ├── pregame_sharp.py     # NEW (v11.0b)
│   ├── crypto_spot_lag.py   # NEW (v11.1)
│   ├── snipe.py             # unchanged
│   └── base.py              # unchanged
├── markets/
│   ├── sports_matcher.py    # unchanged
│   ├── crypto_matcher.py    # NEW (v11.1)
│   └── (gamma.py, clob.py, scanner.py — unchanged)
├── sports/
│   ├── win_prob.py          # unchanged
│   ├── calibrator.py        # unchanged
│   ├── margin_model.py      # unchanged (PR #15)
│   ├── totals_model.py      # NEW (v11.0a)
│   └── espn_client.py       # MODIFIED — adds BPI fetch
├── crypto/                  # NEW (v11.1)
│   ├── spot_client.py
│   ├── oracle_resolver.py
│   ├── vol_estimator.py
│   └── implied_prob.py
├── learning/                # MODIFIED (v11.0c) — completes hourly loop
├── safeguards/
│   ├── (existing files unchanged)
│   └── portfolio_caps.py    # NEW (v11.1)
└── db/
    └── (new tables: pregame_calibration, crypto_lag_observations)
```

### Key architectural decisions

1. **`crypto/` is a top-level peer to `sports/`**, not nested inside `strategies/`. Spot client, vol estimator, and BS pricing are data infrastructure consumed by the strategy but independently testable. Future crypto strategies (event-reaction in v12) reuse the same primitives.

2. **`oracle_resolver.py` is the make-or-break component for crypto.** Every Polymarket crypto market has a resolution rule pointing to a specific source ("Coinbase BTC-USD price at 4pm ET on date X" / "Chainlink BTC/USD aggregator at block N"). The resolver parses rules into `(venue, symbol, resolution_ts, method)` with a confidence score. Markets below 0.95 confidence are skipped, never traded. Trading against a spot source that doesn't match the resolution source is basis risk, not alpha.

3. **`portfolio_caps.py` is a centralized check, not per-strategy enforcement.** Strategies query `portfolio_caps.can_enter(strategy, symbol, proposed_dollar_delta)` before submitting. Returns False if any cap would be breached. Sports strategies don't currently have signed exposure (binary-resolving positions); the module is crypto-aware but extensible.

4. **`pregame_sharp.py` is a separate strategy file**, not a Live Sports extension, because entry timing (T-30min) and signal source (ESPN BPI) differ. Lifecycle reuses everything else.

5. **No new processes, no new languages, no new data stores.** Same constraint v10 lived under. Crypto WebSocket runs in the existing asyncio loop.

6. **Position handoff via `position_owner` column.** A new column on `trades` tracks which strategy *currently owns* exit management. Pregame opens a position; at tip-off, ownership transfers to Live Sports. Hard rule: only one strategy can own a market at any time.

### Engine loop

```
engine.run():
  loop forever:
    check safeguards (drawdown, divergence, stage) → halt if any trip
    parallel:
      live_sports.run_once(ctx)
      pregame_sharp.run_once(ctx)        # NEW (v11.0b)
      crypto_spot_lag.run_once(ctx)      # NEW (v11.1)
      snipe.run_once(ctx)
    every 60s:  capital_divergence_check()
    every 5min: bankroll_reconcile()
    every 1h:   learning_cycle(            # COMPLETED (v11.0c)
                  calibrator_refit_live_sports,
                  calibrator_refit_pregame,
                  kelly_scaler_refit,
                  edge_decay_evaluate)
    every 24h:  self_assessment(kill_switch, daily_report)
    every 7d:   weekly_reflection()
```

---

## § 3 — v11.1 Crypto Spot-Lag Engine

### Edge thesis

Polymarket lists markets like "Will BTC close above $X by Friday 4pm ET?". The market resolves from a deterministic price oracle (typically a TWAP from Coinbase Pro at a known timestamp, occasionally Chainlink or a UMA-resolved feed).

The **true probability** under geometric Brownian motion with annualized vol σ and drift μ ≈ 0:

```
ln(spot_T / spot_now) ~ Normal((μ − σ²/2)·τ, σ²·τ)
P(spot_T > strike) = 1 − Φ(d)
where d = (ln(strike / spot_now) − (μ − σ²/2)·τ) / (σ·√τ)
       τ = (T − now) / 365 days  (annualized)
       Φ = standard normal CDF
```

The **market price** lags spot by 30s–5min on large moves and overshoots/undershoots during low-volume hours. Edge per trade = `|BS_implied_prob − polymarket_price|`.

**Why this edge persists:**

1. Polymarket retail flow is directional speculation, not arbitrage-driven. Retail doesn't recompute BS every tick.
2. Real crypto quants work where the liquidity is — Binance, Coinbase, Deribit. Polymarket's crypto markets are downstream venues, populated by retail, not professional MMs.
3. The pricing model has no calibration mystery, no bucket sparsity, no oracle dispute risk on Coinbase/Chainlink-resolved markets.
4. Resolution is mechanical at oracle time. No interpretation risk.

**Honest counter-thesis:**

- If a serious MM bot already runs this exact strategy, the lag may be 1–5 seconds — too tight for our infrastructure (LaunchAgent on a Mac, async Python).
- Vol estimation is the soft spot. Realized vol is backward-looking; a vol regime shift miscalibrates our prices for hours.
- Resolution-source mismatch: 0.1% basis between our spot source and the oracle source on a tight-strike market is alpha-killing.

The **Layer-0 lag-measurement gate (§ 7)** must demonstrate tradeable lag empirically before strategy code ships.

### Pricing module — `polybot/crypto/implied_prob.py`

Pure functions. No state, no side effects.

```python
def prob_above(spot, strike, tau, sigma, mu=0.0) -> float
def prob_below(spot, strike, tau, sigma, mu=0.0) -> float
def prob_between(spot, lo, hi, tau, sigma, mu=0.0) -> float
```

For τ < 7 days, μ = 0. For 7 ≤ τ ≤ 14 days, μ = 0 with a 1% probability bound widening (conservatism on long-dated markets). Markets with τ > 14 days are not traded in v1.

### Vol estimation — `polybot/crypto/vol_estimator.py`

**Primary signal: 30-day realized vol** annualized from 1-minute log-returns of the resolution-source spot:

```
σ_30d = sqrt(525600) × stdev(log(p_t / p_{t-1}))    # over 30d rolling window of 1m bars
```

**Vol-spike override:** if 7d realized > 1.3 × 30d realized, use 7d.

**Low-vol regime guard:** if 30d realized < 20% annualized, halve all crypto position sizes for that symbol.

**No GARCH, no Heston, no vol-of-vol modeling.** Realized stdev is the right tool for v1.

### Oracle alignment — `polybot/crypto/oracle_resolver.py`

For each Polymarket crypto market, parse resolution rules into:

```python
@dataclass
class ResolutionSource:
    venue: Literal["coinbase", "binance", "chainlink", "kraken"]
    symbol: str               # "BTC-USD", "ETH-USD"
    resolution_ts: datetime   # exact UTC timestamp
    method: Literal["spot", "twap_5m", "twap_1h", "close"]
    confidence: float         # 0-1, from regex match strength
```

Rules:
- Confidence < 0.95 → market skipped, never traded.
- TWAP methods require a small TWAP correction at evaluation time (implied prob computed against expected-TWAP, not spot).
- Chainlink-resolved markets are tradeable but lower priority (Chainlink updates every 30–60s, shorter lag arms-race).
- UMA-resolved markets: refuse entry within 6h of resolution (UMA disputes happen in the resolution window).
- A seed mapping table covers known patterns; unknown patterns are logged for human review, not guessed.

### Spot data — `polybot/crypto/spot_client.py`

- **Primary:** Coinbase Pro WebSocket (matches most common Polymarket resolution source).
- **Fallback:** Binance WebSocket. REST fallback if both WebSockets fail.
- Stale guard: any spot reading > 5s old triggers strategy pause for that symbol.
- Heartbeat: must publish a tick every ≤ 2s during normal operation; missed heartbeat triggers feed-failure handling.
- Cross-source check: Coinbase + Binance disagreement > 0.3% for > 30s halts strategy until reconciled.

### Entry rules (all must hold)

| Condition | Threshold |
|---|---|
| Resolver confidence | ≥ 0.95 |
| Spot freshness | < 5s |
| Time to resolve `τ` | 1h ≤ `τ` ≤ 14d |
| Edge magnitude | ≥ 0.025 (2.5% post-fee) |
| Polymarket book depth at entry | ≥ $5,000 |
| Vol regime | 30d σ ≥ 0.20 annualized |
| No existing position in this market | — |
| Portfolio caps allow proposed size | — |

### Sizing

```
position_pct = cs_kelly_mult × kelly_fraction(p_model, p_market) × strategy_kelly_scaler
             clamped to [0.25 × cs_kelly_mult, 2.0 × cs_kelly_mult]
             then clamped to [0, 8%] of bankroll
```

`cs_kelly_mult = 0.30` (more conservative than Live Sports' 0.50 because BS-implied prob is a model output, not calibrated frequency; until dry-run validates win rate, sizing stays defensive).

Strategy bankroll cap: 50% of deployable.

### Execution — taker-only

No maker/post-only attempts. Spot moves continuously; any unfilled maker order at the BS-implied price becomes stale within seconds. Taker fee (1% Polymarket) is explicit in edge threshold (2.5% gross → 1.5% post-fee).

**Pre-trade re-check:** between edge detection and order submission, re-fetch spot. If spot moved > 0.3% in either direction, recompute `p_model` and abort if edge no longer clears 2.5% post-fee.

### Exit rules

| Trigger | Action |
|---|---|
| Resolution time reached | Hold to oracle settlement (no slippage) |
| Edge inverts (model now disagrees with our side by ≥ 2%) | Close immediately at market |
| Spot crosses strike with `τ < 1h` | Close immediately (spot pinning region) |
| TP: market price reaches `p_model ± 0.01` | Close at market (lag converged) |
| Time stop: position open ≥ 24h | Close at market (long holds accumulate vol risk) |
| Spot data stale > 60s | Close immediately (can't manage what we can't measure) |

**The "edge inverts" exit is the key one.** In sports, win prob trends monotonically toward 1 or 0; Live Sports holds. In crypto, spot mean-reverts within sessions; a position whose model now disagrees has lost its thesis and should close.

### Configuration knobs (hot-reloadable)

```
cs_kelly_mult = 0.30
cs_min_edge = 0.025
cs_book_depth_min_usd = 5000
cs_max_single_pct = 0.08
cs_strategy_cap_pct = 0.50
cs_resolver_confidence_min = 0.95
cs_spot_freshness_max_s = 5
cs_tau_min_hours = 1
cs_tau_max_days = 14
cs_vol_floor_annualized = 0.20
cs_pretrade_recheck_drift_pct = 0.003
cs_edge_invert_threshold = 0.02
cs_per_symbol_signed_cap = 0.25
cs_per_symbol_gross_cap = 0.35
cs_cross_symbol_corr_factor = 0.80
cs_time_stop_hours = 24
cs_uma_refuse_window_hours = 6
```

---

## § 4 — v11.0 Sports Completion

### v11.0a — Totals Gaussian model

Mirror PR #15's spread-margin model, but for total runs/points/goals.

**New module:** `polybot/sports/totals_model.py`

```python
def cover_prob_total(sport, period, clock_s, current_total, line, side) -> float | None:
    """P(final_total > line) for "Over", P(final_total < line) for "Under".
    Returns None for unsupported sport / invalid inputs."""
```

```
remaining_total ~ Normal(expected_remaining, σ_total_remaining²)
σ_total_remaining = total_σ_total × √(fraction_of_regulation_remaining)
P(over) = 1 − Φ((line − current_total − expected_remaining) / σ_total_remaining)
```

Per-sport σ table:

| Sport | total_σ_total | Notes |
|---|---|---|
| MLB | 4.0 (runs) | scale by innings remaining × avg run rate |
| NBA | 14 (points) | scale by game minutes remaining |
| NCAAB | 16 (points) | scale by game minutes remaining |
| NHL | 1.6 (goals) | scale by minutes remaining |
| Soccer | 1.3 (goals) | scale by minutes remaining |

**Strategy extension** (`live_sports.py`): add `_evaluate_total` branch when `match.market_type == "total"`. Mirrors `_evaluate_spread` (PR #15) — picks larger-edge side if it clears `lg_total_min_edge=0.05` (lower than spread's 0.06; totals have lower variance per period).

**Production-safety invariants:**

| Invariant | Test |
|---|---|
| Unknown sport → rejected | `test_total_unsupported_sport_returns_none` |
| Game-over → deterministic; reject within 95% elapsed | `test_total_game_over_deterministic` |
| Both edges below bar → rejected | `test_total_evaluate_rejects_when_edge_below_bar` |
| Missing line → rejected | `test_total_evaluate_rejects_missing_line` |
| Symmetry: over/under sum to 1 | `test_total_over_under_sum_to_one` |
| σ shrinks with time | `test_total_sigma_shrinks_toward_end_of_game` |

Config: `lg_total_min_edge = 0.05`, `lg_total_kelly_reduction = 0.50`.

### v11.0b — Pregame sharp-line strategy

**New module:** `polybot/strategies/pregame_sharp.py`

**Edge thesis:** Closing-line value (CLV) is the most well-studied quantity in sports betting (Levitt 2004 et al.). Sportsbook closing lines reflect sharp-money + house adjustment; they beat 99% of pre-game predictors. Polymarket pre-game prices often haven't fully converged to the closing line because Polymarket pre-game volume is thinner.

**Signal source:** ESPN BPI (Basketball/Baseball/Hockey Power Index) game probabilities, fetched via extended `espn_client.py`.

```python
async def get_bpi_win_prob(sport: str, game_id: str) -> float | None:
    """Returns ESPN BPI's home-team win probability.
    Available 60+ min before game start; finalized at tip-off."""
```

**Calibration:** New table `pregame_calibration` per (sport, BPI bucket); same isotonic-regression pattern as `sport_calibration`. Buckets: 10 BPI ranges × 3 home/road/neutral splits per sport.

**Entry rules:**

| Condition | Threshold |
|---|---|
| Time to game start | 15 ≤ minutes ≤ 60 |
| Calibrated BPI win prob | ≥ 0.60 |
| Polymarket edge vs calibrated BPI | ≥ 0.04 |
| Book depth at entry price | ≥ $5,000 |
| Matcher confidence | ≥ 0.95 (reuses sports_matcher) |
| BPI freshness | < 6h |
| No existing position from Live Sports or Pregame in this market | — |

**Sizing:** `pg_kelly_mult = 0.40`. Cap 12% per single position.

**Execution:** Maker-first like Live Sports (slow-moving pre-game, no spot-lag arms race). 60s taker fallback.

**Exit rules:**

- Default: hold to game start, then transition to Live Sports' management.
- Pre-tip emergency: if BPI updates and calibrated win prob drops below 0.50, close immediately.
- TP at 0.95 pre-tip.

**Position handoff:** new column `trades.position_owner`. Pregame opens with `position_owner='pregame_sharp'`. At tip-off, owner is updated to `'live_sports'` and pregame's exit logic disengages. Hard rule: only one owner per market at any time.

### v11.0c — Hourly learning loop completion

Wires the items deferred from v10 spec § 5 Loop 2:

1. **Live Sports calibrator refit** every hour (currently only learns on observation-add).
2. **Beta-Binomial Kelly scaler refit** per strategy.
3. **Edge decay disable** (50-trade short window vs 200-trade long window) per (strategy, category).
4. **Pregame calibrator refit** (new).

Single DB transaction per hour. No new architecture; just connecting wires v10 left dangling.

### v11.0 release dependencies

```
v11.0a (totals)         — independent, ships first
v11.0c (hourly loop)    — independent, ships in parallel with v11.0a
v11.0b (pregame sharp) ─→ depends on v11.0c for the calibrator refit
```

PR order: v11.0a → v11.0c → v11.0b.

---

## § 5 — Risk + Portfolio Caps

### Crypto-specific failure modes (and controls)

**1. Coordinated regime change.** BTC dumps 8% in 30min on macro news; long positions die in cluster.
- Per-symbol signed exposure cap: 25% net long *or* short.
- Cross-strategy crypto-directional cap: total absolute net dollar-delta from all open crypto positions ≤ 35% of bankroll.
- High-vol sizing: 30d σ > 80% annualized → halve all crypto Kelly.

**2. Spot-feed failure.**
- 60s stale spot → close all open crypto positions, halt strategy 5min.
- Coinbase + Binance disagreement > 0.3% for > 30s → halt until reconciled.
- Heartbeat: ≤ 2s tick cadence required.

**3. Oracle-source mismatch / dispute.**
- Resolver confidence < 0.95 floor (already in entry rules).
- UMA-resolved markets: refuse entry within 6h of resolution.
- After resolution, reconcile P&L against actual oracle settlement; > 2% divergence → audit alert.

**4. Vol-of-vol surprise.**
- 7d-vs-30d spike override (§ 3) catches the obvious case.
- Hard kill: 1h realized vol exceeds 30d × 3 → halt new entries 1h. Existing positions evaluated via aggressive-exit rules.

**5. Liquidity collapse during the event.**
- Edge-invert exits use limit orders 0.5% inside best bid/ask, with 30s taker fallback.
- Time-stop exits execute as taker immediately.
- Forced exit with depth < $1,000 at our size → partial exit at smaller size, retry next cycle.

**6. Recursive lag (faster MM bots have already eaten the edge).**
- Pre-trade re-check (§ 3) catches obvious staleness.
- Slippage sentinel: track per-trade slippage between detected `p_market` and filled `p_market`. 7-day mean > 1% → auto-pause crypto, email alert.

**7. Dry-run blind spots.**
- Stages 2–3 shadow mode: every live signal also runs through dry-run simulator. > 2% fill divergence → alert.
- Same v10 ladder applies: 5% / 25% / 70% deployed cap.

### `polybot/safeguards/portfolio_caps.py`

Centralized check, runs every cycle before strategy entries:

```python
@dataclass
class PortfolioState:
    crypto_signed_exposure_pct: float
    crypto_gross_exposure_pct: float
    per_symbol_signed: dict[str, float]
    per_symbol_gross: dict[str, float]
    cross_symbol_corr_adjusted_signed: float  # BTC + ETH treated as 0.8 correlated

def can_enter(strategy: str, symbol: str, proposed_dollar_delta: float) -> bool: ...
```

Strategies query before submitting. Returns False if any cap breached.

### Existing v10 safeguards — no relaxation

All 9 layers from v10 § 6 apply unchanged:

1. Per-trade gates — implemented per § 3
2. Per-strategy pause (5% loss / 100 trades) — applies via existing learning loop
3. LLM spend cap — N/A for crypto
4. Daily loss circuit breaker (15%) — applies bankroll-level
5. Capital divergence halt — applies
6. Total drawdown halt (30%) — applies
7. Heartbeat monitor — extended with spot-feed heartbeat
8. Shutdown cleanup — applies
9. Orphan sweeper — applies

### v11.1 deployment ladder additions

| Stage | Crypto-specific gate addition |
|---|---|
| 0. Dry-run | 14 days + ≥ 200 crypto trades + Layer-0 lag-measurement passes |
| 1. Preflight | Spot-feed redundancy verified + oracle-resolver fixture replay ≥ 95% |
| 2. Micro-test | First 7 days at half normal Kelly (`cs_kelly_mult = 0.15`) |
| 3. Ramp | Standard `cs_kelly_mult` restored if Sharpe > 0 |
| 4. Full | Standard caps |

### Recovery procedures

One new operator script:

- `scripts/audit_crypto_basis_risk.py` — for each open crypto position, fetch current spot from Coinbase + Binance, compute the implied resolution price under each, flag divergences > 0.5%. Run before stage advancement and weekly.

---

## § 6 — Learning Layer Additions

v10's four-loop structure is preserved. v11 changes:

### Loop 1 — Per-trade

`trade_outcome` rows for crypto trades populate:

```
{strategy: "crypto_spot_lag", market_id, symbol, entry_price, exit_price, pnl,
 model_inputs: {spot, sigma, tau, strike(s), p_model, p_market, venue, resolver_confidence},
 exit_reason: "resolution"|"edge_invert"|"spot_pinning"|"tp"|"time_stop"|"feed_failure",
 duration_minutes}
```

`model_inputs` makes per-trade post-mortems trivial.

### Loop 2 — Hourly (v11.0c completion + v11.1 additions)

1. Refit Live Sports calibrators (deferred from v10).
2. Refit pregame_calibration (new).
3. Refit per-strategy Beta-Binomial Kelly scaler (deferred from v10).
4. Per-(strategy, category) edge decay evaluation (deferred from v10).
5. **Crypto slippage sentinel** (new): rolling 7-day mean fill-time slippage; > 1% triggers strategy pause.

### Loop 3 — Daily

Existing v10 checks plus:

6. **Crypto basis-risk drift**: for any open crypto position, recompute `p_model` using both Coinbase + Binance spot; > 0.5% divergence flags audit.

### Loop 4 — Weekly

Existing v10 checks plus:

7. **Crypto vol-regime audit**: report 30d realized σ trajectory for BTC and ETH; flag entries into low-vol regime (< 20%) or high-vol regime (> 80%).
8. **Pregame CLV report**: closing-line-value distribution per pregame entry. Negative 30-day CLV is the kill signal for the strategy.

### Excluded by design

- No neural-net win-prob models for crypto (BS lognormal *is* the right tool for this domain; sport calibration is a separate concern).
- No RL over strategy selection (4 strategies; over-engineered).
- No LLM post-trade reflection.
- No cross-strategy "regime detection" — premature.

---

## § 7 — Testing

### Layer 0 — Pre-implementation lag measurement (NEW for v11.1)

**Hard gate before strategy code ships.** Standalone script `scripts/measure_crypto_lag.py` runs in parallel with the existing bot for 48 hours (extending to 7 days if 48h shows ambiguous results).

Records to new table `crypto_lag_observations`:

```
ts, market_id, polymarket_yes, polymarket_no, polymarket_depth,
coinbase_spot, binance_spot, vol_30d, tau_hours,
computed_p_model, raw_edge
```

Sampled every 30s on every BTC/ETH market with `1h ≤ τ ≤ 14d`.

**Pass criteria for proceeding to v11.1 strategy implementation:**

- Median edge-persistence ≥ 60s (we can fill within that window)
- Mean edge in tail (top decile of `|p_model − p_market|`) ≥ 3%
- Spot-source 99th-percentile divergence ≤ 0.3%

**If any criterion fails:** v11.1 crypto track is killed. Engineering redirected to deeper sports work or a different opportunity.

### Layer 1 — Unit tests (≥ 90% coverage of new code)

| Module | Coverage target |
|---|---|
| `crypto/implied_prob.py` | BS pricing against canonical option-pricing fixtures; deep ITM ≈ 1, deep OTM ≈ 0, ATM ≈ 0.5; vol-zero edge case; τ-zero edge case; between-strikes composition |
| `crypto/vol_estimator.py` | Synthetic returns with known σ; 7d-vs-30d spike override; low-vol regime detection |
| `crypto/oracle_resolver.py` | **Highest priority.** Fixture set of 50 real Polymarket crypto rules; ≥ 95% confidence parsing; UMA/Coinbase/Chainlink classification; refusal on ambiguous rules |
| `crypto/spot_client.py` | WebSocket reconnect (mock disconnect), REST fallback, dual-source agreement check, stale-data guard |
| `markets/crypto_matcher.py` | Strike/symbol/timestamp extraction across BTC/ETH market title variants |
| `strategies/crypto_spot_lag.py` | All entry gates, all exit triggers, pre-trade re-check, edge-invert exit, time stop |
| `safeguards/portfolio_caps.py` | Per-symbol signed/gross caps, cross-symbol correlation adjustment, `can_enter` blocks at threshold |
| `sports/totals_model.py` | Per PR #15 invariants table (§ 4 v11.0a) |
| `strategies/pregame_sharp.py` | Entry gate (all 7 conditions); pre-tip emergency exit; tip-off handoff to Live Sports |
| `learning/*` (extensions) | Hourly loop refit; CLV computation; slippage sentinel |

### Layer 2 — Integration tests

Per-strategy full `run_once` paths against a Postgres test schema:

1. **Crypto happy path** — mock Coinbase WebSocket emits BTC spot; mock Polymarket market with stale price; entry passes all gates; order written; resolution payout reconciled.
2. **Crypto edge invert during hold** — open position; spot moves against; edge-invert exit triggers.
3. **Crypto spot-feed failure** — mock disconnect mid-cycle; open positions force-closed; strategy halts 5min; resumes.
4. **Crypto regime kill** — vol spike triggers 1h halt; existing positions evaluated; new entries blocked.
5. **Portfolio cap dedupe** — two strategies attempt same-direction BTC entry; second blocked.
6. **Oracle source mismatch** — market resolves at different price than our model assumed; reconciliation flags audit alert.
7. **Pregame → Live Sports handoff** — pregame opens at T-30min; at tip-off, owner transitions; Live Sports manages exit.
8. **Totals happy path** — late-game MLB lead; total markets matched; over/under entry picked correctly.
9. **Pregame emergency exit** — BPI updates pre-tip drop calibrated WP below 0.50; position closed at market.

### Layer 3 — Realistic dry-run

- All v10 dry-run infrastructure unchanged (spread-aware fills, simulated taker fee, order-book fetch).
- Crypto positions persist to `trades` with `status='dry_run'` and `model_inputs`.
- Shadow mode in stages 2–3: every live crypto signal also runs through dry-run simulator; > 2% fill divergence → alert.
- Stage 0 minimum: 200 crypto trades before advancement.

### Layer 4 — Preflight (every live start)

Existing v10 preflight checks plus:

- **Crypto matcher fixture replay** — 50 canned crypto market titles; ≥ 47 must parse correctly.
- **Oracle resolver fixture replay** — 50 canned resolution-rules texts; ≥ 47 must classify correctly.
- **Spot-feed redundancy** — both Coinbase and Binance WebSocket connections established; ≥ 1 tick within 5s.
- **Vol estimator sanity** — for BTC and ETH, 30d realized vol within historical range (10%-200%); outside → halt.

### Layer 5 — Production smoke tests (hourly)

Existing v10 hourly checks plus:

1. Open crypto positions: each has fresh spot data (< 60s old).
2. `crypto_lag_observations` insertions in last hour > 0 (recording stays alive for ongoing tuning).
3. Per-symbol signed/gross caps in compliance.

### Discipline

Every new module lands with tests in the same PR. TDD via `superpowers:test-driven-development`. The lag-measurement script (Layer 0) is a hard gate before v11.1 strategy implementation.

---

## § 8 — Milestones (gated)

Advancement is gated on objective criteria, not calendar dates.

| Milestone | Gate |
|---|---|
| v11.0a code complete | Totals model + tests merged; Live Sports `_evaluate_total` branch live |
| v11.0c code complete | Hourly learning loop refit wired; calibrator refit + Kelly scaler + edge decay all running |
| v11.0b code complete | Pregame sharp strategy + tests merged; ESPN BPI fetch live; position handoff via `position_owner` |
| v11.0 dry-run productive | Cumulative trade count > 0 from each of {Live Sports totals, pregame_sharp} within 7 days of merge |
| v11.1 lag-measurement complete | 48h–7d data collected; pass criteria evaluated; go/no-go decision committed to spec |
| v11.1 code complete | All v11.1 modules merged; tests green; lag-measurement passed |
| v11.1 dry-run validation start | v11.1 merged to main; LaunchAgent restarted with crypto strategy enabled |
| v11.1 dry-run validation complete | Positive cumulative crypto P&L + ≥ 200 crypto trades + no safeguard trips + slippage sentinel within bounds |
| v11.1 micro-test live start | Preflight passes; operator approves stage advance |
| v11.1 micro-test complete | 7 days at half-Kelly + fills within 2% of dry-run + no safeguard trips |
| v11.1 ramp complete | 7 days standard Kelly + Sharpe > 0 |
| v11.1 full → bankroll scaling | 30 days positive full-stage P&L |

**Expected pacing (may compress with dense trade flow):** v11.0 complete in ~1 week. v11.1 lag-measurement runs concurrently. v11.1 code complete in ~3 weeks from start (assuming lag-measurement passes). v11.1 micro-test earliest ~6 weeks from v11 start. Full-stage earliest ~8 weeks.

---

## Open questions / future work (out of v11 scope)

- **Deribit implied vol blend** for `vol_estimator` (v12).
- **SOL and other crypto assets** beyond BTC/ETH (v11.2 once BTC/ETH validated).
- **Funding-rate signal** as a directional drift input for crypto pricing (v12).
- **Hourly crypto markets** (excluded for noise; revisit if v11.1 daily/weekly proves profitable and we want frequency expansion).
- **Resolution-rules edge as a production strategy** (research lane; v12).
- **News-event reaction** (excluded; speed war).
- **Multi-bankroll scaling** beyond $2K → $5K+ (fresh risk analysis required).
- **Cross-venue arbitrage** (excluded since v10).

---

## Design self-review

- **Placeholder scan:** Clean — no TBDs, TODOs, or vague requirements.
- **Internal consistency:** Scope (§ 1) matches architecture (§ 2) matches strategy details (§ 3–4) matches risk (§ 5) matches learning (§ 6) matches testing (§ 7). Capital allocation in § 1 aligns with strategy caps in § 3 and portfolio caps in § 5.
- **Scope:** v11.0 and v11.1 are independently shippable; v11.1 is gated on v11.0c (hourly loop) only via the calibrator-refit dependency that pregame uses, not crypto. Lag-measurement is a hard gate for v11.1 strategy implementation but doesn't block v11.0 at all.
- **Ambiguity check:** All thresholds are numeric; oracle-resolver confidence floor is explicit; portfolio-cap math is specified. Position-handoff rule (§ 4 v11.0b) is the only mechanism that could be interpreted two ways — clarified to "single owner at any time, transition at tip-off via `position_owner` column update."

Ready for implementation planning.
