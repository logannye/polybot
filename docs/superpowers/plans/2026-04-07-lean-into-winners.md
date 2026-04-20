# Lean Into Winners Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase daily profits by amplifying the historically profitable trade profiles (Forecast low-prob YES take-profits, MR mid-range, Snipe volume) while cutting the bleeders (Forecast stop-losses, disabled strategies leaking capital).

**Architecture:** Four config-driven changes + two code filters. All changes use the existing pydantic Settings pattern (`.env` overrides) and the existing position_manager exit logic. No new strategies or architectural changes — just tighter guards on entry and exit for strategies that already have proven profitable profiles.

**Tech Stack:** Python 3.13, pytest, pydantic-settings, asyncpg

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/core/config.py` | Modify | Add new config fields for forecast entry filter + per-strategy stop-loss |
| `polybot/strategies/forecast.py` | Modify | Add YES-side max entry price filter in `_full_analyze_and_trade()` |
| `polybot/strategies/mean_reversion.py` | Modify | Add mid-range price filter (reject entries outside 0.25-0.75) |
| `polybot/trading/position_manager.py` | Modify | Add per-strategy stop-loss thresholds for forecast |
| `.env` | Modify | Set new config values + re-enable forecast + disable bleeders |
| `tests/test_forecast_strategy.py` | Modify | Tests for YES-side entry filter |
| `tests/test_mean_reversion.py` | Modify | Tests for mid-range price filter |
| `tests/test_position_manager.py` | Modify | Tests for per-strategy stop-loss |
| `tests/test_config.py` | Modify | Tests for new config fields |

---

### Task 1: Per-Strategy Stop-Loss — Tighter Forecast Stop-Loss

The data shows Forecast stop-losses average -$5.31 on ~$15 positions (35% loss). The global `stop_loss_threshold` is 0.15 (15%). Forecast needs a tighter stop at 0.10 (10%) to cut losses ~33% while still giving room for normal volatility. This is the highest-impact single change.

**Files:**
- Modify: `polybot/core/config.py:93` (add `forecast_stop_loss_threshold`)
- Modify: `polybot/trading/position_manager.py:283-294` (use per-strategy threshold)
- Test: `tests/test_position_manager.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test for per-strategy stop-loss lookup**

Add to `tests/test_position_manager.py`:

```python
class TestPerStrategyStopLoss:
    def test_forecast_uses_tighter_stop(self):
        """Forecast should use its own stop-loss threshold (0.10), not global (0.15)."""
        # YES entry at 0.50, price drops to 0.44 → -12% unrealized
        # Global 0.15 threshold: would NOT trigger (12% < 15%)
        # Forecast 0.10 threshold: SHOULD trigger (12% > 10%)
        assert should_cut_loss("YES", 0.50, 0.44, threshold=0.10) is True
        assert should_cut_loss("YES", 0.50, 0.44, threshold=0.15) is False

    def test_non_forecast_uses_global_stop(self):
        """Non-forecast strategies should still use the global threshold."""
        # YES entry at 0.50, price drops to 0.40 → -20% unrealized
        # Global 0.15: triggers
        assert should_cut_loss("YES", 0.50, 0.40, threshold=0.15) is True
```

- [ ] **Step 2: Run test to verify it passes (pure function tests, no code change needed)**

Run: `cd ~/polybot && python -m pytest tests/test_position_manager.py::TestPerStrategyStopLoss -v`
Expected: PASS (these test the existing `should_cut_loss` with explicit thresholds)

- [ ] **Step 3: Write the failing integration test for position manager using forecast stop-loss**

Add to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_forecast_position_uses_forecast_stop_loss():
    """Forecast positions should use forecast_stop_loss_threshold from settings."""
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[{
        "id": 1, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 10.0, "strategy": "forecast", "status": "dry_run",
        "polymarket_id": "mkt-1", "question": "Test market?",
        "ensemble_probability": 0.65,
        "resolution_time": None, "opened_at": None, "kelly_inputs": None,
    }])
    db.fetchval = AsyncMock(return_value=None)

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-1.20)

    scanner = MagicMock()
    # YES entry at 0.50, current yes_price=0.44 → -12% loss
    # Should trigger with forecast threshold 0.10 but NOT global 0.15
    scanner.get_all_cached_prices.return_value = {
        "mkt-1": {"yes_price": 0.44}
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.15  # global: would NOT trigger at -12%
    settings.forecast_stop_loss_threshold = 0.10  # forecast: SHOULD trigger at -12%
    settings.early_exit_edge = 0.02
    settings.forecast_time_stop_minutes = 90.0
    settings.forecast_time_stop_fraction = 0.15
    settings.forecast_time_stop_max_minutes = 480.0
    settings.forecast_time_stop_min_resolution_hours = 48.0
    settings.snipe_max_hold_hours = 6.0

    pm = ActivePositionManager(db, executor, scanner, AsyncMock(), settings)
    await pm.check_positions()

    # Should have exited via stop_loss
    executor.exit_position.assert_called_once()
    call_kwargs = executor.exit_position.call_args[1]
    assert call_kwargs["exit_reason"] == "stop_loss"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd ~/polybot && python -m pytest tests/test_position_manager.py::test_forecast_position_uses_forecast_stop_loss -v`
Expected: FAIL — `ActivePositionManager` doesn't read `forecast_stop_loss_threshold` yet

- [ ] **Step 5: Add config field**

In `polybot/core/config.py`, add after line 93 (`stop_loss_threshold: float = 0.15`):

```python
    forecast_stop_loss_threshold: float = 0.10  # tighter stop for forecast (data: avg loss was -35% at 0.15)
```

- [ ] **Step 6: Implement per-strategy stop-loss in position manager**

In `polybot/trading/position_manager.py`, modify `__init__` to read the new threshold:

After line 47 (`self._stop_loss = settings.stop_loss_threshold`), add:

```python
        self._forecast_stop_loss = getattr(settings, 'forecast_stop_loss_threshold', self._stop_loss)
```

Then modify the generic exit block (around line 283-294). Replace:

```python
            strategy = pos["strategy"]
            tp_threshold = learned_thresholds.get(strategy, {}).get(
                "take_profit_threshold", self._take_profit)
            sl_threshold = learned_thresholds.get(strategy, {}).get(
                "stop_loss_threshold", self._stop_loss)
```

With:

```python
            strategy = pos["strategy"]
            tp_threshold = learned_thresholds.get(strategy, {}).get(
                "take_profit_threshold", self._take_profit)
            base_sl = self._forecast_stop_loss if strategy == "forecast" else self._stop_loss
            sl_threshold = learned_thresholds.get(strategy, {}).get(
                "stop_loss_threshold", base_sl)
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `cd ~/polybot && python -m pytest tests/test_position_manager.py::test_forecast_position_uses_forecast_stop_loss tests/test_position_manager.py::TestPerStrategyStopLoss -v`
Expected: PASS

- [ ] **Step 8: Run the full position manager test suite**

Run: `cd ~/polybot && python -m pytest tests/test_position_manager.py -v`
Expected: All existing tests still pass

- [ ] **Step 9: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py polybot/trading/position_manager.py tests/test_position_manager.py
git commit -m "feat: per-strategy stop-loss — tighter 10% stop for forecast

Data showed forecast stop-losses averaged -\$5.31 (35% loss on ~\$15 positions).
Tightening from 15% to 10% cuts average loss by ~33% while still giving room
for normal volatility. Non-forecast strategies keep the global 15% threshold."
```

---

### Task 2: Forecast YES-Side Entry Price Filter

The data shows Forecast YES wins averaged entry at 0.098 (sub-10 cents) while YES stop-losses averaged entry at 0.169. Higher YES entries lose more. Add a max entry price filter to keep Forecast in its sweet spot.

**Files:**
- Modify: `polybot/core/config.py` (add `forecast_yes_max_entry`)
- Modify: `polybot/strategies/forecast.py:464-466` (filter before placing order)
- Test: `tests/test_forecast_strategy.py`

- [ ] **Step 1: Write the failing test for YES entry filter**

Add to `tests/test_forecast_strategy.py`:

```python
@pytest.mark.asyncio
async def test_forecast_rejects_yes_above_max_entry():
    """Forecast should skip YES trades where market price exceeds max entry threshold."""
    settings = MagicMock()
    settings.forecast_interval_seconds = 300
    settings.forecast_kelly_mult = 0.25
    settings.forecast_max_single_pct = 0.15
    settings.use_maker_orders = True
    settings.max_positions_per_market = 1
    settings.min_trade_size = 1.0
    settings.forecast_yes_max_entry = 0.15  # only enter YES below 15 cents
    settings.forecast_no_min_entry = 0.0    # no filter on NO side
    settings.quant_weights = {
        "line_movement": 0.30, "volume_spike": 0.25,
        "book_imbalance": 0.20, "spread": 0.15, "time_decay": 0.10,
    }
    settings.ensemble_stdev_low = 0.05
    settings.ensemble_stdev_high = 0.12
    settings.confidence_mult_low = 1.0
    settings.confidence_mult_mid = 0.7
    settings.confidence_mult_high = 0.4
    settings.quant_negative_mult = 0.75
    settings.post_breaker_kelly_reduction = 0.50
    settings.bankroll_survival_threshold = 50.0
    settings.bankroll_growth_threshold = 500.0

    ensemble = MagicMock()
    researcher = MagicMock()
    strategy = EnsembleForecastStrategy(settings=settings, ensemble=ensemble, researcher=researcher)

    # Simulate a YES trade on a market priced at 0.25 (above 0.15 threshold)
    from polybot.markets.filters import MarketCandidate
    from polybot.analysis.quant import QuantSignals
    from unittest.mock import AsyncMock, patch
    from datetime import datetime, timezone, timedelta

    candidate = MarketCandidate(
        polymarket_id="test-mkt",
        question="Will X happen?",
        category="crypto",
        resolution_time=datetime.now(timezone.utc) + timedelta(hours=48),
        current_price=0.25,  # above 0.15 threshold
        book_depth=5000,
        volume_24h=10000,
        last_analyzed_at=None,
        previous_price=None,
        yes_token_id="yes-token",
        no_token_id="no-token",
        no_price=0.75,
    )

    # Mock kelly to return YES side
    mock_kelly = MagicMock()
    mock_kelly.edge = 0.10
    mock_kelly.side = "YES"
    mock_kelly.kelly_fraction = 0.05

    ctx = MagicMock()
    ctx.settings = settings
    ctx.portfolio_lock = asyncio.Lock()
    ctx.executor = AsyncMock()
    ctx.risk_manager = MagicMock()
    ctx.risk_manager.confidence_multiplier.return_value = 1.0
    ctx.risk_manager.edge_skepticism_discount.return_value = 1.0
    ctx.risk_manager.check.return_value = MagicMock(allowed=True)

    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(return_value=1)
    ctx.db.fetchrow = AsyncMock(return_value={
        "bankroll": 500.0, "total_deployed": 50.0, "daily_pnl": 0.0,
        "circuit_breaker_until": None, "category_scores": None,
    })
    ctx.db.fetch = AsyncMock(return_value=[])

    with patch("polybot.strategies.forecast.compute_kelly", return_value=mock_kelly):
        with patch("polybot.strategies.forecast.compute_position_size", return_value=10.0):
            await strategy._full_analyze_and_trade(
                candidate=candidate,
                quant=QuantSignals(0, 0, 0, 0, 0),
                trust_weights={},
                bankroll=500.0,
                kelly_mult=0.25,
                edge_threshold=0.05,
                portfolio=MagicMock(circuit_breaker_until=None),
                calibration_corrections={},
                ctx=ctx,
            )

    # Should NOT have placed an order — YES at 0.25 exceeds 0.15 max entry
    ctx.executor.place_order.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && python -m pytest tests/test_forecast_strategy.py::test_forecast_rejects_yes_above_max_entry -v`
Expected: FAIL — no entry filter exists yet, so `place_order` will be called

- [ ] **Step 3: Add config fields**

In `polybot/core/config.py`, add after the `forecast_category_filter_enabled` line (125):

```python
    forecast_yes_max_entry: float = 0.15       # only enter YES below this price (data: winners avg 0.098)
    forecast_no_min_entry: float = 0.60        # only enter NO above this yes_price (mid-range filter)
```

- [ ] **Step 4: Implement entry filter in forecast strategy**

In `polybot/strategies/forecast.py`, in `_full_analyze_and_trade()`, add the filter right after the kelly side is determined and before position sizing. Add after line 339 (`return`) and before line 342 (`conf_mult = ...`):

```python
        # YES-side entry price filter: data shows low-prob YES bets win;
        # higher-priced YES entries have worse stop-loss outcomes
        _yes_max_entry = getattr(self._settings, 'forecast_yes_max_entry', 1.0)
        _no_min_entry = getattr(self._settings, 'forecast_no_min_entry', 0.0)
        if kelly_result.side == "YES" and candidate.current_price > _yes_max_entry:
            log.info("forecast_yes_entry_filtered", market=candidate.polymarket_id,
                     price=candidate.current_price, max_entry=_yes_max_entry)
            return
        if kelly_result.side == "NO" and candidate.current_price < _no_min_entry:
            log.info("forecast_no_entry_filtered", market=candidate.polymarket_id,
                     price=candidate.current_price, min_entry=_no_min_entry)
            return
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/polybot && python -m pytest tests/test_forecast_strategy.py::test_forecast_rejects_yes_above_max_entry -v`
Expected: PASS

- [ ] **Step 6: Run full forecast test suite**

Run: `cd ~/polybot && python -m pytest tests/test_forecast_strategy.py -v`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py polybot/strategies/forecast.py tests/test_forecast_strategy.py
git commit -m "feat: forecast entry price filter — YES below 15c, NO above 60c

Data: 14 YES wins averaged entry at 0.098; 14 YES stop-losses averaged 0.169.
Low-probability YES bets are the profitable profile. Filter keeps forecast
in its sweet spot. Config-driven via forecast_yes_max_entry/forecast_no_min_entry."
```

---

### Task 3: Mean Reversion Mid-Range Filter

MR is +$22.64 in the 0.30-0.70 range but -$18.45 at extremes. The existing code already filters `< 0.10` and `> 0.90` (line 103). Tightening to 0.25-0.75 via config cuts the losing tail.

**Files:**
- Modify: `polybot/core/config.py` (add `mr_min_entry_price`, `mr_max_entry_price`)
- Modify: `polybot/strategies/mean_reversion.py:102-103`
- Test: `tests/test_mean_reversion.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mean_reversion.py`:

```python
class TestMidRangeFilter:
    """MR should only trade in the mid-range (0.25-0.75 by default)."""

    @pytest.mark.asyncio
    async def test_rejects_extreme_low_price(self):
        """Market at 0.15 should be skipped (below mr_min_entry_price)."""
        s = _make_settings()
        s.mr_min_entry_price = 0.25
        s.mr_max_entry_price = 0.75
        strategy = MeanReversionStrategy(s)
        # Inject a snapshot to trigger detection, then feed a low-price market
        now = datetime.now(timezone.utc)
        strategy._price_snapshots["m1"] = [(0.30, now - timedelta(minutes=5))]

        ctx = _make_ctx()
        ctx.scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", price=0.15, volume=5000.0, depth=1000.0)
        ])
        await strategy.run_once(ctx)
        ctx.executor.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_extreme_high_price(self):
        """Market at 0.85 should be skipped (above mr_max_entry_price)."""
        s = _make_settings()
        s.mr_min_entry_price = 0.25
        s.mr_max_entry_price = 0.75
        strategy = MeanReversionStrategy(s)
        now = datetime.now(timezone.utc)
        strategy._price_snapshots["m1"] = [(0.70, now - timedelta(minutes=5))]

        ctx = _make_ctx()
        ctx.scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", price=0.85, volume=5000.0, depth=1000.0)
        ])
        await strategy.run_once(ctx)
        ctx.executor.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_mid_range_price(self):
        """Market at 0.50 with a big move should be accepted."""
        s = _make_settings()
        s.mr_min_entry_price = 0.25
        s.mr_max_entry_price = 0.75
        s.mr_trigger_threshold = 0.05
        s.mr_min_expected_reversion = 0.0
        strategy = MeanReversionStrategy(s)
        now = datetime.now(timezone.utc)
        # Inject snapshot at 0.60 → current 0.50 = 10% drop, should trigger
        strategy._price_snapshots["m1"] = [(0.60, now - timedelta(minutes=5))]

        ctx = _make_ctx()
        ctx.scanner.fetch_markets = AsyncMock(return_value=[
            _make_market("m1", price=0.50, volume=5000.0, depth=1000.0)
        ])
        ctx.db.fetchval = AsyncMock(side_effect=_mr_fetchval_factory(bankroll=500.0))
        ctx.db.fetchrow = AsyncMock(return_value={
            "bankroll": 500.0, "total_deployed": 50.0, "daily_pnl": 0.0,
            "circuit_breaker_until": None, "post_breaker_until": None,
        })
        ctx.db.fetch = AsyncMock(return_value=[])
        ctx.risk_manager.check.return_value = MagicMock(allowed=True)

        await strategy.run_once(ctx)
        ctx.executor.place_order.assert_called_once()
```

Note: `_make_ctx` and `_mr_fetchval_factory` may need to be defined if not already present. The implementer should check the existing test file and reuse or create appropriate fixtures matching the existing patterns.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && python -m pytest tests/test_mean_reversion.py::TestMidRangeFilter -v`
Expected: FAIL — no `mr_min_entry_price`/`mr_max_entry_price` in settings or code

- [ ] **Step 3: Add config fields**

In `polybot/core/config.py`, add after `mr_big_move_kelly_boost` (line 203):

```python
    mr_min_entry_price: float = 0.25           # skip extremes below this (data: +$22.64 mid-range vs -$18.45 extremes)
    mr_max_entry_price: float = 0.75           # skip extremes above this
```

- [ ] **Step 4: Implement mid-range filter**

In `polybot/strategies/mean_reversion.py`, in `__init__`, add after line 34:

```python
        self._min_entry_price = getattr(settings, 'mr_min_entry_price', 0.10)
        self._max_entry_price = getattr(settings, 'mr_max_entry_price', 0.90)
```

Then replace the existing price filter on lines 102-103:

```python
            # Skip extreme prices (near resolution, snipe territory)
            if price < 0.10 or price > 0.90:
                continue
```

With:

```python
            # Mid-range filter: data shows MR profits in 0.25-0.75, loses at extremes
            if price < self._min_entry_price or price > self._max_entry_price:
                continue
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/polybot && python -m pytest tests/test_mean_reversion.py::TestMidRangeFilter -v`
Expected: PASS

- [ ] **Step 6: Run full MR test suite**

Run: `cd ~/polybot && python -m pytest tests/test_mean_reversion.py -v`
Expected: All tests pass (existing tests use default settings where `mr_min_entry_price`/`mr_max_entry_price` fall back to existing behavior)

- [ ] **Step 7: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py polybot/strategies/mean_reversion.py tests/test_mean_reversion.py
git commit -m "feat: MR mid-range filter — only trade 0.25-0.75 price range

Data: MR was +\$22.64 in 0.30-0.70 range but -\$18.45 at extremes.
Configurable via mr_min_entry_price/mr_max_entry_price in .env."
```

---

### Task 4: Snipe Volume Boost

Snipe is the most reliable strategy (71% win rate, +$0.66 avg win, -$0.32 avg loss). Increase throughput by reducing cooldown and allowing more entries per market.

**Files:**
- Modify: `.env` (config-only change, no code)
- Test: Manual verification via logs

- [ ] **Step 1: Write the config change test**

Add to `tests/test_config.py` (or verify inline):

```python
def test_snipe_config_defaults():
    """Verify snipe config fields have expected defaults."""
    import os
    # Temporarily clear env vars to test defaults
    from polybot.core.config import Settings
    s = Settings(
        polymarket_api_key="test", polymarket_private_key="test",
        anthropic_api_key="test", openai_api_key="test",
        google_api_key="test", brave_api_key="test",
        database_url="postgresql://localhost/test",
        resend_api_key="test",
    )
    assert s.snipe_cooldown_hours == 4.0
    assert s.snipe_max_entries_per_market == 3
```

- [ ] **Step 2: Run to confirm current defaults**

Run: `cd ~/polybot && python -m pytest tests/test_config.py::test_snipe_config_defaults -v`
Expected: PASS (confirming current state)

- [ ] **Step 3: Update .env with snipe volume boost**

In `.env`, update these values:

```
SNIPE_COOLDOWN_HOURS=0.5
SNIPE_MAX_ENTRIES_PER_MARKET=6
```

This is config-only. The code already reads these values. The effect:
- Cooldown 4.0h → 0.5h: re-enter markets 8x faster after exit
- Max entries 3 → 6: double the position stacking on high-confidence markets

- [ ] **Step 4: Commit**

```bash
cd ~/polybot
git add .env
git commit -m "config: snipe volume boost — cooldown 4h->0.5h, max entries 3->6

Snipe is the most reliable strategy (71% win rate, +\$0.66 avg win,
-\$0.32 avg loss). Increasing throughput on the proven winner."
```

---

### Task 5: Re-Enable Forecast + Disable Bleeders + Re-Enable MR

Flip the strategy enables based on the data. Forecast gets re-enabled with the new guardrails from Tasks 1-2. MR gets re-enabled with the mid-range filter from Task 3. Bleeders get explicitly disabled.

**Files:**
- Modify: `.env`

- [ ] **Step 1: Update .env strategy enables**

```
FORECAST_ENABLED=true
MR_ENABLED=true
ARB_ENABLED=false
CV_ENABLED=false
```

- [ ] **Step 2: Also tighten forecast position sizing**

The data shows Forecast losers have larger positions ($15.42) than winners ($11.09). Reduce max position to keep sizing in the winning range:

```
FORECAST_MAX_SINGLE_PCT=0.05
FORECAST_KELLY_MULT=0.15
```

This caps Forecast positions at ~5% of bankroll (~$24 on $483) and reduces kelly aggression, keeping sizes closer to the winning $11 average.

- [ ] **Step 3: Set the new forecast entry filters**

```
FORECAST_YES_MAX_ENTRY=0.15
FORECAST_NO_MIN_ENTRY=0.60
FORECAST_STOP_LOSS_THRESHOLD=0.10
```

- [ ] **Step 4: Set the new MR mid-range filter**

```
MR_MIN_ENTRY_PRICE=0.25
MR_MAX_ENTRY_PRICE=0.75
```

- [ ] **Step 5: Run the full test suite to verify nothing is broken**

Run: `cd ~/polybot && python -m pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add .env
git commit -m "config: re-enable forecast+MR with guardrails, disable bleeders

Forecast: re-enabled with 10% stop-loss, YES<15c entry filter, 5% max position.
MR: re-enabled with 0.25-0.75 mid-range filter.
Arbitrage: disabled (0 wins, -\$123.67 all-time).
Cross-venue: disabled (0 wins, -\$10.68 all-time)."
```

---

### Task 6: Final Validation — Full Test Suite + Config Sanity Check

- [ ] **Step 1: Run the complete test suite**

Run: `cd ~/polybot && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All tests pass, no regressions

- [ ] **Step 2: Verify config loads correctly**

Run: `cd ~/polybot && python -c "from polybot.core.config import Settings; s = Settings(); print(f'forecast_enabled={s.forecast_enabled}, mr_enabled={s.mr_enabled}, arb_enabled={s.arb_enabled}, cv_enabled={s.cv_enabled}'); print(f'forecast_stop_loss={s.forecast_stop_loss_threshold}, yes_max_entry={s.forecast_yes_max_entry}, no_min_entry={s.forecast_no_min_entry}'); print(f'mr_min_entry={s.mr_min_entry_price}, mr_max_entry={s.mr_max_entry_price}'); print(f'snipe_cooldown={s.snipe_cooldown_hours}, snipe_max_entries={s.snipe_max_entries_per_market}')"`

Expected output:
```
forecast_enabled=True, mr_enabled=True, arb_enabled=False, cv_enabled=False
forecast_stop_loss=0.1, yes_max_entry=0.15, no_min_entry=0.6
mr_min_entry=0.25, mr_max_entry=0.75
snipe_cooldown=0.5, snipe_max_entries=6
```

- [ ] **Step 3: Commit final state if any adjustments were needed**

Only if test failures required fixes in previous tasks.
