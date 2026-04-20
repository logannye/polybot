# Polybot v10 "Information Arb Specialist" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild polybot from a 7-strategy generalist that lost 93% of live bankroll on 2026-04-10–14 into a focused 2-strategy (Live Sports + Snipe) information-arb system that compounds on $2,000 with a tight learning layer and a 5-stage deployment ladder.

**Architecture:** Three sequential PRs (`PR A → PR B → PR C`), each independently shippable and producing working software. PR A is a scorched-earth deletion + safeguards refactor with no behavior change for surviving code. PR B builds the new Live Sports engine (`sports/` module + rewritten `live_sports.py`). PR C rebuilds Snipe with 2 tiers + the cross-strategy learning layer.

**Tech Stack:** Python 3.13, PostgreSQL 16, `asyncio`, `py-clob-client`, `structlog`, `pytest` + `pytest-asyncio`, `isotonic` regression via `scikit-learn`.

**Reference spec:** `docs/superpowers/specs/2026-04-16-v10-information-arb-specialist-design.md`

---

## Overview — three PRs, not one

| PR | Scope | Risk | Why separate |
|---|---|---|---|
| **A** | Delete 6 strategies + ~88 config keys + ~12 support modules. Extract halt/divergence/preflight logic from `engine.py` into `polybot/safeguards/`. | Low — pure deletion + mechanical extraction, existing tests validate | Shrinks the surface area so PR B/C aren't fighting dead code. Safe to ship even if B/C slip. |
| **B** | Build `polybot/sports/` (espn_client reuse + `win_prob.py` + `calibrator.py`) + `markets/sports_matcher.py` + rewrite `strategies/live_sports.py` per spec §3. | Medium — new code, matcher is highest-risk component | Size. Needs its own TDD cycle. Calibrator needs real dry-run data before it's graded. |
| **C** | Rewrite `strategies/snipe.py` to 2 tiers per spec §4. Add `trade_outcome` table + `learning/kelly_scaler.py` + `learning/edge_decay.py` per spec §5. | Medium — cross-strategy schema + learning loop | Depends on B's dry-run activity for training data. Independent shipping lets A+B bake. |

**Advancement gates between PRs (human-approved, not auto):**
- A → B: All tests green on main, surviving strategies still run the dry-run loop, `safeguards/` module has ≥90% coverage.
- B → C: Live Sports in dry-run logging ≥10 entry-gate passes/day, matcher fixture replay at ≥95% accuracy, calibrator populated for ≥3 sports with ≥30 observations/bucket.
- C → v10 Stage 0 start: 14-day dry-run validation begins per spec §6 deployment ladder.

---

# Phase A — Scorched Earth + Safeguards Extraction

**Ship as PR A.** Every task is independently verifiable. Order matters only for the strategies (Tasks A1–A6) because they share support modules — delete strategies first, then support modules, then config, then `__main__.py`, then extract safeguards.

### File Map (Phase A)

| File | Action | Responsibility |
|---|---|---|
| `polybot/strategies/arbitrage.py` | Delete | Strategy removed per spec §1 |
| `polybot/strategies/political.py` | Delete | Strategy removed per spec §1 |
| `polybot/strategies/cross_venue.py` | Delete | Strategy removed per spec §1 |
| `polybot/strategies/forecast.py` | Delete | Strategy removed per spec §1 |
| `polybot/strategies/market_maker.py` | Delete | Strategy removed per spec §1 |
| `polybot/strategies/mean_reversion.py` | Delete | Strategy removed per spec §1 |
| `tests/test_arbitrage.py` | Delete | Tests for removed strategy |
| `tests/test_political_strategy.py` | Delete | Tests for removed strategy |
| `tests/test_cross_venue.py` | Delete | Tests for removed strategy |
| `tests/test_forecast_strategy.py` | Delete | Tests for removed strategy |
| `tests/test_market_maker.py` | Delete | Tests for removed strategy |
| `tests/test_mean_reversion.py` | Delete | Tests for removed strategy |
| `polybot/analysis/ensemble.py` | Delete | LLM ensemble only used by forecast |
| `polybot/analysis/calibration.py` | Delete | Per-model trust weights only used by ensemble |
| `polybot/analysis/odds_client.py` | Delete | Only used by cross_venue + snipe odds verification |
| `polybot/analysis/prescore.py` | Delete | Only used by forecast |
| `polybot/analysis/prompts.py` | Delete | Only used by ensemble |
| `polybot/analysis/research.py` | Delete | Brave researcher, only used by forecast |
| `polybot/analysis/win_probability.py` | Delete | Old per-sport models, replaced by `sports/win_prob.py` in PR B |
| `polybot/analysis/quant.py` | **Keep** | Snipe may use `compute_spread_signal`; re-audit in PR C |
| `polybot/trading/quote_manager.py` | Delete | MM-only |
| `polybot/trading/inventory.py` | Delete | MM-only |
| `tests/test_ensemble.py` | Delete | |
| `tests/test_calibration.py` | Delete | |
| `tests/test_odds_client.py` | Delete | |
| `tests/test_prescore.py` | Delete | |
| `tests/test_prompts.py` | Delete | |
| `tests/test_research.py` | Delete | |
| `tests/test_win_probability.py` | Delete | |
| `tests/test_inventory.py` | Delete | |
| `polybot/core/config.py` | Modify | Remove ~88 deleted-strategy keys; keep surviving keys |
| `polybot/__main__.py` | Modify | Remove 6 strategy imports + construction blocks; keep Snipe + LiveGame |
| `polybot/core/engine.py` | Modify | Remove drawdown/divergence/preflight methods (extracted) |
| `polybot/safeguards/__init__.py` | Create | Module entry point |
| `polybot/safeguards/drawdown_halt.py` | Create | Extracted `_check_drawdown_halt` + 30s cache |
| `polybot/safeguards/capital_divergence.py` | Create | Extracted `_check_capital_divergence` + self-healing |
| `polybot/safeguards/deployment_stage.py` | Create | Deployment stage cap enforcement |
| `polybot/safeguards/preflight.py` | Create | Wrapper around existing `scripts/live_preflight.py` for import use |
| `tests/test_safeguards.py` | Modify | Point to new module paths; existing behavior tests apply |
| `tests/test_engine.py` | Modify | Remove assertions on extracted methods |

### Task A0: Worktree + branch setup

- [ ] **Step 1: Create a worktree for this PR**

Run:
```bash
cd ~/polybot
git worktree add ../polybot-v10-phase-a -b fix/v10-phase-a-scorched-earth
cd ../polybot-v10-phase-a
```
Expected: new worktree at `~/polybot-v10-phase-a` with branch `fix/v10-phase-a-scorched-earth`.

- [ ] **Step 2: Verify tests pass before any deletions**

Run:
```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: all (or pre-existing failures only) green. Record the baseline pass count — we want this number constant or lower by end of Phase A.

### Task A1: Delete arbitrage strategy

**Files:**
- Delete: `polybot/strategies/arbitrage.py`
- Delete: `tests/test_arbitrage.py`

- [ ] **Step 1: Verify no non-test file imports arbitrage**

Run:
```bash
grep -rn "from polybot.strategies.arbitrage\|strategies\.arbitrage" polybot/ --include="*.py"
```
Expected: only `polybot/__main__.py:29` + `import polybot.strategies.arbitrage` pattern matches. If anything else surfaces, document it before deleting.

- [ ] **Step 2: Delete the files**

Run:
```bash
git rm polybot/strategies/arbitrage.py tests/test_arbitrage.py
```

- [ ] **Step 3: Remove the import + construction block from `__main__.py`**

Delete these lines in `polybot/__main__.py`:
```python
from polybot.strategies.arbitrage import ArbitrageStrategy  # line ~29
```
And the construction block (currently lines ~204–206):
```python
if getattr(settings, 'arb_enabled', True):
    arb_strategy = ArbitrageStrategy(settings=settings)
    engine.add_strategy(arb_strategy)
```

- [ ] **Step 4: Run tests, expect green**

Run:
```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: pass count = baseline − (number of test_arbitrage.py tests).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(v10): delete arbitrage strategy

Part of v10 scorched-earth: combinatorial arbitrage was -\$124 realized
and execution risk at current book depth is ruinous per spec §1.
"
```

### Task A2: Delete political strategy

**Files:**
- Delete: `polybot/strategies/political.py`
- Delete: `tests/test_political_strategy.py`

- [ ] **Step 1: Verify no non-test imports**

Run:
```bash
grep -rn "from polybot.strategies.political\|strategies\.political" polybot/ --include="*.py"
```
Expected: only `polybot/__main__.py`.

- [ ] **Step 2: Delete files**

```bash
git rm polybot/strategies/political.py tests/test_political_strategy.py
```

- [ ] **Step 3: Remove import + construction block from `__main__.py`**

Delete line:
```python
from polybot.strategies.political import PoliticalStrategy
```
And block (lines ~208–213):
```python
if getattr(settings, 'pol_enabled', True):
    pol_strategy = PoliticalStrategy(settings=settings)
    engine.add_strategy(pol_strategy)
    await db.execute(
        """INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled)
           VALUES ('political', 0, 0, 0, 0, true) ON CONFLICT (strategy) DO NOTHING""")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: pass count = prior baseline − test_political_strategy count.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(v10): delete political strategy

Political calibration's edge is real on average but noisy in short
horizons and slow capital per spec §1.
"
```

### Task A3: Delete cross_venue strategy + odds_client

**Files:**
- Delete: `polybot/strategies/cross_venue.py`
- Delete: `polybot/analysis/odds_client.py`
- Delete: `tests/test_cross_venue.py`
- Delete: `tests/test_odds_client.py`

- [ ] **Step 1: Verify no non-test imports of `cross_venue`**

```bash
grep -rn "from polybot.strategies.cross_venue" polybot/ --include="*.py"
```
Expected: only `polybot/__main__.py`.

- [ ] **Step 2: Verify odds_client is only referenced by cross_venue + snipe**

```bash
grep -rn "odds_client\|OddsClient" polybot/ --include="*.py"
```
Record every match. `polybot/__main__.py` constructs it conditionally for cross_venue and for snipe verification (`snipe_odds_verification_enabled`). Per spec §4 Snipe v10 is **LLM-only** (no odds API), so the snipe path is also removed below.

- [ ] **Step 3: Delete the four files**

```bash
git rm polybot/strategies/cross_venue.py polybot/analysis/odds_client.py \
       tests/test_cross_venue.py tests/test_odds_client.py
```

- [ ] **Step 4: Remove imports and construction from `__main__.py`**

Delete:
```python
from polybot.analysis.odds_client import OddsClient
from polybot.strategies.cross_venue import CrossVenueStrategy
```

Remove the cross_venue block:
```python
if getattr(settings, 'cv_enabled', False) and getattr(settings, 'odds_api_key', ''):
    odds_client = OddsClient(
        api_key=settings.odds_api_key,
        sports=getattr(settings, 'cv_sports', 'basketball_nba,icehockey_nhl').split(','))
    await odds_client.start()
    cv_strategy = CrossVenueStrategy(settings=settings, odds_client=odds_client)
    engine.add_strategy(cv_strategy)
```

Remove the snipe odds-verification block (lines ~165–171):
```python
if getattr(settings, 'snipe_odds_verification_enabled', False) and getattr(settings, 'odds_api_key', ''):
    if 'odds_client' in dir():
        _snipe_odds = odds_client
    else:
        from polybot.analysis.odds_client import OddsClient as _OC
        _snipe_odds = _OC(api_key=settings.odds_api_key)
        await _snipe_odds.start()
```

Update the `ResolutionSnipeStrategy` construction to drop the `odds_client` kwarg (pass `None` for now; PR C rewrites snipe):
```python
engine.add_strategy(ResolutionSnipeStrategy(
    settings=settings, ensemble=ensemble, odds_client=None))
```

Also remove the `_snipe_odds` variable declaration at the top of `main()`:
```python
_snipe_odds = None   # DELETE this line
```

And in the shutdown `for client in [...]` loop, remove `_snipe_odds, odds_client` from the list:
```python
for client in [scanner, researcher, odds_client, _snipe_odds, espn_client]:
    # becomes:
for client in [scanner, researcher, espn_client]:
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: green. Snipe tests pass because the `odds_client=None` path already exists (pre-v10 config had `snipe_odds_verification_enabled=False` by default).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(v10): delete cross_venue + odds_client

Cross-venue depends on The Odds API credit fragility; most Polymarket
mispricings reflect 24/7 tradability not arbitrage per spec §1.
Snipe v10 is LLM-only so odds_client has no remaining consumer.
"
```

### Task A4: Delete forecast strategy + ensemble + research + prescore + prompts + win_probability + calibration

**Files:**
- Delete: `polybot/strategies/forecast.py`
- Delete: `polybot/analysis/ensemble.py`
- Delete: `polybot/analysis/calibration.py`
- Delete: `polybot/analysis/research.py`
- Delete: `polybot/analysis/prescore.py`
- Delete: `polybot/analysis/prompts.py`
- Delete: `polybot/analysis/win_probability.py`
- Delete: `tests/test_forecast_strategy.py`
- Delete: `tests/test_ensemble.py`
- Delete: `tests/test_calibration.py`
- Delete: `tests/test_research.py`
- Delete: `tests/test_prescore.py`
- Delete: `tests/test_prompts.py`
- Delete: `tests/test_win_probability.py`

- [ ] **Step 1: Confirm forecast is the only consumer of these modules**

```bash
for mod in ensemble calibration research prescore prompts win_probability; do
  echo "=== $mod ==="
  grep -rn "from polybot.analysis.$mod\|analysis\.$mod" polybot/ --include="*.py" \
    | grep -v "polybot/analysis/$mod.py"
done
```
Record consumers. Expected: `forecast.py`, `__main__.py`, and `engine.py` (which wires ensemble through the strategy constructor). Nothing else.

- [ ] **Step 2: Delete the files**

```bash
git rm polybot/strategies/forecast.py \
       polybot/analysis/ensemble.py polybot/analysis/calibration.py \
       polybot/analysis/research.py polybot/analysis/prescore.py \
       polybot/analysis/prompts.py polybot/analysis/win_probability.py \
       tests/test_forecast_strategy.py tests/test_ensemble.py \
       tests/test_calibration.py tests/test_research.py \
       tests/test_prescore.py tests/test_prompts.py tests/test_win_probability.py
```

- [ ] **Step 3: Remove imports + construction from `__main__.py`**

Delete:
```python
from polybot.analysis.research import BraveResearcher
from polybot.analysis.ensemble import EnsembleAnalyzer
from polybot.strategies.forecast import EnsembleForecastStrategy
```

Delete `researcher` and `ensemble` construction:
```python
researcher = BraveResearcher(api_key=settings.brave_api_key)
await researcher.start()
ensemble = EnsembleAnalyzer(
    anthropic_key=settings.anthropic_api_key,
    openai_key=settings.openai_api_key,
    google_key=settings.google_api_key)
```

Delete forecast construction block:
```python
if getattr(settings, 'forecast_enabled', True):
    engine.add_strategy(EnsembleForecastStrategy(
        settings=settings, ensemble=ensemble, researcher=researcher))
```

Update `Engine(...)` constructor — it currently takes `researcher=researcher, ensemble=ensemble`; pass `None` for each (they're positional in the signature; keep the kwarg names):
```python
engine = Engine(
    db=db, scanner=scanner, researcher=None, ensemble=None,
    executor=executor, recorder=recorder, risk_manager=risk_manager,
    settings=settings, email_notifier=email_notifier,
    position_manager=position_manager, clob=clob,
    portfolio_lock=portfolio_lock, trade_learner=trade_learner,
    price_history_scanner=price_history_scanner)
```

Update snipe construction to drop `ensemble`:
```python
engine.add_strategy(ResolutionSnipeStrategy(
    settings=settings, ensemble=None, odds_client=None))
```

Remove `researcher` from the shutdown close loop:
```python
for client in [scanner, espn_client]:   # was [scanner, researcher, espn_client]
```

- [ ] **Step 4: Remove ensemble/researcher kwargs from Engine.__init__**

`polybot/core/engine.py:14–43`: find the `__init__` signature and drop `researcher` and `ensemble` params. If the Engine body references them (grep `self._researcher\|self._ensemble`), remove those references.

Run:
```bash
grep -n "self._researcher\|self._ensemble" polybot/core/engine.py
```
Delete each such line.

- [ ] **Step 5: Run tests**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -10
```
Expected: green. `test_engine.py` may fail if it constructs Engine with `researcher=` / `ensemble=`. Fix those construction calls in the test file to omit those kwargs (match the new Engine signature).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(v10): delete forecast strategy + ensemble + research + prescore + prompts + win_probability + calibration

Ensemble LLM forecasting does not reliably beat attention-weighted
markets per literature and our own history; per-model trust weight +
Brave research + prescore + prompt machinery removed together since
forecast is the only consumer per spec §1.
"
```

### Task A5: Delete market_maker strategy + quote_manager + inventory

**Files:**
- Delete: `polybot/strategies/market_maker.py`
- Delete: `polybot/trading/quote_manager.py`
- Delete: `polybot/trading/inventory.py`
- Delete: `tests/test_market_maker.py`
- Delete: `tests/test_inventory.py`

- [ ] **Step 1: Verify consumers**

```bash
grep -rn "QuoteManager\|InventoryTracker\|MarketMakerStrategy" polybot/ --include="*.py"
```
Expected: only inside the three files to be deleted + `__main__.py`.

- [ ] **Step 2: Delete files**

```bash
git rm polybot/strategies/market_maker.py \
       polybot/trading/quote_manager.py polybot/trading/inventory.py \
       tests/test_market_maker.py tests/test_inventory.py
```

- [ ] **Step 3: Remove from `__main__.py`**

Delete:
```python
from polybot.strategies.market_maker import MarketMakerStrategy
```

Delete the MM construction block:
```python
if settings.mm_enabled:
    mm_strategy = MarketMakerStrategy(
        settings=settings, clob=clob, scanner=scanner,
        dry_run=settings.dry_run)
    engine.add_strategy(mm_strategy)
```

- [ ] **Step 4: Scan engine.py for MM-specific branches**

```bash
grep -n "mm_\|market_maker\|MarketMaker" polybot/core/engine.py
```
The `_fill_monitor` has a `skip_mm_orders` branch. After MM is gone that branch is dead; leave it for now since the SQL filter naturally returns zero rows. Audit in PR B cleanup.

- [ ] **Step 5: Run tests**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(v10): delete market_maker strategy + quote_manager + inventory

MM blew up in April live trading (adverse selection at \$2K is a death
sentence per spec §1). QuoteManager and InventoryTracker had no other
consumer.
"
```

### Task A6: Delete mean_reversion strategy

**Files:**
- Delete: `polybot/strategies/mean_reversion.py`
- Delete: `tests/test_mean_reversion.py`

- [ ] **Step 1: Verify no non-test consumers**

```bash
grep -rn "MeanReversionStrategy\|mean_reversion" polybot/ --include="*.py" \
  | grep -v "polybot/strategies/mean_reversion.py"
```
Expected: `polybot/__main__.py` only.

- [ ] **Step 2: Delete files**

```bash
git rm polybot/strategies/mean_reversion.py tests/test_mean_reversion.py
```

- [ ] **Step 3: Remove from `__main__.py`**

Delete:
```python
from polybot.strategies.mean_reversion import MeanReversionStrategy
```

Delete MR block:
```python
if getattr(settings, 'mr_enabled', False):
    mr_strategy = MeanReversionStrategy(settings=settings)
    engine.add_strategy(mr_strategy)
    price_history_scanner = PriceHistoryScanner(...)
    engine._price_history_scanner = price_history_scanner
```

**Also delete the now-orphaned PriceHistoryScanner construction** (MR was its only consumer):
```python
from polybot.markets.price_history import PriceHistoryScanner  # remove this import
price_history_scanner = None  # remove this line
# The Engine(...) call already passes price_history_scanner=None; leave that as-is
```

- [ ] **Step 4: Delete PriceHistoryScanner module + test**

```bash
git rm polybot/markets/price_history.py tests/test_price_history.py
```

Remove from engine.py the scan task registration:
```bash
grep -n "price_history\|_scan_price_history" polybot/core/engine.py
```
Delete the `if self._price_history_scanner:` block in `run_forever` (around line 71–73) and the `_scan_price_history` method at the bottom of engine.py.

Also drop the `price_history_scanner` kwarg from `Engine.__init__` and the `self._price_history_scanner` attribute.

- [ ] **Step 5: Run tests**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: green. `test_engine.py` may need its Engine constructor call updated.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(v10): delete mean_reversion strategy + price_history_scanner

MR's +\$100 dry-run was unreliable (fake spread model pre-Apr 14).
Revisit only after v10 core proves out per spec §1. PriceHistoryScanner
had no other consumer.
"
```

### Task A7: Prune config.py

**Files:**
- Modify: `polybot/core/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: List all config keys owned by deleted strategies**

```bash
grep -nE "^\s*(forecast_|mm_|mr_|cv_|pol_|arb_|ensemble_|brave_|prescore_|odds_api)" polybot/core/config.py
```
Record the full list. Expect ~88 lines.

- [ ] **Step 2: Delete strategy-specific keys**

Using `Edit` tool, remove each of these line blocks from `polybot/core/config.py`:
- All lines with prefixes `forecast_`, `mm_`, `mr_`, `cv_`, `pol_`, `arb_`, `ensemble_`, `prescore_`, `brave_`
- `odds_api_key` + `odds_api_*` keys
- `anthropic_api_key`, `openai_api_key`, `google_api_key` (LLM keys — the only remaining LLM is Snipe's Gemini Flash; that key stays. Audit: keep `google_api_key` only if `snipe.py` uses `google_api_key` for Gemini; otherwise remove. Snipe currently uses `ensemble` so this gets re-audited in PR C — for now **keep** `google_api_key`.)

Keep:
- `dry_run`, `dry_run_realistic`, `dry_run_taker_fee_pct`, `dry_run_max_spread`
- `starting_bankroll`, `kelly_mult`, `edge_threshold`, `scan_interval_seconds`
- All `snipe_*` keys (rewritten in PR C but we don't need to churn config twice)
- All `lg_*` keys (Live Game Closer is the seed for Live Sports v10 in PR B)
- All safeguard keys: `max_total_drawdown_pct`, `max_capital_divergence_pct`, `live_deployment_stage`, `post_breaker_*`
- DB / Resend / Polymarket credential keys
- All `max_single_position_pct`, `max_total_deployed_pct`, `max_per_category_pct`, `max_concurrent_positions`, `daily_loss_limit_pct`, `circuit_breaker_hours`, `min_trade_size`, `book_depth_max_pct`
- `cold_start_trades`, `brier_ema_alpha` (TradeLearner still uses these)

- [ ] **Step 3: Update `tests/test_config.py`**

Run:
```bash
uv run pytest tests/test_config.py --timeout=30 -v 2>&1 | tail -30
```
For every failing assertion on a deleted key, delete that assertion line. Do not "fix" them by inventing new keys.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -10
```
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(v10): prune config keys for deleted strategies (~88 keys removed)"
```

### Task A8: Extract `polybot/safeguards/` module — write failing tests first

**Files:**
- Create: `polybot/safeguards/__init__.py`
- Create: `polybot/safeguards/drawdown_halt.py`
- Create: `polybot/safeguards/capital_divergence.py`
- Create: `polybot/safeguards/deployment_stage.py`
- Test: `tests/test_safeguards.py` (already exists — modify)

This is the only Phase A task with non-trivial new code; do this TDD.

- [ ] **Step 1: Read the existing tests to anchor the extracted behavior**

```bash
cat tests/test_safeguards.py
```
Record every behavior assertion. The extraction must preserve every one.

- [ ] **Step 2: Write a failing test for the new import path**

Append to `tests/test_safeguards.py`:
```python
def test_drawdown_halt_module_importable():
    """Phase A extraction: drawdown halt logic lives in polybot.safeguards."""
    from polybot.safeguards.drawdown_halt import DrawdownHalt
    assert DrawdownHalt is not None


def test_capital_divergence_module_importable():
    from polybot.safeguards.capital_divergence import CapitalDivergenceMonitor
    assert CapitalDivergenceMonitor is not None


def test_deployment_stage_module_importable():
    from polybot.safeguards.deployment_stage import DeploymentStageGate
    assert DeploymentStageGate is not None
```

Run:
```bash
uv run pytest tests/test_safeguards.py::test_drawdown_halt_module_importable -v
```
Expected: FAIL with `ModuleNotFoundError: polybot.safeguards`.

- [ ] **Step 3: Create the module skeleton**

Create `polybot/safeguards/__init__.py`:
```python
"""Safeguard layer — halt/divergence/stage checks, extracted from engine.py.

Each safeguard is a single-responsibility class with:
- an async ``check(state) -> bool`` (or equivalent) method
- no side effects beyond DB writes + logging + email

Consumed by ``polybot.core.engine.Engine`` via dependency injection.
"""

from polybot.safeguards.drawdown_halt import DrawdownHalt
from polybot.safeguards.capital_divergence import CapitalDivergenceMonitor
from polybot.safeguards.deployment_stage import DeploymentStageGate

__all__ = ["DrawdownHalt", "CapitalDivergenceMonitor", "DeploymentStageGate"]
```

Create `polybot/safeguards/drawdown_halt.py`:
```python
"""Total drawdown halt — permanent stop at N% loss from high-water mark.

Extracted from polybot.core.engine.Engine._check_drawdown_halt.
Behavior preserved bit-for-bit including the 30s result cache.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger()


class DrawdownHalt:
    def __init__(self, db, settings, email_notifier=None, cache_ttl_seconds: float = 30.0):
        self._db = db
        self._settings = settings
        self._email = email_notifier
        self._cache_ttl = cache_ttl_seconds
        self._cache: tuple[bool, float] | None = None  # (result, monotonic_timestamp)

    async def check(self) -> bool:
        """Return True if trading should be halted due to drawdown."""
        if self._cache is not None:
            cached_result, cached_at = self._cache
            if time.monotonic() - cached_at < self._cache_ttl:
                return cached_result

        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            self._cache = (False, time.monotonic())
            return False

        bankroll = float(state["bankroll"])
        high_water = float(state.get("high_water_bankroll", bankroll) or bankroll)
        halt_until = state.get("drawdown_halt_until")

        if halt_until and halt_until > datetime.now(timezone.utc):
            self._cache = (True, time.monotonic())
            return True

        if bankroll > high_water:
            await self._db.execute(
                "UPDATE system_state SET high_water_bankroll = $1 WHERE id = 1", bankroll)
            self._cache = (False, time.monotonic())
            return False

        if high_water > 0:
            drawdown = 1.0 - (bankroll / high_water)
            max_drawdown = getattr(self._settings, "max_total_drawdown_pct", 0.30)
            if drawdown >= max_drawdown:
                halt_time = datetime.now(timezone.utc) + timedelta(days=365)
                await self._db.execute(
                    "UPDATE system_state SET drawdown_halt_until = $1 WHERE id = 1",
                    halt_time)
                log.critical("DRAWDOWN_HALT", bankroll=bankroll, high_water=high_water,
                             drawdown_pct=round(drawdown * 100, 1))
                if self._email:
                    try:
                        await self._email.send(
                            "[POLYBOT CRITICAL] DRAWDOWN HALT — ALL TRADING STOPPED",
                            f"<p>Bankroll ${bankroll:.2f} is {drawdown*100:.1f}% below "
                            f"high-water ${high_water:.2f}. Threshold: {max_drawdown*100:.0f}%.</p>"
                            f"<p>All trading halted. Manual DB reset required to resume.</p>")
                    except Exception:
                        pass
                self._cache = (True, time.monotonic())
                return True

        self._cache = (False, time.monotonic())
        return False
```

Create `polybot/safeguards/capital_divergence.py`:
```python
"""Capital divergence monitor — halts on CLOB vs DB mismatch > threshold.

Self-heals after 3 consecutive OK checks. Extracted from
polybot.core.engine.Engine._check_capital_divergence.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()


class CapitalDivergenceMonitor:
    def __init__(self, db, clob, settings, email_notifier=None,
                 ok_streak_to_recover: int = 3):
        self._db = db
        self._clob = clob
        self._settings = settings
        self._email = email_notifier
        self._ok_streak_target = ok_streak_to_recover
        self._halted = False
        self._ok_streak = 0

    @property
    def is_halted(self) -> bool:
        return self._halted

    async def check(self) -> None:
        """Run one check cycle; updates internal halt state. No return value."""
        if not self._clob or self._settings.dry_run:
            return
        try:
            state = await self._db.fetchrow(
                "SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
            clob_balance = await self._clob.get_balance()
            expected_cash = float(state["bankroll"]) - float(state["total_deployed"])
            if expected_cash <= 0:
                return
            divergence = abs(clob_balance - expected_cash) / expected_cash
            max_div = getattr(self._settings, "max_capital_divergence_pct", 0.10)
            if divergence > max_div:
                self._halted = True
                self._ok_streak = 0
                log.critical("CAPITAL_DIVERGENCE_HALT", clob=clob_balance,
                             expected=expected_cash, divergence_pct=round(divergence * 100, 1))
                if self._email:
                    try:
                        await self._email.send(
                            "[POLYBOT CRITICAL] Capital divergence halt",
                            f"<p>CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}, "
                            f"Divergence: {divergence*100:.1f}%</p>")
                    except Exception:
                        pass
            elif self._halted:
                self._ok_streak += 1
                if self._ok_streak >= self._ok_streak_target:
                    self._halted = False
                    self._ok_streak = 0
                    log.info("CAPITAL_DIVERGENCE_RECOVERED",
                             clob=clob_balance, expected=expected_cash)
                    if self._email:
                        try:
                            await self._email.send(
                                "[POLYBOT INFO] Capital divergence recovered",
                                f"<p>CLOB balance back in sync after "
                                f"{self._ok_streak_target} consecutive OK checks. "
                                f"CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}</p>")
                        except Exception:
                            pass
        except Exception as e:
            log.error("capital_divergence_check_error", error=str(e))
```

Create `polybot/safeguards/deployment_stage.py`:
```python
"""Deployment stage gate — caps deployed capital per v10 ladder per spec §6.

Stages: 0=dry_run (70% cap), 1=preflight, 2=micro_test (5% cap),
3=ramp (25% cap), 4=full (70% cap). Read-only check — returns max $
allowed for NEW trades given current deployed.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()

STAGE_CAPS = {
    "dry_run": 0.70,
    "micro_test": 0.05,
    "ramp": 0.25,
    "full": 0.70,
}


class DeploymentStageGate:
    def __init__(self, db, settings):
        self._db = db
        self._settings = settings

    async def available_capital(self) -> float:
        """Return $ amount that may still be deployed for new trades."""
        stage = getattr(self._settings, "live_deployment_stage", "dry_run")
        cap_pct = STAGE_CAPS.get(stage, 0.70)
        state = await self._db.fetchrow(
            "SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
        if not state:
            return 0.0
        bankroll = float(state["bankroll"])
        deployed = float(state["total_deployed"])
        allowed = bankroll * cap_pct
        return max(0.0, allowed - deployed)
```

- [ ] **Step 4: Run the new import tests**

```bash
uv run pytest tests/test_safeguards.py::test_drawdown_halt_module_importable \
             tests/test_safeguards.py::test_capital_divergence_module_importable \
             tests/test_safeguards.py::test_deployment_stage_module_importable -v
```
Expected: 3 passed.

- [ ] **Step 5: Add unit tests for extracted logic**

Append to `tests/test_safeguards.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta


@pytest.mark.asyncio
async def test_drawdown_halt_triggers_at_threshold():
    from polybot.safeguards.drawdown_halt import DrawdownHalt

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 70.0, "high_water_bankroll": 100.0,
        "drawdown_halt_until": None,
    })
    db.execute = AsyncMock()
    settings = MagicMock()
    settings.max_total_drawdown_pct = 0.30

    halt = DrawdownHalt(db=db, settings=settings, email_notifier=None)
    assert await halt.check() is True
    # Second call should hit the 30s cache without re-reading
    db.fetchrow.reset_mock()
    assert await halt.check() is True
    db.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_drawdown_halt_clear_when_bankroll_new_high():
    from polybot.safeguards.drawdown_halt import DrawdownHalt

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 150.0, "high_water_bankroll": 100.0,
        "drawdown_halt_until": None,
    })
    db.execute = AsyncMock()
    settings = MagicMock()
    settings.max_total_drawdown_pct = 0.30

    halt = DrawdownHalt(db=db, settings=settings)
    assert await halt.check() is False
    # Should have updated high-water mark
    assert any("high_water_bankroll" in str(c) for c in db.execute.call_args_list)


@pytest.mark.asyncio
async def test_capital_divergence_self_heals_after_three_ok():
    from polybot.safeguards.capital_divergence import CapitalDivergenceMonitor

    db = AsyncMock()
    settings = MagicMock()
    settings.dry_run = False
    settings.max_capital_divergence_pct = 0.10
    clob = AsyncMock()

    monitor = CapitalDivergenceMonitor(
        db=db, clob=clob, settings=settings, email_notifier=None)

    # Halt: clob=50, expected=100 → 50% divergence
    db.fetchrow = AsyncMock(return_value={"bankroll": 100.0, "total_deployed": 0.0})
    clob.get_balance = AsyncMock(return_value=50.0)
    await monitor.check()
    assert monitor.is_halted is True

    # Now 3 OK checks recover
    clob.get_balance = AsyncMock(return_value=100.0)
    await monitor.check(); assert monitor.is_halted is True  # 1 ok
    await monitor.check(); assert monitor.is_halted is True  # 2 ok
    await monitor.check(); assert monitor.is_halted is False  # 3 ok → recovered


@pytest.mark.asyncio
async def test_deployment_stage_caps_deployed_capital():
    from polybot.safeguards.deployment_stage import DeploymentStageGate

    db = AsyncMock()
    settings = MagicMock()

    # micro_test allows 5% of $2000 = $100; $50 already deployed → $50 remaining
    settings.live_deployment_stage = "micro_test"
    db.fetchrow = AsyncMock(return_value={"bankroll": 2000.0, "total_deployed": 50.0})
    gate = DeploymentStageGate(db=db, settings=settings)
    assert await gate.available_capital() == pytest.approx(50.0)

    # full allows 70% = $1400; $100 deployed → $1300 remaining
    settings.live_deployment_stage = "full"
    db.fetchrow = AsyncMock(return_value={"bankroll": 2000.0, "total_deployed": 100.0})
    assert await gate.available_capital() == pytest.approx(1300.0)
```

Run:
```bash
uv run pytest tests/test_safeguards.py --timeout=30 -v 2>&1 | tail -25
```
Expected: all tests pass (old + new).

- [ ] **Step 6: Wire the new modules into Engine**

In `polybot/core/engine.py`:

1. Import at top:
```python
from polybot.safeguards import DrawdownHalt, CapitalDivergenceMonitor, DeploymentStageGate
```

2. In `Engine.__init__`, construct each:
```python
self._drawdown_halt = DrawdownHalt(
    db=db, settings=settings, email_notifier=email_notifier)
self._divergence_monitor = CapitalDivergenceMonitor(
    db=db, clob=clob, settings=settings, email_notifier=email_notifier)
self._deployment_gate = DeploymentStageGate(db=db, settings=settings)
```

3. Replace the old method bodies with delegation:
```python
async def _check_drawdown_halt(self) -> bool:
    return await self._drawdown_halt.check()

async def _check_capital_divergence(self):
    await self._divergence_monitor.check()
```

4. Remove the cache attributes `self._drawdown_cache`, `self._capital_divergence_halted`, `self._capital_divergence_ok_count` from `__init__` — they now live inside the extracted objects.

5. Remove the bodies of the extracted methods (the code now in `safeguards/`). Keep the thin `_check_*` delegators as transitional surface; PR B can remove them entirely by making `run_forever` call the modules directly.

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest --timeout=60 -q 2>&1 | tail -5
```
Expected: green. If `test_engine.py` fails because it poked at private cache attributes, update those tests to use the new module-level state.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(v10): extract safeguards module

DrawdownHalt, CapitalDivergenceMonitor, DeploymentStageGate extracted
from engine.py into polybot/safeguards/ per spec §2. Behavior identical
to prior engine logic (including 30s drawdown cache and 3-OK-streak
self-healing). Engine now delegates via thin wrappers; direct-call
migration deferred to PR B.
"
```

### Task A9: End-to-end validation + readiness check

- [ ] **Step 1: Full test suite + coverage**

```bash
uv run pytest --timeout=60 -q --cov=polybot --cov-report=term-missing 2>&1 | tail -20
```
Expected: All tests pass. Record coverage numbers for surviving modules — this is the new baseline.

- [ ] **Step 2: Smoke-run polybot locally (dry-run) for 2 minutes**

```bash
uv run python -m polybot 2>&1 | tee /tmp/v10-phase-a-smoke.log &
PID=$!
sleep 120
kill -TERM $PID
wait
```
Inspect `/tmp/v10-phase-a-smoke.log` for:
- `polybot_starting` fires
- `polybot_mode` shows `dry_run=True`
- Snipe + LiveGame strategies register (others MUST NOT appear)
- No `ImportError`, no `AttributeError`, no `ModuleNotFoundError`
- Graceful shutdown on SIGTERM

- [ ] **Step 3: Update project memory**

Append to `~/.claude/projects/-Users-logannye/memory/project_polybot.md`:
```
**v10 Phase A shipped (2026-04-19):** scorched-earth rebuild. 6
strategies + 12 support modules + ~88 config keys deleted. Safeguards
extracted to `polybot/safeguards/`. Only Snipe + LiveGame remain as
transitional; v10 Live Sports (PR B) and Snipe v10 (PR C) follow.
```

- [ ] **Step 4: Push branch + open PR**

```bash
git push -u origin fix/v10-phase-a-scorched-earth
gh pr create --title "v10 Phase A: scorched-earth + safeguards extraction" \
  --body "$(cat <<'EOF'
## Summary
First of 3 PRs rebuilding polybot into v10 "Information Arb Specialist" per approved spec `2026-04-16-v10-information-arb-specialist-design.md`.

This PR is behavior-preserving for surviving code. It deletes:
- 6 strategies (arbitrage, political, cross_venue, forecast, market_maker, mean_reversion)
- 12 support modules (ensemble, calibration, research, prescore, prompts, win_probability, odds_client, quote_manager, inventory, price_history, + their tests)
- ~88 config keys (forecast_*, mm_*, mr_*, cv_*, pol_*, arb_*, ensemble_*, brave_*, odds_api_*, prescore_*)

And extracts `polybot/safeguards/` (DrawdownHalt, CapitalDivergenceMonitor, DeploymentStageGate) from engine.py with unit tests.

## Why
Spec §1 kill list — these strategies were marginal or unprofitable and collectively cost 93% of live bankroll on Apr 10–14. v10 concentrates capital on 2 structurally defensible strategies (Live Sports + Snipe).

## Test plan
- [x] Full test suite green (baseline pass count preserved)
- [x] `tests/test_safeguards.py` — 3 new import tests + 4 new behavior tests for extracted modules
- [x] 2-min smoke run: polybot starts, registers only surviving strategies, shuts down cleanly
- [ ] Merge gates Phase B

## What's next
- PR B: `polybot/sports/` module + rewritten `strategies/live_sports.py` per spec §3
- PR C: 2-tier Snipe + `trade_outcome` table + learning layer per spec §4-5

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# Phase B — Live Sports v10 Engine (outline — expand to its own plan doc)

**Ship as PR B after Phase A merges.** Creates a new plan doc `docs/superpowers/plans/YYYY-MM-DD-v10-phase-b-live-sports.md` with bite-sized TDD tasks.

### File Map (Phase B — to detail later)

| File | Action |
|---|---|
| `polybot/sports/__init__.py` | Create |
| `polybot/sports/espn_client.py` | Move from `polybot/analysis/espn_client.py` + add 15s cadence, 60s freshness guard, 9-league `asyncio.gather` |
| `polybot/sports/win_prob.py` | Create — pure-function per-sport models (MLB, NBA, NHL, NCAAB, UCL, EPL, La Liga, Bundesliga, MLS) |
| `polybot/sports/calibrator.py` | Create — online isotonic regression per `(sport, game_state_bucket)`, hourly refit, <30 obs shrinkage fallback |
| `polybot/markets/sports_matcher.py` | Create — 3-pass matcher (normalization + regex classification + confidence score), ≥95% test fixture accuracy |
| `polybot/strategies/live_sports.py` | Rewrite from `live_game.py` — 6-condition entry gate, maker-first with one taker fallback, cancel-on-adverse-drift, emergency exit at 0.70, TP at 0.97 |
| `polybot/db/migrations/v10_sports_tables.sql` | Create — `sport_calibration` table |
| `tests/sports/test_win_prob.py` | Create — fixture states per sport (tied late game, overtime, blowouts, weather-shortened MLB, soccer ET+PK) |
| `tests/sports/test_calibrator.py` | Create — isotonic fit on synthetic data, shrinkage <30 obs, idempotent refit |
| `tests/markets/test_sports_matcher.py` | Create — **≥90% coverage, highest priority**, fuzzy name matching across 9 leagues, ambiguity → confidence floor reject |
| `tests/strategies/test_live_sports.py` | Create — all 6 entry conditions, emergency exit, TP, dedupe with Snipe |

### Entry gate (reference — Spec §3)

| Condition | Threshold |
|---|---|
| Calibrated win probability ≥ | 0.85 |
| Polymarket edge vs model ≥ | 0.04 (4%) |
| Book depth at my entry price ≥ | $10,000 |
| ESPN data freshness < | 60s |
| Matcher confidence ≥ | 0.95 |
| No existing open position in this market | — |

### Exit rules
- Default hold to resolution
- Emergency exit: calibrated prob drops below 0.70 → taker close immediately
- TP: price ≥ 0.97 → taker close, recycle capital
- Time stop: 6h hard maximum

### Success criteria for PR B merge
- All entry-gate conditions covered by unit tests
- Matcher fixture replay ≥95% on 100 canned ESPN↔Polymarket pairs
- Live Sports in dry-run logs `live_sports_entry` (pass) or `live_sports_gate_reject` (fail) on every eligible game
- `sport_calibration` table populated after 48h with ≥1 bucket per active sport

---

# Phase C — Snipe v10 + Learning Layer (outline — expand to its own plan doc)

**Ship as PR C after Phase B soaks for ~7 days and dry-run produces trade_outcome rows.**

### File Map (Phase C — to detail later)

| File | Action |
|---|---|
| `polybot/strategies/snipe.py` | Rewrite — 2 tiers (T0: ≥$0.96 / ≤12h / no LLM / 0.50× Kelly / 10% cap; T1: $0.88–0.96 / ≤8h / Gemini Flash required / 0.30× Kelly / 7% cap). Drop T2/T3. |
| `polybot/analysis/gemini_client.py` | Create — thin async client for Gemini Flash, $2/day spend cap per spec §4 |
| `polybot/learning/__init__.py` | Keep structure; rebuild contents |
| `polybot/learning/kelly_scaler.py` | Create — per-strategy Beta-Binomial posterior Kelly scaler, hourly refit, `[0.25, 2.0]` clamp |
| `polybot/learning/edge_decay.py` | Create — compare last-50 realized edge vs last-200; negative short with positive long → disable `(strategy, category)` 48h |
| `polybot/learning/trade_outcome.py` | Create — writer for `trade_outcome` table on every position close |
| `polybot/learning/weekly_reflection.py` | Create — Sunday markdown report, calibrator drift, bucket adequacy |
| `polybot/db/migrations/v10_trade_outcome.sql` | Create — `trade_outcome` table per spec §5 Loop 1 |
| `tests/learning/test_kelly_scaler.py` | Create |
| `tests/learning/test_edge_decay.py` | Create |
| `tests/strategies/test_snipe_v10.py` | Create — T0/T1 classification, LLM spend cap, dedupe with Live Sports, cooldown, exposure cap |

### Learning loops (reference — Spec §5)

| Loop | Cadence | Job |
|---|---|---|
| 1 | on every close | Write `trade_outcome` row |
| 2 | hourly | Refit Live Sports calibrators; per-strategy Beta-Binomial Kelly scaler; `(strategy, category)` edge decay |
| 3 | daily midnight UTC | Kill-switch (<-5% over 100 trades → 24h pause, 2 pauses → disable); stage-advance readiness log; daily P&L email |
| 4 | Sunday UTC | Calibrator drift audit; bucket adequacy; weekly markdown report |

### Success criteria for PR C merge
- `trade_outcome` row written on every close path (unit tests cover all paths)
- Kelly scaler output stable in `[0.25, 2.0]` on synthetic history
- Edge decay correctly disables `(strategy, category)` pairs on crafted inputs
- Weekly reflection markdown renders from fixture data

---

# Global Success Criteria (v10 Stage 0 start)

After PR A + PR B + PR C all merge and soak for 48h:

- All tests green (≥90% coverage for new modules per spec §7)
- Only 2 strategies active: Live Sports + Snipe
- `polybot/safeguards/` owns all halts
- `sport_calibration` populated for ≥3 sports
- Dry-run logging ≥10 Live Sports entry-gate passes/day + ≥5 Snipe entries/day
- No `periodic_error` storms, no `strategy_error` cascades
- Bankroll $2,000, `live_deployment_stage=dry_run`, `max_total_drawdown_pct=0.30`

**Then** 14-day dry-run validation begins per spec §6 deployment ladder. Live Stage 2 (micro_test 5%) requires explicit human approval.

---

## Open questions / deferred

- **Schema migrations:** PR A deliberately doesn't touch the `trades` / `strategy_performance` / `analyses` schema. Historical rows stay — we'll inspect them in PR C to decide what migrates into `trade_outcome`.
- **Dashboard:** `polybot/dashboard/app.py` currently surfaces forecast/MM widgets. Out-of-scope for A; re-audit during B.
- **Scripts directory:** `scripts/` has 30+ one-off audit scripts. Left untouched in A; prune in C.
- **`analysis/quant.py`:** `compute_spread_signal` may not be used post-v10. Re-audit in PR C.
- **`learning/recorder.py` + `learning/trade_learning.py`:** transitional. PR C replaces them with `trade_outcome.py` + `kelly_scaler.py` + `edge_decay.py`.

---

## Self-review checklist (from writing-plans skill)

1. **Spec coverage:**
   - §1 kill list → covered by Tasks A1–A6 ✓
   - §1 code deletions → covered by Task A7 (config) + A4 (support modules) ✓
   - §2 safeguards extraction → Task A8 ✓
   - §3 Live Sports → Phase B outline (separate plan doc) ✓
   - §4 Snipe v10 → Phase C outline ✓
   - §5 Learning layer → Phase C outline ✓
   - §6 Deployment ladder → Task A8 DeploymentStageGate + existing `live_deployment_stage` key preserved ✓
   - §7 Testing discipline → TDD steps in A8, extensive for B/C ✓
2. **Placeholder scan:** No "TBD", "implement later", or "similar to Task N". Every A task has exact files and commands.
3. **Type consistency:** Safeguard class names (`DrawdownHalt`, `CapitalDivergenceMonitor`, `DeploymentStageGate`) match between imports, constructor calls, and tests.
