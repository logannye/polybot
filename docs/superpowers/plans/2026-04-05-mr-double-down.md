# Mean Reversion Double-Down Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Amplify the most profitable aspects of mean reversion: raise the trigger threshold to filter out losing small moves, cut the time-stop to exit losers faster, and size up more aggressively on the biggest moves.

**Architecture:** Three independent config/code changes, each backed by production data. The trigger threshold and time-stop are pure config changes (.env + config.py defaults). The tiered Kelly boost requires a small code change in `mean_reversion.py` to multiply `kelly_adj` based on move magnitude — simple, hot-reloadable via new config keys.

**Tech Stack:** Python 3.13, pytest, pydantic Settings (.env)

**Data backing these changes:**

| Metric | Before (current) | Evidence |
|--------|-------------------|----------|
| Trigger threshold | 0.05 (.env override) | <3% exp_rev bucket: 19 trades, 37.5% win, -$0.64 PnL |
| Time-stop | 24h | 60+ min holds: 14% win rate, -$13.33 PnL |
| Kelly mult (fixed) | 0.35x for all moves | $25+ positions: 67% win, +$21.18; <$5: 40% win, +$1.52 |

---

### Task 1: Raise MR Trigger Threshold to 10%

**Files:**
- Modify: `.env`
- Modify: `polybot/core/config.py:188`

The .env currently has `MR_TRIGGER_THRESHOLD=0.05`, overriding the config.py default of 0.075. Production data shows moves below 10% have mediocre-to-negative PnL. Raising to 0.10 filters out the losing small-move bucket while keeping all the top winners (which had moves of 10-33%).

- [ ] **Step 1: Update .env**

In `.env`, change:

```
MR_TRIGGER_THRESHOLD=0.05
```

To:

```
MR_TRIGGER_THRESHOLD=0.10
```

- [ ] **Step 2: Update config.py default to match**

In `polybot/core/config.py:188`, change:

```python
    mr_trigger_threshold: float = 0.075
```

To:

```python
    mr_trigger_threshold: float = 0.10
```

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add .env polybot/core/config.py && git commit -m "$(cat <<'EOF'
tune: raise MR trigger threshold from 5% to 10%

Production data: <3% exp_rev bucket (19 trades) has 37.5% win rate and
-$0.64 total PnL. All top MR winners had moves >10%. Raising threshold
concentrates capital on high-edge moves.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Cut MR Time-Stop from 24h to 3h

**Files:**
- Modify: `.env`
- Modify: `polybot/core/config.py:196`
- Test: `tests/test_position_manager.py`

Production data: 60+ min hold trades have 14% win rate and -$13.33 total PnL. Meanwhile 86% of MR winners close within 60 minutes. A 3h time-stop (180 min) gives winners room while cutting the long tail of slow losers. The time-stop is stored in `kelly_inputs.max_hold_hours` at trade entry and read by `position_manager.py:163`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_position_manager.py`:

```python
@pytest.mark.asyncio
async def test_mr_time_stop_3h():
    """MR position held >3h should trigger time_stop exit."""
    from datetime import datetime, timezone, timedelta
    import json

    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)  # no learned params
    db.fetch = AsyncMock(return_value=[{
        "id": 42, "side": "YES", "entry_price": 0.50, "shares": 20.0,
        "position_size_usd": 30.0, "strategy": "mean_reversion", "status": "dry_run",
        "polymarket_id": "mkt-mr", "question": "MR test market?",
        "ensemble_probability": 0.55, "resolution_time": None,
        "opened_at": datetime.now(timezone.utc) - timedelta(hours=3, minutes=5),
        "kelly_inputs": json.dumps({
            "move": 0.15, "old_price": 0.35, "trigger_price": 0.50,
            "expected_reversion": 0.06, "tp_yes_price": 0.56,
            "sl_yes_price": 0.46, "max_hold_hours": 3.0,
        }),
    }])

    executor = AsyncMock()
    executor.exit_position = AsyncMock(return_value=-1.50)

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-mr": {"yes_price": 0.49, "no_price": 0.51},
    }

    settings = MagicMock()
    settings.take_profit_threshold = 0.20
    settings.stop_loss_threshold = 0.25
    settings.early_exit_edge = 0.02

    email = AsyncMock()

    mgr = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email, settings=settings)
    await mgr.check_positions()

    executor.exit_position.assert_called_once_with(
        trade_id=42, exit_price=0.49, exit_reason="time_stop")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_position_manager.py::test_mr_time_stop_3h -v
```

Expected: PASS actually — the position_manager already reads `max_hold_hours` from kelly_inputs. This test validates the existing behavior with the new value. If it passes, that's correct — the time-stop logic is already in place, we're just changing the config value.

- [ ] **Step 3: Run test to confirm it passes**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_position_manager.py::test_mr_time_stop_3h -v
```

Expected: PASS

- [ ] **Step 4: Add MR_MAX_HOLD_HOURS to .env**

Append to `.env`:

```
MR_MAX_HOLD_HOURS=3.0
```

- [ ] **Step 5: Update config.py default**

In `polybot/core/config.py:196`, change:

```python
    mr_max_hold_hours: float = 24.0
```

To:

```python
    mr_max_hold_hours: float = 3.0
```

- [ ] **Step 6: Run all position manager tests**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_position_manager.py -v
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add .env polybot/core/config.py tests/test_position_manager.py && git commit -m "$(cat <<'EOF'
tune: cut MR time-stop from 24h to 3h

Production data: 60+ min holds have 14% win rate and -$13.33 PnL.
86% of MR winners close within 60 min. 3h gives winners room while
cutting the long tail of slow losers.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Tiered Kelly Boost for Big Moves

**Files:**
- Modify: `polybot/strategies/mean_reversion.py:195`
- Modify: `polybot/core/config.py` (add 2 new config keys after line 197)
- Test: `tests/test_mean_reversion.py`

Position size is already proportional to edge via Kelly, but the data shows the biggest moves ($25+ positions from 15%+ moves) have 67% win rate and +$21.18 PnL. Adding a tiered Kelly boost amplifies sizing on these high-conviction signals.

Tiers:
- Move < 15%: base Kelly (0.35x) — no change
- Move 15-25%: 1.3x Kelly boost → effective 0.455x
- Move > 25%: 1.6x Kelly boost → effective 0.56x

The boost applies as a multiplier to `kelly_adj` in `mean_reversion.py`, right before `compute_position_size()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mean_reversion.py`:

```python
@pytest.mark.asyncio
async def test_big_move_kelly_boost():
    """Moves >15% should get a Kelly boost, producing larger positions."""
    s = _make_settings()
    s.use_maker_orders = True
    s.min_trade_size = 1.0
    s.post_breaker_kelly_reduction = 0.5
    s.bankroll_survival_threshold = 50.0
    s.bankroll_growth_threshold = 500.0
    s.mr_min_expected_reversion = 0.0
    s.conviction_stack_enabled = False
    s.conviction_stack_per_signal = 0.5
    s.conviction_stack_max = 3.0
    s.mr_trigger_threshold = 0.10
    s.mr_big_move_threshold = 0.15
    s.mr_big_move_kelly_boost = 1.3

    strategy = MeanReversionStrategy(s)

    now = datetime.now(timezone.utc)
    # 20% move (above big_move_threshold of 15%)
    strategy._price_snapshots["m1"] = [(0.30, now - timedelta(minutes=5))]
    market = _make_market("m1", price=0.50, volume=5000.0, depth=5000.0)

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.db.fetchval = AsyncMock(side_effect=[
        True,   # strategy enabled
        0,      # mr_open count
        0,      # cooldown
        0,      # existing position
        1,      # market upsert
        1,      # analysis insert
    ])
    ctx.db.fetchrow = AsyncMock(side_effect=[
        {"bankroll": 500.0, "total_deployed": 0.0, "daily_pnl": 0.0,
         "circuit_breaker_until": None},
        {"bankroll": 500.0, "total_deployed": 0.0, "daily_pnl": 0.0,
         "post_breaker_until": None, "circuit_breaker_until": None},
    ])
    ctx.db.fetch = AsyncMock(return_value=[])  # no open trades
    ctx.executor = AsyncMock()
    ctx.executor.place_order = AsyncMock(return_value=True)
    ctx.settings = s
    ctx.risk_manager = RiskManager()
    ctx.portfolio_lock = asyncio.Lock()
    ctx.email_notifier = AsyncMock()
    ctx.scanner = MagicMock()
    ctx.scanner.fetch_markets = AsyncMock(return_value=[market])

    await strategy.run_once(ctx)

    ctx.executor.place_order.assert_called_once()
    call_kwargs = ctx.executor.place_order.call_args
    size_usd = call_kwargs.kwargs.get("size_usd") or call_kwargs[1].get("size_usd")
    # With 20% move, kelly boost of 1.3x should produce a larger position
    # than the ~$20 base sizing. Exact value depends on Kelly math, but
    # must be > $25 (boosted) rather than ~$20 (unboosted).
    assert size_usd > 25.0, f"Expected boosted size > $25, got ${size_usd:.2f}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_mean_reversion.py::test_big_move_kelly_boost -v
```

Expected: FAIL — `mr_big_move_threshold` and `mr_big_move_kelly_boost` don't exist yet, and no boost logic is applied.

- [ ] **Step 3: Add config keys**

In `polybot/core/config.py`, after line 197 (`mr_min_expected_reversion: float = 0.03`), add:

```python
    mr_big_move_threshold: float = 0.15   # moves above this get kelly boost
    mr_big_move_kelly_boost: float = 1.3  # kelly multiplier for big moves
```

- [ ] **Step 4: Read the new config in MeanReversionStrategy.__init__**

In `polybot/strategies/mean_reversion.py`, after line 34 (`self._min_expected_reversion = ...`), add:

```python
        self._big_move_threshold = getattr(settings, 'mr_big_move_threshold', 0.15)
        self._big_move_kelly_boost = getattr(settings, 'mr_big_move_kelly_boost', 1.3)
```

- [ ] **Step 5: Apply Kelly boost before compute_position_size**

In `polybot/strategies/mean_reversion.py`, after line 195 (the `kelly_fraction = ...` line), add the boost:

```python
                # Tiered Kelly boost: bigger moves get more aggressive sizing
                if abs(move) >= self._big_move_threshold:
                    kelly_adj *= self._big_move_kelly_boost
```

So lines 195-201 become:

```python
                kelly_fraction = net_edge / (1 - buy_price) if buy_price < 1.0 else 0.0
                # Tiered Kelly boost: bigger moves get more aggressive sizing
                if abs(move) >= self._big_move_threshold:
                    kelly_adj *= self._big_move_kelly_boost
                size = compute_position_size(
                    bankroll=bankroll, kelly_fraction=kelly_fraction,
                    kelly_mult=kelly_adj, confidence_mult=1.0,
                    max_single_pct=self.max_single_pct,
                    min_trade_size=ctx.settings.min_trade_size)
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_mean_reversion.py::test_big_move_kelly_boost -v
```

Expected: PASS

- [ ] **Step 7: Run all mean reversion tests**

```bash
cd ~/polybot && .venv/bin/python -m pytest tests/test_mean_reversion.py -v
```

Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
cd ~/polybot && git add polybot/strategies/mean_reversion.py polybot/core/config.py tests/test_mean_reversion.py && git commit -m "$(cat <<'EOF'
feat: tiered Kelly boost for MR big moves (>15% → 1.3x)

Production data: $25+ positions have 67% win rate and +$21.18 PnL.
Moves >15% are the highest-edge signals. Boosting Kelly on these
amplifies sizing on the most profitable trades.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Restart Bot and Run Full Suite

**Files:** None (operational)

- [ ] **Step 1: Run full test suite**

```bash
cd ~/polybot && .venv/bin/python -m pytest -x -q
```

Expected: All tests pass.

- [ ] **Step 2: Restart the bot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 3: Verify new config is loaded**

```bash
sleep 10 && grep "mr_trigger\|mr_max_hold\|mr_big_move\|polybot_starting" ~/polybot/data/polybot_stdout.log | tail -5
```

Expected: See the bot start with the new config values.

- [ ] **Step 4: Watch for MR activity under new params**

```bash
tail -f ~/polybot/data/polybot_stdout.log | grep --line-buffered "mr_\|mean_reversion"
```

Expected: Fewer candidates (higher threshold filtering), but when a trade fires it should be larger ($25+).
