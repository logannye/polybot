# MR Market-Price Fills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MR orders fill instantly by fetching the real-time CLOB price and ordering at market, instead of using the stale scanner price that results in 80% timeout cancellations.

**Architecture:** Add a `get_market_price(token_id)` method to `ClobGateway` that fetches the real-time midpoint/best-ask. MR strategy calls this before placing the order, overriding the scanner's cached price. The fill timeout for MR is also reduced from 120s to 30s since taker orders should fill within seconds.

**Tech Stack:** Python 3.13, py-clob-client, pytest

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/trading/clob.py` | Modify | Add `get_market_price(token_id)` method |
| `polybot/strategies/mean_reversion.py` | Modify | Fetch real-time price before placing order |
| `polybot/core/config.py` | Modify | Add `mr_fill_timeout_seconds` config |
| `tests/test_mean_reversion.py` | Modify | Add test for market-price order flow |

---

### Task 1: Add `get_market_price` to ClobGateway

**Files:**
- Modify: `polybot/trading/clob.py:58` (after `get_balance`)
- Modify: `tests/test_clob.py` (append test)

- [ ] **Step 1: Write failing test**

Append to `tests/test_clob.py`:

```python
@pytest.mark.asyncio
async def test_get_market_price():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_price.return_value = "0.5500"
    gw._client = mock_client
    result = await gw.get_market_price("token123")
    assert abs(result - 0.55) < 0.001
    mock_client.get_price.assert_called_once_with("token123", "buy")


@pytest.mark.asyncio
async def test_get_market_price_fallback():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_price.side_effect = Exception("API error")
    gw._client = mock_client
    result = await gw.get_market_price("token123")
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run python -m pytest tests/test_clob.py::test_get_market_price tests/test_clob.py::test_get_market_price_fallback -v`
Expected: FAIL — `get_market_price` doesn't exist

- [ ] **Step 3: Implement `get_market_price`**

In `polybot/trading/clob.py`, add after the `get_balance` method (after line 62):

```python
    async def get_market_price(self, token_id: str) -> float | None:
        """Fetch real-time buy price for a token from the CLOB."""
        try:
            result = await asyncio.to_thread(self._client.get_price, token_id, "buy")
            return float(result)
        except Exception as e:
            log.warning("clob_get_price_failed", token_id=token_id[:20], error=str(e))
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/polybot && uv run python -m pytest tests/test_clob.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/trading/clob.py tests/test_clob.py
git commit -m "feat: add get_market_price to ClobGateway for real-time CLOB pricing"
```

---

### Task 2: MR Strategy Uses Real-Time Price

**Files:**
- Modify: `polybot/strategies/mean_reversion.py:169-174` (buy_price calculation)
- Modify: `polybot/strategies/mean_reversion.py:285-299` (order placement)

- [ ] **Step 1: Update buy_price to fetch real-time CLOB price**

In `polybot/strategies/mean_reversion.py`, replace the buy_price block at lines 169-174:

Current:
```python
            if move > 0:
                side = "NO"
                buy_price = m.get("no_price", 1.0 - m["yes_price"])
            else:
                side = "YES"
                buy_price = m["yes_price"]
```

Replace with:
```python
            if move > 0:
                side = "NO"
                buy_price = m.get("no_price", 1.0 - m["yes_price"])
            else:
                side = "YES"
                buy_price = m["yes_price"]

            # For taker orders, fetch real-time CLOB price to guarantee fill.
            # Scanner prices are stale; the CLOB price reflects the actual book.
            if not getattr(self._settings, 'mr_use_maker_orders', True) and hasattr(ctx, 'clob') and ctx.clob is not None:
                token_id = m.get("yes_token_id", "") if side == "YES" else m.get("no_token_id", "")
                if token_id:
                    live_price = await ctx.clob.get_market_price(token_id)
                    if live_price is not None:
                        log.debug("mr_live_price", scanner_price=round(buy_price, 4),
                                  clob_price=round(live_price, 4), side=side)
                        buy_price = live_price
```

- [ ] **Step 2: Make clob available in TradingContext**

Check if `ctx.clob` is already available. Search `TradingContext` definition:

Run: `cd ~/polybot && grep -n "class TradingContext" polybot/`

If `clob` is not a field on `TradingContext`, we need to pass it through. Check `__main__.py` where the context is built and add `clob=clob` if needed. The context is a dataclass or namespace — add the field.

- [ ] **Step 3: Run full tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add polybot/strategies/mean_reversion.py polybot/
git commit -m "feat: MR fetches real-time CLOB price for taker orders

Scanner prices are stale by the time the order reaches the CLOB,
causing 80% of MR orders to sit unfilled and timeout. Now MR
fetches the live buy price from the CLOB before placing the order."
```

---

### Task 3: Reduce MR Fill Timeout

**Files:**
- Modify: `polybot/core/config.py` (add `mr_fill_timeout_seconds`)
- Modify: `polybot/core/engine.py:211` (use per-strategy timeout)

- [ ] **Step 1: Add config key**

In `polybot/core/config.py`, in the MR section (after `mr_use_maker_orders`):

```python
    mr_fill_timeout_seconds: float = 30.0   # taker orders should fill in seconds, not minutes
```

- [ ] **Step 2: Update fill monitor to use MR-specific timeout**

In `polybot/core/engine.py`, replace line 211:

Current:
```python
                timeout = (self._settings.arb_fill_timeout_seconds if trade["strategy"] == "arbitrage"
                           else self._settings.fill_timeout_seconds)
```

Replace with:
```python
                if trade["strategy"] == "arbitrage":
                    timeout = self._settings.arb_fill_timeout_seconds
                elif trade["strategy"] == "mean_reversion":
                    timeout = getattr(self._settings, 'mr_fill_timeout_seconds', self._settings.fill_timeout_seconds)
                else:
                    timeout = self._settings.fill_timeout_seconds
```

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py polybot/core/engine.py
git commit -m "config: MR fill timeout 30s (taker orders should fill instantly)"
```
