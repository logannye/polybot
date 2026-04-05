# Expand PriceHistoryScanner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase mean reversion trade frequency by 5-10x through parallel price history scanning of 500 markets every 3 minutes (up from 100 markets every 10 minutes).

**Architecture:** Refactor `PriceHistoryScanner.scan_for_moves()` to fetch price histories in parallel using `asyncio.gather` with a concurrency semaphore (50 concurrent). Update config defaults for `max_markets` (100→500) and engine scan interval (600s→180s). Add config key for scan interval so it's hot-reloadable.

**Tech Stack:** Python 3.13, asyncio, pytest, AsyncMock

---

## Evidence

Empirical scan of live Polymarket data (April 5, 2026):
- Top 100 markets: 4 had 10%+ moves in 24h (4%)
- Markets 500-520: 2/20 had 10%+ moves (10%)
- Extrapolation: ~20-40 opportunities/day across top 500 markets
- Current scanner checks 100 → catches ~4
- Expanded to 500 → catches ~20-40

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `polybot/markets/price_history.py` | Modify | Add parallel fetching with semaphore to `scan_for_moves()` |
| `polybot/core/config.py` | Modify | Add `mr_history_scan_interval` config, update `max_markets` default |
| `polybot/core/engine.py` | Modify | Use config interval instead of hardcoded 600 |
| `polybot/__main__.py` | Modify | Pass updated `max_markets` from config |
| `tests/test_price_history.py` | Modify | Add tests for parallel fetching behavior |

---

### Task 1: Add parallel fetching to PriceHistoryScanner

The current `scan_for_moves()` fetches price history sequentially — one market at a time. With 500 markets at ~200ms each, that's 100 seconds. With `asyncio.gather` and a semaphore limiting to 50 concurrent requests, it drops to ~2 seconds.

**Files:**
- Modify: `polybot/markets/price_history.py:53-104`
- Modify: `tests/test_price_history.py` (append)

- [ ] **Step 1: Write the failing test for parallel fetching**

Append to `tests/test_price_history.py`:

```python
import asyncio


@pytest.mark.asyncio
async def test_scanner_fetches_in_parallel():
    """Scanner should fetch multiple markets concurrently, not sequentially."""
    call_times = []

    async def mock_fetch(token_id, interval="1h"):
        call_times.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.01)  # simulate network latency
        return [0.60] * 12 + [0.50, 0.50, 0.50]  # big move

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        f"mkt-{i}": {
            "yes_price": 0.50, "volume_24h": 10000,
            "yes_token_id": f"tok_{i}", "polymarket_id": f"mkt-{i}",
            "question": f"Market {i}?",
        }
        for i in range(10)
    }
    scanner.fetch_price_history = mock_fetch

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05, max_markets=50,
        concurrency=5)
    moves = await phs.scan_for_moves()

    assert len(moves) == 10  # all 10 had big moves
    # With concurrency=5 and 10 markets, should complete in ~2 batches
    # Sequential would take ~0.1s; parallel should take ~0.02s
    elapsed = call_times[-1] - call_times[0]
    assert elapsed < 0.05  # must be parallel, not sequential


@pytest.mark.asyncio
async def test_scanner_respects_concurrency_limit():
    """Scanner should not exceed the concurrency limit."""
    max_concurrent = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def mock_fetch(token_id, interval="1h"):
        nonlocal max_concurrent, current_concurrent
        async with lock:
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
        await asyncio.sleep(0.02)
        async with lock:
            current_concurrent -= 1
        return [0.60] * 12 + [0.50, 0.50, 0.50]

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        f"mkt-{i}": {
            "yes_price": 0.50, "volume_24h": 10000,
            "yes_token_id": f"tok_{i}", "polymarket_id": f"mkt-{i}",
            "question": f"Market {i}?",
        }
        for i in range(20)
    }
    scanner.fetch_price_history = mock_fetch

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05,
        max_markets=50, concurrency=3)
    await phs.scan_for_moves()

    assert max_concurrent <= 3


@pytest.mark.asyncio
async def test_scanner_one_failure_doesnt_block_others():
    """A single market failing should not prevent other markets from being scanned."""
    call_count = 0

    async def mock_fetch(token_id, interval="1h"):
        nonlocal call_count
        call_count += 1
        if token_id == "tok_bad":
            raise ConnectionError("API down")
        return [0.60] * 12 + [0.50, 0.50, 0.50]

    scanner = MagicMock()
    scanner.get_all_cached_prices.return_value = {
        "mkt-good": {
            "yes_price": 0.50, "volume_24h": 10000,
            "yes_token_id": "tok_good", "polymarket_id": "mkt-good",
            "question": "Good?",
        },
        "mkt-bad": {
            "yes_price": 0.50, "volume_24h": 20000,
            "yes_token_id": "tok_bad", "polymarket_id": "mkt-bad",
            "question": "Bad?",
        },
    }
    scanner.fetch_price_history = mock_fetch

    phs = PriceHistoryScanner(
        scanner=scanner, min_volume=1000, move_threshold=0.05, max_markets=50)
    moves = await phs.scan_for_moves()

    assert call_count == 2  # both were attempted
    assert len(moves) == 1  # only the good one returned a move
    assert moves[0]["polymarket_id"] == "mkt-good"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_price_history.py::test_scanner_fetches_in_parallel -v`
Expected: FAIL — `PriceHistoryScanner.__init__() got an unexpected keyword argument 'concurrency'`

- [ ] **Step 3: Refactor `PriceHistoryScanner` for parallel fetching**

Replace the entire `PriceHistoryScanner` class in `polybot/markets/price_history.py` (lines 53-104) with:

```python
import asyncio


class PriceHistoryScanner:
    """Scans high-volume markets for recent big price moves via CLOB price history.

    Fetches price history in parallel using asyncio.gather with a concurrency
    semaphore to avoid overwhelming the CLOB API.
    """

    def __init__(
        self,
        scanner,
        min_volume: float = 5000.0,
        move_threshold: float = 0.05,
        max_markets: int = 500,
        concurrency: int = 50,
    ):
        self._scanner = scanner
        self._min_volume = min_volume
        self._move_threshold = move_threshold
        self._max_markets = max_markets
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _fetch_one(self, m: dict) -> dict | None:
        """Fetch price history for one market and check for big moves."""
        async with self._semaphore:
            try:
                prices = await self._scanner.fetch_price_history(
                    m["yes_token_id"], interval="2h")
                if not prices:
                    return None
                result = detect_big_moves(prices, threshold=self._move_threshold)
                if result:
                    return {
                        "polymarket_id": m.get("polymarket_id", ""),
                        "question": m.get("question", ""),
                        "yes_price": m.get("yes_price", 0),
                        **result,
                    }
            except Exception as e:
                log.debug("price_history_scan_error",
                          market=m.get("polymarket_id"), error=str(e))
            return None

    async def scan_for_moves(self) -> list[dict]:
        """Scan top markets by volume for big recent price moves."""
        price_cache = self._scanner.get_all_cached_prices()
        if not price_cache:
            return []

        candidates = [
            m for m in price_cache.values()
            if m.get("volume_24h", 0) >= self._min_volume
            and m.get("yes_token_id")
        ]
        candidates.sort(key=lambda m: m.get("volume_24h", 0), reverse=True)
        candidates = candidates[:self._max_markets]

        if not candidates:
            return []

        results = await asyncio.gather(
            *(self._fetch_one(m) for m in candidates),
            return_exceptions=True)

        moves = [r for r in results if isinstance(r, dict)]

        log.info("price_history_scan_complete",
                 scanned=len(candidates), moves_found=len(moves))
        return moves
```

Note: add `import asyncio` near the top of the file (after `import structlog`).

- [ ] **Step 4: Run all price history tests**

Run: `cd ~/polybot && uv run pytest tests/test_price_history.py -v`
Expected: ALL PASS (10 existing + 3 new = 13)

- [ ] **Step 5: Run full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
cd ~/polybot
git add polybot/markets/price_history.py tests/test_price_history.py
git commit -m "feat: parallel price history scanning with semaphore

Refactor PriceHistoryScanner to fetch price histories concurrently
using asyncio.gather with a configurable semaphore (default 50).
500 markets at ~200ms each: 100s sequential → ~2s parallel.

Also increase max_markets default from 100 to 500 to capture 5x
more big-move opportunities.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add config for scan interval and update engine

Make the price history scan interval configurable and reduce the default from 600s to 180s.

**Files:**
- Modify: `polybot/core/config.py` (add `mr_history_scan_interval`)
- Modify: `polybot/core/engine.py:55-56` (use config instead of hardcoded 600)

- [ ] **Step 1: Add config field**

In `polybot/core/config.py`, after the `mr_min_expected_reversion` line (end of MR config block), add:

```python
    mr_history_scan_interval: float = 180.0  # seconds between price history scans
    mr_history_max_markets: int = 500        # max markets to scan per cycle
    mr_history_concurrency: int = 50         # max concurrent API requests
```

- [ ] **Step 2: Update engine to use config interval**

In `polybot/core/engine.py`, find (around line 55-56):

```python
        if self._price_history_scanner:
            tasks.append(self._run_periodic(self._scan_price_history, 600))
```

Replace with:

```python
        if self._price_history_scanner:
            scan_interval = getattr(self._settings, 'mr_history_scan_interval', 180)
            tasks.append(self._run_periodic(self._scan_price_history, scan_interval))
```

- [ ] **Step 3: Update `__main__.py` to pass new config values**

In `polybot/__main__.py`, find where the `PriceHistoryScanner` is created and update to use the new config fields:

```python
        price_history_scanner = PriceHistoryScanner(
            scanner=scanner,
            min_volume=settings.mr_min_volume_24h,
            move_threshold=settings.mr_trigger_threshold,
            max_markets=getattr(settings, 'mr_history_max_markets', 500),
            concurrency=getattr(settings, 'mr_history_concurrency', 50),
        )
```

- [ ] **Step 4: Run full test suite**

Run: `cd ~/polybot && uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py polybot/core/engine.py polybot/__main__.py
git commit -m "feat: configurable scan interval (600s→180s), 500 market default

Add mr_history_scan_interval, mr_history_max_markets, and
mr_history_concurrency config fields. Reduce scan interval from
10 minutes to 3 minutes for faster signal discovery. All values
are hot-reloadable via config.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Verification

After deploying, monitor with:

```sql
-- Check trade frequency compared to before
SELECT opened_at::date as day,
       COUNT(*) as trades,
       ROUND(SUM(COALESCE(pnl, 0))::numeric, 2) as net_pnl,
       ROUND(AVG(position_size_usd)::numeric, 2) as avg_size
FROM trades WHERE strategy = 'mean_reversion'
GROUP BY day ORDER BY day DESC LIMIT 5;
```

Check scanner logs for scan performance:
```bash
# Should see "price_history_scan_complete" every ~3 minutes
# with scanned=500 (or however many pass volume filter)
```

Expected outcome:
- Scan cycle: 500 markets in ~2-5 seconds (parallel)
- Trade frequency: 3/day → 15-20/day
- Per-trade PnL: unchanged (~$3-5 on large moves)
- Daily PnL: $7.58 → $37-75
