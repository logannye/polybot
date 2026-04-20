# MM Live Fill Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the market maker record every order in the trades table and track fills through the existing fill monitor, so MM capital is always accounted for.

**Architecture:** The QuoteManager's `place_two_sided()` method currently submits CLOB orders but doesn't record them in the trades DB. We modify it to accept a `db` parameter, insert each quote as a trade row (status='open', strategy='market_maker'), and store the `clob_order_id`. The existing `engine._fill_monitor` already polls all `status='open'` trades — MM orders will be picked up automatically. MM quotes that get replaced via `requote()` cancel the old trades in the DB. The `analysis_id` column needs to be nullable since MM has no analysis step.

**Tech Stack:** Python 3.13, PostgreSQL, pytest

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| DB `trades` table | Modify | Make `analysis_id` nullable |
| `polybot/trading/quote_manager.py` | Modify | Record quotes as trades in DB |
| `polybot/strategies/market_maker.py` | Modify | Pass DB + market_id to QuoteManager |
| `tests/test_market_maker.py` | Modify | Update tests for DB-backed quotes |

---

### Task 1: Make analysis_id Nullable

**Files:** PostgreSQL schema

- [ ] **Step 1: Alter column**

```sql
ALTER TABLE trades ALTER COLUMN analysis_id DROP NOT NULL;
```

- [ ] **Step 2: Verify**

```sql
SELECT column_name, is_nullable FROM information_schema.columns 
WHERE table_name = 'trades' AND column_name = 'analysis_id';
```

Expected: `is_nullable = YES`

---

### Task 2: QuoteManager Records Trades in DB

**Files:**
- Modify: `polybot/trading/quote_manager.py`

- [ ] **Step 1: Read the current file**

Read `polybot/trading/quote_manager.py` fully.

- [ ] **Step 2: Add db parameter and trade recording**

The `QuoteManager.__init__` currently takes `clob, settings, dry_run`. Add `db=None`:

```python
class QuoteManager:
    def __init__(self, clob, settings, dry_run: bool = False, db=None):
        self._clob = clob
        self._settings = settings
        self._dry_run = dry_run
        self._db = db
        self._active_quotes: dict[str, tuple[Quote | None, Quote | None]] = {}
```

- [ ] **Step 3: Modify `place_two_sided` to record trades**

In the live (non-dry-run) branch of `place_two_sided`, after each successful CLOB order submission, insert a trade row. The method needs a `market_id` parameter (the DB market ID, not the polymarket_id).

Update the signature:
```python
    async def place_two_sided(
        self, market: ActiveMarket, bid_price: float, bid_size: float,
        ask_price: float, ask_size: float, market_id: int | None = None,
    ) -> tuple[str | None, str | None]:
```

In the live branch, after the bid CLOB submission succeeds (after line `bid_quote = Quote(...)`), add:

```python
                if self._db and market_id:
                    bid_quote.trade_id = await self._db.fetchval(
                        """INSERT INTO trades (market_id, analysis_id, side, entry_price,
                           position_size_usd, shares, kelly_inputs, status, strategy, clob_order_id)
                           VALUES ($1, NULL, 'YES', $2, $3, $4, '{}', 'open', 'market_maker', $5)
                           RETURNING id""",
                        market_id, bid_price, round(bid_size * bid_price, 2), bid_size, bid_id)
                    await self._db.execute(
                        "UPDATE system_state SET total_deployed = total_deployed + $1 WHERE id = 1",
                        round(bid_size * bid_price, 2))
```

Same for the ask side (after ask CLOB submission succeeds):

```python
                if self._db and market_id:
                    ask_quote.trade_id = await self._db.fetchval(
                        """INSERT INTO trades (market_id, analysis_id, side, entry_price,
                           position_size_usd, shares, kelly_inputs, status, strategy, clob_order_id)
                           VALUES ($1, NULL, 'NO', $2, $3, $4, '{}', 'open', 'market_maker', $5)
                           RETURNING id""",
                        market_id, ask_price, round(ask_size * ask_price, 2), ask_size, ask_id)
                    await self._db.execute(
                        "UPDATE system_state SET total_deployed = total_deployed + $1 WHERE id = 1",
                        round(ask_size * ask_price, 2))
```

Also add `trade_id: int | None = None` to the `Quote` dataclass:

```python
@dataclass
class Quote:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    posted_at: datetime
    status: str = "live"
    trade_id: int | None = None
```

- [ ] **Step 4: Modify `cancel_market_quotes` to cancel trades in DB**

In `cancel_market_quotes`, after cancelling CLOB orders, also cancel the trade rows:

```python
    async def cancel_market_quotes(self, polymarket_id: str) -> None:
        if not self._dry_run:
            old_bid, old_ask = self._active_quotes.get(polymarket_id, (None, None))
            ids_to_cancel = []
            if old_bid and old_bid.status == "live":
                ids_to_cancel.append(old_bid.order_id)
            if old_ask and old_ask.status == "live":
                ids_to_cancel.append(old_ask.order_id)
            if ids_to_cancel:
                await self._clob.cancel_orders_batch(ids_to_cancel)
            # Cancel trade rows in DB and free deployed capital
            if self._db:
                for quote in [old_bid, old_ask]:
                    if quote and quote.trade_id:
                        size = round(quote.size * quote.price, 2)
                        await self._db.execute(
                            "UPDATE trades SET status = 'cancelled' WHERE id = $1", quote.trade_id)
                        await self._db.execute(
                            "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                            size)
        self._active_quotes.pop(polymarket_id, None)
```

- [ ] **Step 5: Pass `market_id` through `requote`**

Update `requote` to accept and pass `market_id`:

```python
    async def requote(
        self, market: ActiveMarket, new_bid: float, new_bid_size: float,
        new_ask: float, new_ask_size: float, threshold: float = 0.005,
        market_id: int | None = None,
    ) -> None:
        old_bid, old_ask = self._active_quotes.get(market.polymarket_id, (None, None))
        bid_moved = old_bid is None or abs(new_bid - old_bid.price) > threshold
        ask_moved = old_ask is None or abs(new_ask - old_ask.price) > threshold
        if not bid_moved and not ask_moved:
            return
        await self.cancel_market_quotes(market.polymarket_id)
        await self.place_two_sided(market, new_bid, new_bid_size, new_ask, new_ask_size,
                                   market_id=market_id)
        log.debug("mm_requoted", market=market.polymarket_id,
                  bid=round(new_bid, 4), ask=round(new_ask, 4))
```

- [ ] **Step 6: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`

Fix any failures from the signature changes (existing MM tests may need `market_id=None` added to `requote` calls).

- [ ] **Step 7: Commit**

```bash
cd ~/polybot && git add polybot/trading/quote_manager.py
git commit -m "feat: QuoteManager records MM orders in trades table

Every bid/ask quote is now inserted as a trade row with strategy='market_maker'
and clob_order_id. Cancelled quotes update status='cancelled' and free capital.
The existing fill_monitor picks up fills automatically.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire DB and Market ID into MM Strategy

**Files:**
- Modify: `polybot/strategies/market_maker.py`

- [ ] **Step 1: Pass db to QuoteManager**

In `MarketMakerStrategy.__init__`, the QuoteManager is created at line ~38:

```python
        self._quote_mgr = quote_manager or QuoteManager(
            clob=clob, settings=settings, dry_run=dry_run)
```

Change to:

```python
        self._quote_mgr = quote_manager or QuoteManager(
            clob=clob, settings=settings, dry_run=dry_run, db=None)
```

We'll set the `db` from the context in `run_once`.

- [ ] **Step 2: Set db on first run and pass market_id to requote**

In `run_once`, at the top (after the heartbeat), add:

```python
        # Ensure QuoteManager has DB access for trade tracking
        if self._quote_mgr._db is None and hasattr(ctx, 'db'):
            self._quote_mgr._db = ctx.db
```

In `_manage_quotes`, the `requote` call (around line 223) currently is:

```python
        await self._quote_mgr.requote(
            market, bid_price, bid_size, ask_price, ask_size,
            threshold=s.mm_requote_threshold)
```

We need to pass the DB market_id. Add a market_id lookup before the requote call:

```python
        # Look up DB market_id for trade tracking
        db_market_id = None
        if hasattr(self, '_market_db_ids') and market.polymarket_id in self._market_db_ids:
            db_market_id = self._market_db_ids[market.polymarket_id]
        elif not self._dry_run and ctx.db:
            db_market_id = await ctx.db.fetchval(
                "SELECT id FROM markets WHERE polymarket_id = $1", market.polymarket_id)
            if db_market_id:
                if not hasattr(self, '_market_db_ids'):
                    self._market_db_ids = {}
                self._market_db_ids[market.polymarket_id] = db_market_id

        await self._quote_mgr.requote(
            market, bid_price, bid_size, ask_price, ask_size,
            threshold=s.mm_requote_threshold, market_id=db_market_id)
```

Note: `_manage_quotes` needs access to `ctx`. Currently it only receives `market` and `ctx`. Check the call site — it already passes `ctx`:

```python
        for market in list(self._active_markets.values()):
            try:
                await self._manage_quotes(market, ctx)
```

Good — `ctx` is available.

- [ ] **Step 3: Pass ctx to _manage_quotes (verify it's already there)**

Read the `_manage_quotes` signature. It already takes `ctx`:

```python
    async def _manage_quotes(self, market: ActiveMarket, ctx) -> None:
```

Good — no change needed.

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
All tests must pass.

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/strategies/market_maker.py
git commit -m "feat: MM passes DB and market_id to QuoteManager for trade tracking

Every MM quote is now recorded in the trades table via QuoteManager.
The fill_monitor automatically detects when MM orders fill or cancel.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: MM Fill Timeout — Don't Timeout MM Orders

**Files:**
- Modify: `polybot/core/engine.py`

MM orders should NOT be timed out by the fill monitor — they're meant to sit on the book until filled or replaced by requote. Add MM to the timeout exclusion.

- [ ] **Step 1: Skip MM orders in timeout check**

In `_fill_monitor` (around line 287-300), the timeout logic is:

```python
            elif status["status"] == "live":
                elapsed = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
                if trade["strategy"] == "arbitrage":
                    timeout = self._settings.arb_fill_timeout_seconds
                elif trade["strategy"] == "mean_reversion":
                    timeout = getattr(self._settings, 'mr_fill_timeout_seconds', ...)
                else:
                    timeout = self._settings.fill_timeout_seconds
                if elapsed > timeout:
```

Add MM exclusion before the timeout block:

```python
            elif status["status"] == "live":
                # MM orders are managed by QuoteManager (requote cycle), not fill timeout
                if trade["strategy"] == "market_maker":
                    continue
                elapsed = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
```

- [ ] **Step 2: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add polybot/core/engine.py
git commit -m "feat: fill monitor skips MM orders (managed by requote cycle)

MM orders sit on the book until filled or replaced — they should not
be cancelled by the fill timeout that applies to MR/forecast/snipe.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
