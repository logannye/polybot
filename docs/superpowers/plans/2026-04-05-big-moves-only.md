# Big Moves Only — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Multiply mean reversion profitability by filtering out small-move trades (which lose money), sizing up on large-move trades (which have 71% win rate), and scanning price history to discover more large moves than the sliding window catches.

**Architecture:** Three coordinated changes: (1) config parameter tuning for filter + sizing, (2) a new `PriceHistoryScanner` class that periodically fetches CLOB price history for high-volume markets and injects detected moves into the MR strategy's snapshot window, (3) wiring the scanner into the engine's periodic tasks. The price history scanner runs as an independent periodic task, not inside the MR strategy, to avoid blocking the MR scan cycle.

**Tech Stack:** Python 3.13, pytest, asyncio, aiohttp, AsyncMock

---

## Evidence

Mean reversion profitability by move tier (from production data):

| Tier | Trades | Win Rate | Net PnL | Avg Return |
|------|--------|----------|---------|------------|
| Large (4%+) | 7 | **71%** | **+$3.42** | **14%** |
| Medium (2.5-4%) | 7 | 43% | -$0.41 | 8% |
| Small (<2.5%) | 12 | 25% | -$0.24 | 7% |

Small and medium moves are net negative. All profit comes from large moves. Current position sizing: avg $5.59 (1.2% of bankroll) — far below optimal for this edge.

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `polybot/core/config.py` | Modify | Tune `mr_kelly_mult`, `mr_max_single_pct`, `mr_trigger_threshold`, add `mr_min_expected_reversion` |
| `polybot/strategies/mean_reversion.py` | Modify | Add `mr_min_expected_reversion` filter before entry |
| `polybot/markets/price_history.py` | Create | `PriceHistoryScanner` — fetches CLOB price history for top markets, detects big moves |
| `polybot/core/engine.py` | Modify | Wire `PriceHistoryScanner` into periodic tasks, inject into MR strategy |
| `polybot/__main__.py` | Modify | Instantiate `PriceHistoryScanner` |
| `tests/test_mean_reversion.py` | Modify | Add test for `mr_min_expected_reversion` filter |
| `tests/test_price_history.py` | Create | Tests for `PriceHistoryScanner` move detection logic |

---

### Task 1: Tune MR config parameters

Raise the minimum edge and position sizing for mean reversion.

**Files:**
- Modify: `polybot/core/config.py:183-194`

- [ ] **Step 1: Update config defaults**

In `polybot/core/config.py`, change these existing values:

```python
    # Mean reversion strategy
    mr_enabled: bool = False
    mr_interval_seconds: float = 120.0
    mr_trigger_threshold: float = 0.10       # was 0.05 — require 10% move (produces ~4% expected_reversion at 0.40 fraction)
    mr_reversion_fraction: float = 0.40
    mr_kelly_mult: float = 0.35              # was 0.15 — 2.3x larger positions on proven 71% win-rate edge
    mr_max_single_pct: float = 0.15          # was 0.10 — allow up to 15% of bankroll per MR trade
    mr_max_concurrent: int = 5
    mr_min_volume_24h: float = 2000.0
    mr_min_book_depth: float = 500.0
    mr_cooldown_hours: float = 6.0
    mr_max_hold_hours: float = 24.0
    mr_min_expected_reversion: float = 0.04  # NEW — reject trades with expected_reversion < 4%
```

Key changes:
- `mr_trigger_threshold`: 0.05 → 0.10 (require 10% moves, not 5%)
- `mr_kelly_mult`: 0.15 → 0.35 (2.3x larger bets)
- `mr_max_single_pct`: 0.10 → 0.15 (cap at 15% of bankroll)
- `mr_min_expected_reversion`: NEW field, default 0.04

- [ ] **Step 2: Run config tests**

Run: `cd ~/polybot && uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py
git commit -m "tune: MR big-moves-only — trigger 10%, kelly 0.35, min reversion 4%

Data shows 71% win rate on 4%+ moves vs 32% on smaller moves.
Raising trigger threshold, position sizing, and adding a minimum
expected_reversion filter to focus on proven high-edge trades.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add `mr_min_expected_reversion` filter to MR strategy

Filter out trades where the expected reversion is below the configured minimum. This is the core signal quality gate.

**Files:**
- Modify: `polybot/strategies/mean_reversion.py:150-155`
- Modify: `tests/test_mean_reversion.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mean_reversion.py`:

```python
class TestMinExpectedReversion:
    def test_rejects_small_reversion(self):
        """Trades with expected_reversion < mr_min_expected_reversion should be filtered."""
        s = _make_settings()
        s.mr_min_expected_reversion = 0.04
        strategy = MeanReversionStrategy(s)

        # A 5% move with 0.40 reversion fraction = 0.02 expected reversion
        # This is below the 0.04 minimum → should be rejected
        move = 0.05
        reversion_frac = strategy._reversion_frac  # 0.40
        expected_reversion = move * reversion_frac  # 0.02
        assert expected_reversion < 0.04
        # The net_edge check in the strategy uses 0.02 as the floor,
        # but the new min_expected_reversion check at 0.04 should catch this

    def test_accepts_large_reversion(self):
        """Trades with expected_reversion >= mr_min_expected_reversion should pass."""
        s = _make_settings()
        s.mr_min_expected_reversion = 0.04
        strategy = MeanReversionStrategy(s)

        # A 12% move with 0.40 reversion fraction = 0.048 expected reversion
        move = 0.12
        expected_reversion = move * strategy._reversion_frac  # 0.048
        assert expected_reversion >= 0.04
```

- [ ] **Step 2: Run tests to verify they pass (these are unit-level logic checks, not integration)**

Run: `cd ~/polybot && uv run pytest tests/test_mean_reversion.py::TestMinExpectedReversion -v`
Expected: PASS (these test the math, not the strategy)

- [ ] **Step 3: Add the filter to `mean_reversion.py`**

In `polybot/strategies/mean_reversion.py`, in the `__init__` method (around line 33), add:

```python
        self._min_expected_reversion = getattr(settings, 'mr_min_expected_reversion', 0.0)
```

Then in the `run_once` method, find this block (around line 150-155):

```python
            # Edge estimate: expected reversion * fraction
            expected_reversion = abs(move) * self._reversion_frac
            net_edge = expected_reversion  # maker = 0% fee

            if net_edge < 0.02:
                continue
```

Replace with:

```python
            # Edge estimate: expected reversion * fraction
            expected_reversion = abs(move) * self._reversion_frac
            net_edge = expected_reversion  # maker = 0% fee

            if net_edge < 0.02:
                continue

            # Big-moves-only filter: reject if expected reversion is below minimum
            if expected_reversion < self._min_expected_reversion:
                log.debug("mr_rejected_small_reversion", market=pid,
                          expected_reversion=round(expected_reversion, 4),
                          min_required=self._min_expected_reversion)
                continue
```

- [ ] **Step 4: Run all MR tests**

Run: `cd ~/polybot && uv run pytest tests/test_mean_reversion.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/strategies/mean_reversion.py tests/test_mean_reversion.py
git commit -m "feat: add mr_min_expected_reversion filter

Rejects mean reversion trades where expected_reversion is below the
configured minimum (default 0.04). Small-move trades (<2.5% expected
reversion) have a 25% win rate and lose money — this filter removes
them from the trading pipeline.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Create `PriceHistoryScanner` — detect big moves from CLOB price history

The current sliding window only catches moves that happen during the bot's 2-minute scan cycle. This scanner fetches historical price data from the CLOB API for high-volume markets and returns detected big moves.

**Files:**
- Create: `polybot/markets/price_history.py`
- Create: `tests/test_price_history.py`

- [ ] **Step 1: Write the failing test for the move detection pure function**

Create `tests/test_price_history.py`:

```python
import pytest
from polybot.markets.price_history import detect_big_moves


class TestDetectBigMoves:
    def test_detects_drop(self):
        """A 10% drop from 0.60 to 0.50 in the price series should be detected."""
        # Simulate 30 minutes of prices: stable at 0.60, then drops to 0.50
        prices = [0.60] * 15 + [0.55, 0.52, 0.50, 0.50, 0.50]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is not None
        assert result["direction"] == "down"
        assert result["magnitude"] >= 0.10
        assert result["recent_price"] == pytest.approx(0.50)
        assert result["reference_price"] == pytest.approx(0.60)

    def test_detects_spike(self):
        """A 12% spike from 0.40 to 0.52 should be detected."""
        prices = [0.40] * 15 + [0.45, 0.48, 0.50, 0.52, 0.52]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is not None
        assert result["direction"] == "up"
        assert result["magnitude"] >= 0.12

    def test_ignores_small_moves(self):
        """A 3% move should NOT be detected with 5% threshold."""
        prices = [0.50] * 15 + [0.51, 0.52, 0.53, 0.53, 0.53]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is None

    def test_ignores_fully_reverted(self):
        """A move that already fully reverted should NOT be detected."""
        # Went from 0.50 to 0.60, then back to 0.50
        prices = [0.50, 0.55, 0.60, 0.58, 0.55, 0.52, 0.50, 0.50]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is None

    def test_detects_partial_reversion(self):
        """A move that partially reverted should still be detected."""
        # Went from 0.50 to 0.62 (+12%), reverted to 0.58 (still +8% from start)
        prices = [0.50] * 10 + [0.55, 0.60, 0.62, 0.60, 0.58]
        result = detect_big_moves(prices, threshold=0.05)
        assert result is not None
        assert result["direction"] == "up"

    def test_empty_prices(self):
        """Empty price list should return None."""
        assert detect_big_moves([], threshold=0.05) is None

    def test_too_few_prices(self):
        """Less than 3 prices should return None."""
        assert detect_big_moves([0.50, 0.60], threshold=0.05) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_price_history.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'polybot.markets.price_history'`

- [ ] **Step 3: Write the `detect_big_moves` pure function**

Create `polybot/markets/price_history.py`:

```python
"""Price history scanner — finds big moves in CLOB price history data.

Scans high-volume markets for recent large price moves that the mean
reversion sliding window might miss. Uses the CLOB prices-history
endpoint to get minute-level price data.
"""

import structlog

log = structlog.get_logger()


def detect_big_moves(
    prices: list[float],
    threshold: float = 0.05,
) -> dict | None:
    """Detect a significant price move in a price series.

    Compares recent prices (last 20%) against the baseline (first 60%)
    to find moves that haven't fully reverted.

    Args:
        prices: Chronological list of price points (e.g., 1-min intervals).
        threshold: Minimum absolute move to consider significant.

    Returns:
        Dict with direction, magnitude, recent_price, reference_price,
        or None if no significant move detected.
    """
    if len(prices) < 3:
        return None

    # Split into baseline (first 60%) and recent (last 20%)
    baseline_end = max(1, int(len(prices) * 0.6))
    recent_start = max(baseline_end, int(len(prices) * 0.8))

    baseline = prices[:baseline_end]
    recent = prices[recent_start:]

    if not baseline or not recent:
        return None

    baseline_mid = sum(baseline) / len(baseline)
    recent_mid = sum(recent) / len(recent)

    move = recent_mid - baseline_mid

    if abs(move) < threshold:
        return None

    return {
        "direction": "up" if move > 0 else "down",
        "magnitude": abs(move),
        "recent_price": recent_mid,
        "reference_price": baseline_mid,
    }
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_price_history.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/markets/price_history.py tests/test_price_history.py
git commit -m "feat: add detect_big_moves for price history analysis

Pure function that detects significant price moves in CLOB price
history data. Compares recent prices against baseline to find
moves that haven't fully reverted — candidates for mean reversion.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Add `PriceHistoryScanner` class with CLOB API integration

The async class that fetches price history for high-volume markets and returns detected moves.

**Files:**
- Modify: `polybot/markets/price_history.py` (append class)
- Modify: `tests/test_price_history.py` (append tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_price_history.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
from polybot.markets.price_history import PriceHistoryScanner


@pytest.mark.asyncio
async def test_scanner_finds_big_move():
    """Scanner should return move data for a market with a big price move."""
    scanner = MagicMock()
    # Scanner returns markets with volume/token data
    scanner.get_all_cached_prices.return_value = {
        "mkt-1": {
            "yes_price": 0.55, "no_price": 0.45,
            "yes_token_id": "tok_yes_1", "no_token_id": "tok_no_1",
            "volume_24h": 50000, "question": "Big mover?",
            "polymarket_id": "mkt-1",
        },
    }
    scanner.fetch_price_history = AsyncMock(return_value=[
        0.60, 0.60, 0.60, 0.60, 0.60,  # baseline at 0.60
        0.60, 0.60, 0.58, 0.56, 0.55,  # dropped to 0.55
        0.55, 0.54, 0.53, 0.53, 0.53,  # settled at 0.53
    ])

    phs = PriceHistoryScanner(
        scanner=scanner,
        min_volume=1000,
        move_threshold=0.05,
        max_markets=50,
    )
    moves = await phs.scan_for_moves()

    assert len(moves) >= 1
    assert moves[0]["polymarket_id"] == "mkt-1"
    assert moves[0]["direction"] == "down"


@pytest.mark.asyncio
async def test_scanner_skips_low_volume():
    """Markets below min_volume should not be scanned."""
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-low": {
            "yes_price": 0.50, "volume_24h": 100,  # below min_volume
            "yes_token_id": "tok", "polymarket_id": "mkt-low",
            "question": "Low volume?",
        },
    }
    scanner.fetch_price_history = AsyncMock()

    phs = PriceHistoryScanner(scanner=scanner, min_volume=1000)
    moves = await phs.scan_for_moves()

    assert len(moves) == 0
    scanner.fetch_price_history.assert_not_called()


@pytest.mark.asyncio
async def test_scanner_handles_empty_history():
    """Markets with no price history should be skipped gracefully."""
    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-empty": {
            "yes_price": 0.50, "volume_24h": 5000,
            "yes_token_id": "tok", "polymarket_id": "mkt-empty",
            "question": "Empty history?",
        },
    }
    scanner.fetch_price_history = AsyncMock(return_value=[])

    phs = PriceHistoryScanner(scanner=scanner, min_volume=1000)
    moves = await phs.scan_for_moves()

    assert len(moves) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_price_history.py::test_scanner_finds_big_move -v`
Expected: FAIL with `ImportError: cannot import name 'PriceHistoryScanner'`

- [ ] **Step 3: Implement `PriceHistoryScanner`**

Append to `polybot/markets/price_history.py`:

```python
class PriceHistoryScanner:
    """Scans high-volume markets for recent big price moves via CLOB price history.

    Runs as a periodic task, independent of the MR strategy's scan cycle.
    Fetches 2h price history at 1-minute fidelity for the top N markets
    by 24h volume, runs detect_big_moves on each, and returns detected moves.
    """

    def __init__(
        self,
        scanner,
        min_volume: float = 5000.0,
        move_threshold: float = 0.05,
        max_markets: int = 100,
    ):
        self._scanner = scanner
        self._min_volume = min_volume
        self._move_threshold = move_threshold
        self._max_markets = max_markets

    async def scan_for_moves(self) -> list[dict]:
        """Scan top markets by volume for big recent price moves.

        Returns list of dicts with keys: polymarket_id, question,
        yes_price, direction, magnitude, recent_price, reference_price.
        """
        price_cache = self._scanner.get_all_cached_prices()
        if not price_cache:
            return []

        # Filter to high-volume markets and sort by volume descending
        candidates = [
            m for m in price_cache.values()
            if m.get("volume_24h", 0) >= self._min_volume
            and m.get("yes_token_id")
        ]
        candidates.sort(key=lambda m: m.get("volume_24h", 0), reverse=True)
        candidates = candidates[:self._max_markets]

        moves = []
        for m in candidates:
            try:
                prices = await self._scanner.fetch_price_history(
                    m["yes_token_id"], interval="2h")
                if not prices:
                    continue
                result = detect_big_moves(prices, threshold=self._move_threshold)
                if result:
                    moves.append({
                        "polymarket_id": m.get("polymarket_id", ""),
                        "question": m.get("question", ""),
                        "yes_price": m.get("yes_price", 0),
                        **result,
                    })
            except Exception as e:
                log.debug("price_history_scan_error",
                          market=m.get("polymarket_id"), error=str(e))
                continue

        log.info("price_history_scan_complete",
                 scanned=len(candidates), moves_found=len(moves))
        return moves
```

- [ ] **Step 4: Run all price history tests**

Run: `cd ~/polybot && uv run pytest tests/test_price_history.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/markets/price_history.py tests/test_price_history.py
git commit -m "feat: add PriceHistoryScanner for CLOB-based move detection

Scans top markets by 24h volume using CLOB price history endpoint.
Detects big moves that the MR sliding window misses because it only
sees moves that happen during its 2-minute scan cycle.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Wire `PriceHistoryScanner` into engine + inject moves into MR

Add MR strategy method to accept externally-detected moves, wire the scanner as a periodic task in the engine, and instantiate in `__main__.py`.

**Files:**
- Modify: `polybot/strategies/mean_reversion.py` (add `inject_move` method)
- Modify: `polybot/core/engine.py` (add periodic task)
- Modify: `polybot/__main__.py` (instantiate scanner)

- [ ] **Step 1: Add `inject_move` to MR strategy**

In `polybot/strategies/mean_reversion.py`, add this method to the `MeanReversionStrategy` class (after `__init__`):

```python
    def inject_snapshots(self, market_id: str, price: float, old_price: float) -> None:
        """Inject a synthetic snapshot pair from external price history scanning.

        This allows the PriceHistoryScanner to feed detected big moves
        into the MR sliding window, enabling the standard run_once()
        candidate detection to pick them up on the next cycle.
        """
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # Insert the old price as a recent snapshot, then the current price
        # will be added naturally by the next run_once() scan
        self._price_snapshots.setdefault(market_id, [])
        # Only inject if we don't already have snapshots for this market
        if not self._price_snapshots[market_id]:
            self._price_snapshots[market_id] = [
                (old_price, now - timedelta(minutes=5)),
            ]
            log.info("mr_snapshot_injected", market=market_id,
                     old_price=round(old_price, 4), current=round(price, 4))
```

- [ ] **Step 2: Add periodic scanner task to engine**

In `polybot/core/engine.py`, add this method to the `Engine` class:

```python
    async def _scan_price_history(self):
        """Periodic: scan for big moves via CLOB price history and inject into MR."""
        if not self._price_history_scanner:
            return
        mr_strategy = next(
            (s for s in self._strategies if s.name == "mean_reversion"), None)
        if not mr_strategy:
            return

        try:
            moves = await self._price_history_scanner.scan_for_moves()
            for move in moves:
                mr_strategy.inject_snapshots(
                    market_id=move["polymarket_id"],
                    price=move["yes_price"],
                    old_price=move["reference_price"],
                )
        except Exception as e:
            log.error("price_history_scan_error", error=str(e))
```

In `Engine.__init__`, add a `price_history_scanner` parameter (default `None`):

```python
    def __init__(self, db, scanner, researcher, ensemble, executor, recorder,
                 risk_manager, settings, email_notifier, position_manager, clob=None,
                 portfolio_lock=None, trade_learner=None, price_history_scanner=None):
        # ... existing code ...
        self._price_history_scanner = price_history_scanner
```

In `Engine.run_forever`, add the periodic task (after the other periodic tasks):

```python
        if self._price_history_scanner:
            tasks.append(self._run_periodic(self._scan_price_history, 600))  # every 10 min
```

- [ ] **Step 3: Wire in `__main__.py`**

In `polybot/__main__.py`, add the import at the top:

```python
from polybot.markets.price_history import PriceHistoryScanner
```

After the MR strategy creation (around line 142), add:

```python
    price_history_scanner = None
    if getattr(settings, 'mr_enabled', False):
        mr_strategy = MeanReversionStrategy(settings=settings)
        engine.add_strategy(mr_strategy)
        price_history_scanner = PriceHistoryScanner(
            scanner=scanner,
            min_volume=settings.mr_min_volume_24h,
            move_threshold=settings.mr_trigger_threshold,
            max_markets=100,
        )
```

And pass it to the Engine constructor:

```python
    engine = Engine(
        db=db, scanner=scanner, researcher=researcher, ensemble=ensemble,
        executor=executor, recorder=recorder, risk_manager=risk_manager,
        settings=settings, email_notifier=email_notifier,
        position_manager=position_manager, clob=clob,
        portfolio_lock=portfolio_lock, trade_learner=trade_learner,
        price_history_scanner=price_history_scanner)
```

- [ ] **Step 4: Run the full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/strategies/mean_reversion.py polybot/core/engine.py polybot/__main__.py
git commit -m "feat: wire PriceHistoryScanner into engine + MR strategy

PriceHistoryScanner runs every 10 minutes as a periodic engine task.
Detected big moves are injected into MR's snapshot window via
inject_snapshots(), enabling the standard MR candidate detection
to pick them up on the next 2-minute cycle.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Fix `fetch_price_history` to use correct API parameter

The existing `scanner.fetch_price_history()` uses `token_id` as the query parameter, but the CLOB API requires `market` as the parameter name.

**Files:**
- Modify: `polybot/markets/scanner.py:156-163`

- [ ] **Step 1: Fix the parameter name**

In `polybot/markets/scanner.py`, find `fetch_price_history` (line 156):

```python
    async def fetch_price_history(self, token_id: str, interval: str = "1h") -> list[float]:
        status, data = await self._get(
            f"{self._base_url}/prices-history",
            {"token_id": token_id, "interval": interval, "fidelity": 60},
        )
```

Replace with:

```python
    async def fetch_price_history(self, token_id: str, interval: str = "1h") -> list[float]:
        status, data = await self._get(
            f"{self._base_url}/prices-history",
            {"market": token_id, "interval": interval, "fidelity": 1},
        )
```

Changes: `token_id` → `market` (correct CLOB API param), `fidelity` 60 → 1 (1-minute granularity for better move detection).

- [ ] **Step 2: Run full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
cd ~/polybot
git add polybot/markets/scanner.py
git commit -m "fix: use correct CLOB API param for price history

The prices-history endpoint requires 'market' not 'token_id' as the
query parameter. Also switch fidelity from 60 to 1 for minute-level
granularity needed by the PriceHistoryScanner.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Verification

After deploying, monitor for 4-6 hours with these queries:

```sql
-- Check: are we only taking large-move trades now?
SELECT id,
       (kelly_inputs::jsonb->>'expected_reversion')::float as exp_rev,
       ROUND(position_size_usd::numeric, 2) as size,
       exit_reason, ROUND(pnl::numeric, 4) as pnl
FROM trades
WHERE strategy = 'mean_reversion'
  AND opened_at > NOW() - INTERVAL '6 hours'
ORDER BY opened_at DESC;

-- Check: are position sizes larger?
SELECT ROUND(AVG(position_size_usd)::numeric, 2) as avg_size,
       ROUND(AVG(position_size_usd / 479.0 * 100)::numeric, 1) as pct_bankroll
FROM trades
WHERE strategy = 'mean_reversion'
  AND opened_at > NOW() - INTERVAL '6 hours';
```

Expected:
- All `exp_rev` values should be ≥ 0.04
- `avg_size` should be $15-40 (was $5.59)
- Trade frequency may be lower (fewer signals pass the filter), but per-trade PnL should be much higher
