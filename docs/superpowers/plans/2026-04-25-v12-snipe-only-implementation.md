# v12 — Snipe-Only Implementation Plan

**Date**: 2026-04-25
**Status**: Proposed
**Supersedes**: v10 (Information Arb Specialist), v11 (Spot Lag Specialist)

## Thesis

Polybot has run for one week post-v10 with **zero closed trades**. The April blowup ($669 → $46) drove a hard spread cap that — correctly — prevents losing money but also prevents *learning anything*. Two concurrent strategies (live_sports + snipe) plus a third just-merged (pregame_sharp) all share this fate: the executor rejects ~100% of entries on `spread_too_wide` or `no_book`, and `trade_outcome` remains empty.

The only Polymarket strategy whose edge **does not depend on tight spreads** is resolution arbitrage: buy at ≥0.96 (or sell ≤0.04) on markets that are mechanically locked, hold to resolution. There is no exit transaction, so the spread tax is paid once and bounded by the entry price. Every other strategy on this codebase requires a round-trip through wide books and dies on the spread.

This plan consolidates polybot to **Snipe T1 only** (LLM-verified resolution-arbitrage), kills T0 and the two sports strategies, and adds the telemetry needed to actually measure edge.

## Success criteria

After 4 weeks of dry-run + micro-test:
- ≥150 closed snipe trades (real or simulated-fill)
- Rolling 50-trade hit rate ≥ 97%
- Brier score on verifier predictions vs. resolutions < 0.03
- Net P&L (after fees + slippage) > 0 in micro_test stage

If any of these miss, halt and re-evaluate. Do not promote to ramp.

## Phased rollout (each phase has a go/no-go gate)

### Phase 1 — Telemetry first (no behavior change) — 1-2 days

The goal is to *observe* before *changing*. Land these as one PR:

**Add**:
- `polybot/learning/shadow_log.py` — records every entry signal regardless of fill
- `polybot/learning/hit_rate_killswitch.py` — rolling-50 hit rate halt (observe-only flag at first)
- New table `shadow_signal` (DDL in §DB Migration)
- Structured Gemini schema response (`{verified: bool, reason: str, confidence: float}`); reject any reason shorter than 30 chars or matching hand-wavy regex (`/seems|likely|probably|possibly/i` without a concrete grounding)

**Modify**:
- `polybot/strategies/snipe.py` — emit `shadow_signal` row on every classify hit (before any rejection)
- `polybot/analysis/gemini_client.py` — JSON-schema response, log full reason

**Don't change yet**: other strategies, executor, deletion of any modules.

**Gate to Phase 2**: shadow_signal table populated for 48h, ≥30 verifier responses logged with structured reasons, killswitch unit-tested.

### Phase 2 — Strategy consolidation (flag-only) — 3-5 days

Disable the other strategies via config flags but **don't delete code yet**. This is reversible.

**Modify**:
- `polybot/core/config.py`: add `snipe_only_mode: bool = False`
- `polybot/core/engine.py`: when `snipe_only_mode`, only register the snipe coroutine; skip live_sports + pregame_sharp registration
- `polybot/strategies/snipe.py`: kill T0 entirely (delete the T0 branch in `classify_snipe`); T1 becomes the only entry path
- `polybot/safeguards/deployment_stage.py`: snipe-only requires the killswitch to be in `enforce` mode (not just observe)

**Don't delete**: any source files. Just stop registering them.

**Run for 7 days** in dry-run with `snipe_only_mode=true`. Monitor:
- N candidates/day passing T1 gate
- Verifier confidence distribution (expect bimodal at high/low)
- Shadow-fill P&L at mid (would-be-EV)
- Killswitch trigger count (should be 0)

**Gate to Phase 3**: ≥30 closed shadow trades, hit rate ≥97%, no killswitch trips, no executor errors.

### Phase 3 — Code deletion — 1-2 days

Only after Phase 2 validates. This is the irreversible step.

**Delete files**:
- `polybot/sports/` (entire dir — 7 files, ~860 lines)
- `polybot/strategies/live_sports.py` (762 lines)
- `polybot/strategies/pregame_sharp.py` (439 lines)
- `polybot/markets/sports_matcher.py` (432 lines)
- `polybot/learning/edge_decay.py` (72 lines)
- `polybot/learning/kelly_scaler.py` (89 lines) — replace with static 0.25 in `polybot/trading/kelly.py`
- `polybot/learning/learning_cycle.py` (173 lines) — replaced by `hit_rate_killswitch.py`
- `polybot/learning/calibration.py` (26 lines)
- `polybot/learning/categories.py` (16 lines)
- `polybot/learning/self_assess.py` (47 lines)
- `polybot/learning/trade_learning.py` (326 lines)
- `polybot/analysis/quant.py` (61 lines, unused)
- `scripts/measure_crypto_lag.py` (v11 lag-measurement, orthogonal to snipe)

**Total deleted**: ~3,400 lines.

**Strip from `polybot/core/config.py`** (~25 keys):
- All `live_sports_*`, `pregame_*`, `sport_*`, `espn_*`, `kelly_scaler_*`, `edge_decay_*`, `calibrator_*`, `crypto_lag_*` keys
- Remove `snipe_only_mode` flag (becomes the only mode)
- Remove all T0 keys (`snipe_t0_*`)
- Add `min_verifier_confidence: float = 0.95` and `min_verifier_reason_chars: int = 30`

**Strip from `polybot/core/engine.py`**:
- Strategy registration is now `engine.add_strategy(SnipeStrategy(...))` — single line
- `_hourly_kelly_edge_adjust` → delete (Kelly is now static)
- `_hourly_learning` → reduce to a 5-line "refresh hit rate gauge" function

**DB migration** (additive only, no drops):
- Add `shadow_signal` table
- Add columns `verifier_confidence numeric(4,3)`, `verifier_reason text` to `trade_outcome`
- Pre-v10 strategy rows in `strategy_performance` stay (read-only history)

**Tests**:
- Unit: `test_snipe_t1_classify` (valid + edge cases)
- Unit: `test_gemini_schema_validation` (rejects hand-wavy reasons)
- Unit: `test_hit_rate_killswitch` (trips at <97% over 50 window)
- Unit: `test_shadow_log_idempotent` (same signal logged once per scan)
- Integration: `test_snipe_e2e_dry_run` (signal → verify → shadow_log → executor → resolution → trade_outcome)
- Property: hit-rate floor invariant under any sequence of {win, loss}

**Gate to Phase 4**: full test suite passes, dry-run resumed for 24h with no errors.

### Phase 4 — Promotion — gated by hit-rate evidence

Use existing `DeploymentStageGate` ladder. Each promotion requires the rolling-50 hit rate to hold above 97%, **measured on real fills**, not shadow:

| Stage | Cap | Min trades to promote | Min hit rate |
|---|---|---|---|
| `dry_run` | 70% (simulated) | ∞ (telemetry only) | n/a |
| `micro_test` | 5% real | 30 closed | ≥97% |
| `ramp` | 25% real | 100 closed | ≥97% |
| `full` | 70% real | n/a (steady state) | ≥97% rolling |

Stage transitions are **manual** (operator runs a script that asserts the gates). No auto-promotion. Demotion is automatic on killswitch trip.

## What the system does (post-migration)

Six modules, one process, one strategy:

```
[Scanner] → [Filter] → [LLM Verifier] → [Sizer] → [Maker Executor] → [Resolution Logger]
   30s        sync        Gemini         Kelly      post_only         on resolution
                                                                            ↓
                                                                     [Hit-Rate Killswitch]
```

**Scanner** (existing `markets/scanner.py`, simplified): Polymarket Gamma `/events` every 30s. Keep markets where `time_to_resolution ∈ [5min, 12h]` AND `(yes_price ≥ 0.96 OR yes_price ≤ 0.04)`.

**Filter**: drop if (a) position already open in this market, (b) book depth on entry side < $1K, (c) we're past the daily Gemini spend cap.

**LLM Verifier** (existing `analysis/gemini_client.py`, hardened): structured response. Reject if `confidence < 0.95` or reason fails grounding regex.

**Sizer**: 0.25× Kelly, hard cap 5% of bankroll per trade, hard cap 20% bankroll deployed across all open positions. Kelly inputs are `(verified_p, buy_price)`; no calibrator, no scaler.

**Maker Executor** (existing `trading/executor.py`): post limit at `min(buy_price, best_ask - 1 tick)`. `post_only=True`. Cancel if unfilled in 60s.

**Resolution Logger** (existing `learning/recorder.py` + new `trade_outcome` columns): on resolution, write outcome → update rolling hit rate gauge.

**Hit-Rate Killswitch**: rolling-50 window. If hit rate < 97%, halt all entries; demote deployment stage by one. Persists in `system_state` so a restart doesn't unhalt.

## DB migration

```sql
-- Phase 1: shadow signal log (additive)
CREATE TABLE shadow_signal (
    id              serial PRIMARY KEY,
    polymarket_id   text NOT NULL,
    yes_price       numeric(5,4) NOT NULL,
    hours_remaining numeric(8,2) NOT NULL,
    side            text NOT NULL CHECK (side IN ('YES','NO')),
    buy_price       numeric(5,4) NOT NULL,
    verifier_verdict text,
    verifier_confidence numeric(4,3),
    verifier_reason text,
    passed_filter   boolean NOT NULL,
    fill_attempted  boolean NOT NULL DEFAULT false,
    filled          boolean NOT NULL DEFAULT false,
    reject_reason   text,
    resolved_outcome smallint,           -- 0/1/null until market resolves
    hypothetical_pnl numeric(12,4),      -- P&L if we'd filled at mid
    realized_pnl    numeric(12,4),       -- only set if filled=true
    signaled_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz
);
CREATE INDEX idx_shadow_signal_resolved ON shadow_signal(resolved_at) WHERE resolved_at IS NOT NULL;
CREATE INDEX idx_shadow_signal_polymarket ON shadow_signal(polymarket_id, signaled_at DESC);

-- Phase 3: enrich trade_outcome
ALTER TABLE trade_outcome
  ADD COLUMN verifier_confidence numeric(4,3),
  ADD COLUMN verifier_reason text;

-- Phase 3: hit-rate gauge persisted in system_state
ALTER TABLE system_state
  ADD COLUMN rolling_hit_rate numeric(5,4),
  ADD COLUMN rolling_hit_rate_n integer NOT NULL DEFAULT 0,
  ADD COLUMN killswitch_tripped_at timestamptz;
```

No drops. Old strategy rows in `strategy_performance` stay for historical reference.

## Rollback plan

Each phase is independently revertible:

- **Phase 1**: pure additive (new table, new module, schema change to verifier output). Revert = drop table + delete files.
- **Phase 2**: flag-only. Revert = set `snipe_only_mode=false`. Other strategies still register and run.
- **Phase 3**: code deleted. Revert = `git revert` the deletion commit. DB migrations stay (additive).
- **Phase 4**: stage demotion is one DB write (`live_deployment_stage`). Killswitch trip is auto-demote.

The killswitch is the always-on safety net. Even if the entire migration is wrong, a 50-trade window of <97% hit rate halts the bot before drawdown gets large.

## Risk register

1. **97% hit rate is hard.** If real-world hit rate is 94%, we lose money. Mitigation: start in dry-run + shadow_log for 7 days. If shadow hit rate < 97%, abort migration before Phase 3.
2. **Verifier hallucinations.** Gemini might confidently affirm a non-locked market. Mitigation: structured grounding regex, manual review of the first 50 verifier reasons before Phase 4.
3. **Capacity ceiling.** If Polymarket only has ~5 truly-locked markets/day, $2K bankroll bottoms out fast on per-trade caps. Mitigation: the strategy's purpose is *learning*, not scaling. We can revisit capacity after we've proven edge.
4. **Polymarket API changes.** Gamma `/events` response shape changes break the scanner. Mitigation: existing test coverage on scanner; failure is loud (no candidates) not silent (bad fills).
5. **Fee structure.** Polymarket charges fees on fills. At 2% per-trade EV, fees > 1% would erase edge. Mitigation: `trading/fees.py` already models this; verify in Phase 1 telemetry.

## What we explicitly choose to NOT build

- Per-market calibration. T1 is a binary verifier, not a probability estimator.
- Multi-strategy capital allocation. One strategy, period.
- Inventory management. We hold to resolution, no inventory.
- Sport-specific models. Snipe is sport-agnostic.
- Pregame sharp arb. Punted indefinitely; revisit only after snipe is proven and capacity-constrained.
- Live game closer. Same.
- Crypto lag measurement. Orthogonal — kept as a script if useful, not a strategy.

## File diff summary

| Action | Count | LOC change |
|---|---|---|
| Delete | 13 files | −3,400 |
| Modify | 6 files | ~+200 / −800 |
| Add | 4 files (shadow_log, killswitch, 1 migration, 1 spec) | +500 |
| **Net** | | **~−3,500 LOC** |

Final codebase: ~4,100 LOC, well below pre-v10 size.

## Out of scope for this plan

- Re-enabling sports strategies (a future plan, gated on snipe being capacity-constrained)
- Pinnacle/Betfair data feed (a future plan, only relevant if we revisit predictive strategies)
- Multi-account / multi-wallet (out of scope until micro_test → ramp transition)
