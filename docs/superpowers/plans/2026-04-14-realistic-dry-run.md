# Realistic Dry-Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make dry-run results approximate live trading conditions by using real order books for pricing and fill simulation, so the next live deployment is validated by realistic simulation.

**Architecture:** Add a `dry_run_realistic` config flag. When enabled, the executor's `place_order` fetches the real order book before recording a dry-run trade: if the spread is too wide, the order is rejected; if it would cross the spread, it fills at the best ask (not model price) with a simulated taker fee. Non-crossing orders are recorded as `dry_run_resting` and only fill when the position manager detects the price has crossed. Also enforce the graduated deployment stages from Spec 1.

**Tech Stack:** Python 3.13, pytest, py-clob-client

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/core/config.py` | Modify | Add `dry_run_realistic`, `dry_run_taker_fee_pct` |
| `polybot/trading/executor.py` | Modify | Check order book in dry-run, apply spread/fee |
| `polybot/core/engine.py` | Modify | Enforce deployment stage on startup |
| `tests/test_realistic_dryrun.py` | Create | Tests for spread-aware dry-run |

---

### Task 1: Config Keys

**Files:**
- Modify: `polybot/core/config.py`

- [ ] **Step 1: Add config keys**

After `dry_run: bool = True` (line 22), add:

```python
    dry_run_realistic: bool = True           # use real order books for dry-run pricing
    dry_run_taker_fee_pct: float = 0.02      # simulated taker fee (2%)
    dry_run_max_spread: float = 0.15         # reject dry-run orders on markets with > 15% spread
```

- [ ] **Step 2: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
Fix any config assertion failures.

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py
git commit -m "config: add realistic dry-run settings (spread check, taker fee sim)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Spread-Aware Dry-Run in Executor

**Files:**
- Modify: `polybot/trading/executor.py`
- Create: `tests/test_realistic_dryrun.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_realistic_dryrun.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_realistic_dryrun_rejects_wide_spread():
    """Dry-run should reject orders on markets with spread > threshold."""
    from polybot.trading.executor import OrderExecutor

    scanner = MagicMock()
    wallet = MagicMock()
    wallet.compute_shares.return_value = 20.0
    db = AsyncMock()
    clob = AsyncMock()

    # Order book with 50% spread
    mock_book = MagicMock()
    mock_ask = MagicMock()
    mock_ask.price = "0.75"
    mock_bid = MagicMock()
    mock_bid.price = "0.25"
    mock_book.asks = [mock_ask]
    mock_book.bids = [mock_bid]
    clob.get_order_book_sync = AsyncMock(return_value=mock_book)

    settings = MagicMock()
    settings.dry_run = True
    settings.dry_run_realistic = True
    settings.dry_run_max_spread = 0.15
    settings.dry_run_taker_fee_pct = 0.02

    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=120, clob=clob, dry_run=True)
    executor._settings = settings

    result = await executor.place_order(
        token_id="token123", side="YES", size_usd=10.0, price=0.50,
        market_id=1, analysis_id=1, strategy="mean_reversion")

    # Should be rejected due to wide spread
    assert result is None


@pytest.mark.asyncio
async def test_realistic_dryrun_fills_at_best_ask():
    """Dry-run should fill at best ask price, not model price."""
    from polybot.trading.executor import OrderExecutor

    scanner = MagicMock()
    wallet = MagicMock()
    wallet.compute_shares.return_value = 20.0
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)  # trade_id
    clob = AsyncMock()

    # Tight spread: bid 0.48, ask 0.52
    mock_book = MagicMock()
    mock_ask = MagicMock()
    mock_ask.price = "0.52"
    mock_bid = MagicMock()
    mock_bid.price = "0.48"
    mock_book.asks = [mock_ask]
    mock_book.bids = [mock_bid]
    clob.get_order_book_sync = AsyncMock(return_value=mock_book)

    settings = MagicMock()
    settings.dry_run = True
    settings.dry_run_realistic = True
    settings.dry_run_max_spread = 0.15
    settings.dry_run_taker_fee_pct = 0.02

    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=120, clob=clob, dry_run=True)
    executor._settings = settings

    result = await executor.place_order(
        token_id="token123", side="YES", size_usd=10.0, price=0.50,
        market_id=1, analysis_id=1, strategy="mean_reversion")

    assert result is not None
    # Check the trade was inserted with the best ask price (0.52), not model price (0.50)
    insert_call = db.fetchval.call_args
    # The 4th positional arg ($4) is the entry_price
    assert insert_call[0][3] == 0.52


@pytest.mark.asyncio
async def test_non_realistic_dryrun_fills_at_model_price():
    """When dry_run_realistic=False, fill at model price (old behavior)."""
    from polybot.trading.executor import OrderExecutor

    scanner = MagicMock()
    wallet = MagicMock()
    wallet.compute_shares.return_value = 20.0
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    clob = AsyncMock()

    settings = MagicMock()
    settings.dry_run = True
    settings.dry_run_realistic = False

    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=120, clob=clob, dry_run=True)
    executor._settings = settings

    result = await executor.place_order(
        token_id="token123", side="YES", size_usd=10.0, price=0.50,
        market_id=1, analysis_id=1, strategy="mean_reversion")

    assert result is not None
    insert_call = db.fetchval.call_args
    assert insert_call[0][3] == 0.50  # model price, not order book
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run python -m pytest tests/test_realistic_dryrun.py -v`
Expected: FAIL — no realistic dry-run logic exists yet.

- [ ] **Step 3: Implement spread-aware dry-run in executor**

In `polybot/trading/executor.py`, modify `place_order`. The current flow is:

```python
    async def place_order(self, token_id, side, size_usd, price, market_id, analysis_id,
                          strategy: str = "forecast", kelly_inputs: dict | None = None,
                          post_only: bool = False):
        shares = self._wallet.compute_shares(size_usd, price)
        ...
```

Add a `_settings` attribute. In `__init__`, store settings if available (or we'll set it from ctx). For now, add the realistic check after `shares = ...`:

```python
    async def place_order(self, token_id, side, size_usd, price, market_id, analysis_id,
                          strategy: str = "forecast", kelly_inputs: dict | None = None,
                          post_only: bool = False):
        shares = self._wallet.compute_shares(size_usd, price)
        if shares <= 0:
            return None

        # Realistic dry-run: check order book before filling
        effective_price = price
        if (self._dry_run
                and getattr(self, '_settings', None)
                and getattr(self._settings, 'dry_run_realistic', False)
                and self._clob is not None
                and token_id):
            try:
                book = await asyncio.to_thread(self._clob._client.get_order_book, token_id)
                if book.asks and book.bids:
                    best_ask = float(book.asks[0].price)
                    best_bid = float(book.bids[0].price)
                    spread = best_ask - best_bid
                    max_spread = getattr(self._settings, 'dry_run_max_spread', 0.15)
                    if spread > max_spread:
                        log.info("dryrun_spread_reject", token_id=token_id[:20],
                                 spread=round(spread, 4), max=max_spread, strategy=strategy)
                        return None
                    # For buys: fill at best ask (what you'd actually pay)
                    if side in ("YES", "NO"):
                        effective_price = best_ask
                        shares = self._wallet.compute_shares(size_usd, effective_price)
                        # Apply simulated taker fee
                        fee_pct = getattr(self._settings, 'dry_run_taker_fee_pct', 0.02)
                        size_usd = size_usd * (1 - fee_pct)
                        shares = self._wallet.compute_shares(size_usd, effective_price)
                elif not book.asks:
                    log.info("dryrun_no_asks", token_id=token_id[:20], strategy=strategy)
                    return None
            except Exception as e:
                log.debug("dryrun_book_check_failed", error=str(e)[:60])
                # Fall through to model price if book check fails

        status = "dry_run" if self._dry_run else "open"
```

Also replace `price` with `effective_price` in the INSERT statement (the `$4` parameter):

Change:
```python
            market_id, analysis_id, side, price, size_usd, shares, kelly_json, status, strategy)
```
To:
```python
            market_id, analysis_id, side, effective_price, size_usd, shares, kelly_json, status, strategy)
```

Add `import asyncio` at top of file if not already there.

Also need to store `_settings` on the executor. Add to `__init__`:
```python
        self._settings = None  # set from engine context
```

- [ ] **Step 4: Add a method to set settings on the executor**

The engine creates the executor in `__main__.py`. We need settings available. The simplest way: in `__main__.py`, after creating the executor, set `executor._settings = settings`.

Read `polybot/__main__.py`, find where executor is created (around line 112-116), and add after it:
```python
    executor._settings = settings
```

- [ ] **Step 5: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/test_realistic_dryrun.py tests/ --tb=short -q`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/trading/executor.py polybot/__main__.py tests/test_realistic_dryrun.py
git commit -m "feat: realistic dry-run — uses order book for pricing and spread filtering

When dry_run_realistic=True, the executor fetches the real order book
before recording a dry-run trade. Rejects markets with spread > 15%.
Fills at best ask (not model price) with 2% simulated taker fee.
This ensures dry-run results approximate live trading conditions.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Enforce Deployment Stage on Startup

**Files:**
- Modify: `polybot/core/engine.py`

- [ ] **Step 1: Add deployment stage enforcement**

In `run_forever`, at the very top (before the strategy loop), add:

```python
        # Enforce deployment stage limits
        stage = getattr(self._settings, 'live_deployment_stage', 'dry_run')
        if not self._settings.dry_run:
            if stage == 'dry_run':
                log.critical("DEPLOYMENT_STAGE_BLOCK",
                             message="live_deployment_stage is 'dry_run' but dry_run=false. Refusing to start.")
                return
            if stage == 'micro_test':
                # Override max_total_deployed_pct to 5% for micro testing
                self._settings.max_total_deployed_pct = 0.05
                log.warning("MICRO_TEST_MODE", max_deployed_pct=5)
```

- [ ] **Step 2: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add polybot/core/engine.py
git commit -m "feat: enforce deployment stage — micro_test limits to 5% capital

live_deployment_stage='dry_run' blocks live trading entirely.
'micro_test' overrides max_total_deployed_pct to 5% (~$25).
'full' uses configured limits. Prevents jumping straight to full live.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
