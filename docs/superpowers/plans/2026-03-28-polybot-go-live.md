# Polybot Go-Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire real Polymarket CLOB exchange integration so Polybot can place, monitor, and resolve actual trades — with a dry-run observation mode as the safe default.

**Architecture:** A `ClobGateway` async wrapper around `py-clob-client` is injected into the `OrderExecutor` at construction. The executor owns the `dry_run` flag and `clob` reference — strategies call `place_order()` with the same signature in both modes. Two new engine coroutines handle fill monitoring (30s poll) and resolution detection (60s poll).

**Tech Stack:** py-clob-client (Polymarket SDK), asyncio.to_thread (sync→async bridge), python-dotenv (setup script), asyncpg (DB).

**Spec:** `docs/superpowers/specs/2026-03-28-polybot-go-live-design.md`

---

## File Map

**New files:**
- `polybot/trading/clob.py` — ClobGateway async wrapper around py-clob-client
- `scripts/derive_creds.py` — One-time credential derivation utility
- `tests/test_clob.py` — ClobGateway unit tests
- `tests/test_fill_monitor.py` — Fill polling, timeout, cancel tests
- `tests/test_resolution_monitor.py` — Resolution detection + dry-run resolution tests
- `tests/test_dry_run.py` — Dry-run mode integration tests

**Modified files:**
- `polybot/core/config.py` — Add 4 new settings (api_secret, api_passphrase, chain_id, dry_run)
- `polybot/strategies/base.py` — Add `clob` field to TradingContext
- `polybot/trading/executor.py` — Wire CLOB submission, dry-run status, clob_order_id storage
- `polybot/trading/wallet.py` — Remove `sign_order()` stub
- `polybot/core/engine.py` — Add `_fill_monitor()`, `_resolution_monitor()`, daily_pnl reset, startup bankroll sync
- `polybot/notifications/email.py` — Dry-run subject prefix
- `polybot/dashboard/app.py` — Label dry_run trades
- `polybot/__main__.py` — Construct ClobGateway, inject into executor and context
- `polybot/db/schema.sql` — New column (clob_order_id), expanded status constraint
- `.env.example` — New env vars
- `pyproject.toml` — Add python-dotenv dev dependency

---

### Task 1: Schema & Config for Go-Live

**Files:**
- Modify: `polybot/db/schema.sql`
- Modify: `polybot/core/config.py`
- Modify: `.env.example`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update schema.sql**

Append before the index block at the end of `polybot/db/schema.sql`:

```sql
-- v2.1: CLOB order tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS clob_order_id TEXT;

-- v2.1: Expand trade status for dry-run and fill tracking
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_status_check;
ALTER TABLE trades ADD CONSTRAINT trades_status_check
    CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'closed',
                      'dry_run', 'dry_run_resolved'));
```

Add new index at the end:

```sql
CREATE INDEX IF NOT EXISTS idx_trades_clob_order_id ON trades(clob_order_id);
```

- [ ] **Step 2: Add new settings to config.py**

Add these 4 fields to `polybot/core/config.py` in the `Settings` class, after the `alert_email` field:

```python
    # CLOB L2 credentials (pre-derived via scripts/derive_creds.py)
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_chain_id: int = 137  # Polygon mainnet

    # Dry-run mode (safe default — must set false for live trading)
    dry_run: bool = True
```

Note: `api_secret` and `api_passphrase` default to empty string so the bot can start in dry-run mode without CLOB credentials configured.

- [ ] **Step 3: Update .env.example**

Append to `.env.example`:

```bash
# Polymarket CLOB L2 Credentials (run: uv run python scripts/derive_creds.py)
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

# Dry-run mode (set to false for live trading)
DRY_RUN=true
```

- [ ] **Step 4: Add python-dotenv to dev dependencies**

In `pyproject.toml`, add `"python-dotenv>=1.0",` to the `[project.optional-dependencies] dev` list.

- [ ] **Step 5: Commit**

```bash
git add polybot/db/schema.sql polybot/core/config.py .env.example pyproject.toml
git commit -m "chore: schema and config for go-live — clob_order_id, dry_run, CLOB creds"
```

---

### Task 2: ClobGateway Async Wrapper

**Files:**
- Create: `polybot/trading/clob.py`
- Create: `tests/test_clob.py`

- [ ] **Step 1: Write tests for ClobGateway**

Create `tests/test_clob.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from polybot.trading.clob import ClobGateway


def test_clob_gateway_constructs():
    gw = ClobGateway(
        host="https://clob.polymarket.com",
        chain_id=137,
        private_key="0x" + "a" * 64,
        api_key="test-key",
        api_secret="test-secret",
        api_passphrase="test-pass",
    )
    assert gw is not None


@pytest.mark.asyncio
async def test_submit_order_returns_order_id():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_order = MagicMock()
    mock_client.create_order.return_value = mock_order
    mock_client.post_order.return_value = {"orderID": "order-123"}
    gw._client = mock_client
    result = await gw.submit_order(
        token_id="tok-abc", side="BUY", price=0.55, size=10.0)
    assert result == "order-123"
    mock_client.create_order.assert_called_once()
    mock_client.post_order.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_order_returns_true():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.cancel.return_value = {"canceled": True}
    gw._client = mock_client
    result = await gw.cancel_order("order-123")
    assert result is True


@pytest.mark.asyncio
async def test_get_order_status():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_order.return_value = {
        "status": "MATCHED",
        "size_matched": "10.0",
        "associate_trades": [{"price": "0.55"}],
    }
    gw._client = mock_client
    result = await gw.get_order_status("order-123")
    assert result["status"] == "matched"
    assert result["size_matched"] == 10.0


@pytest.mark.asyncio
async def test_get_balance():
    gw = ClobGateway.__new__(ClobGateway)
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {"balance": "150.50"}
    gw._client = mock_client
    result = await gw.get_balance()
    assert abs(result - 150.50) < 0.01
```

- [ ] **Step 2: Run tests, confirm FAIL**

Run: `uv run pytest tests/test_clob.py -v`

- [ ] **Step 3: Create polybot/trading/clob.py**

```python
import asyncio
import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

log = structlog.get_logger()


class ClobGateway:
    def __init__(self, host: str, chain_id: int, private_key: str,
                 api_key: str, api_secret: str, api_passphrase: str):
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        self._client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=private_key,
            creds=creds,
        )
        log.info("clob_gateway_initialized", address=self._client.get_address())

    async def submit_order(self, token_id: str, side: str, price: float,
                           size: float, order_type: str = "GTC") -> str:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        ot = getattr(OrderType, order_type, OrderType.GTC)

        def _create_and_post():
            signed_order = self._client.create_order(order_args)
            result = self._client.post_order(signed_order, orderType=ot)
            return result

        result = await asyncio.to_thread(_create_and_post)
        order_id = result.get("orderID") or result.get("id", "")
        log.info("clob_order_submitted", order_id=order_id, token_id=token_id,
                 side=side, price=price, size=size)
        return order_id

    async def cancel_order(self, clob_order_id: str) -> bool:
        def _cancel():
            return self._client.cancel(clob_order_id)

        try:
            result = await asyncio.to_thread(_cancel)
            log.info("clob_order_cancelled", order_id=clob_order_id)
            return bool(result.get("canceled", False))
        except Exception as e:
            log.error("clob_cancel_failed", order_id=clob_order_id, error=str(e))
            return False

    async def get_order_status(self, clob_order_id: str) -> dict:
        def _get():
            return self._client.get_order(clob_order_id)

        result = await asyncio.to_thread(_get)
        status_raw = result.get("status", "").upper()
        status_map = {"LIVE": "live", "MATCHED": "matched", "CANCELLED": "cancelled",
                      "CANCELED": "cancelled"}
        return {
            "status": status_map.get(status_raw, status_raw.lower()),
            "size_matched": float(result.get("size_matched", 0)),
        }

    async def get_balance(self) -> float:
        def _get():
            return self._client.get_balance_allowance()

        result = await asyncio.to_thread(_get)
        return float(result.get("balance", 0))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_clob.py -v`

- [ ] **Step 5: Commit**

```bash
git add polybot/trading/clob.py tests/test_clob.py
git commit -m "feat: add ClobGateway async wrapper for Polymarket CLOB API"
```

---

### Task 3: TradingContext + Wallet Cleanup

**Files:**
- Modify: `polybot/strategies/base.py`
- Modify: `polybot/trading/wallet.py`
- Modify: `tests/test_base_strategy.py`

- [ ] **Step 1: Add clob field to TradingContext**

In `polybot/strategies/base.py`, add `clob: Any = None` to the `TradingContext` dataclass:

```python
@dataclass
class TradingContext:
    db: Any
    scanner: Any
    risk_manager: Any
    portfolio_lock: asyncio.Lock
    executor: Any
    email_notifier: Any
    settings: Any
    clob: Any = None  # ClobGateway instance (None in tests/dry-run)
```

- [ ] **Step 2: Remove sign_order from wallet.py**

In `polybot/trading/wallet.py`, delete the `sign_order` method (lines 26-27):

```python
    def sign_order(self, order_data: dict) -> dict:
        return {"signature": "0x...", "order": order_data}
```

- [ ] **Step 3: Update test**

In `tests/test_base_strategy.py`, update `make_context()` to include `clob=None`:

```python
def make_context():
    return TradingContext(
        db=MagicMock(), scanner=MagicMock(), risk_manager=MagicMock(),
        portfolio_lock=asyncio.Lock(), executor=MagicMock(),
        email_notifier=MagicMock(), settings=MagicMock(), clob=None)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_base_strategy.py -v`

- [ ] **Step 5: Commit**

```bash
git add polybot/strategies/base.py polybot/trading/wallet.py tests/test_base_strategy.py
git commit -m "feat: add clob to TradingContext, remove sign_order stub"
```

---

### Task 4: Executor CLOB Integration + Dry-Run

**Files:**
- Modify: `polybot/trading/executor.py`
- Modify: `tests/test_executor.py`
- Create: `tests/test_dry_run.py`

- [ ] **Step 1: Write tests for CLOB submission and dry-run**

Add to `tests/test_executor.py`:

```python
@pytest.mark.asyncio
async def test_place_order_dry_run_skips_clob():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    clob = AsyncMock()
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db,
                              fill_timeout_seconds=120, clob=clob, dry_run=True)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1)
    assert result is not None
    assert result["order_id"] is None  # no CLOB submission
    clob.submit_order.assert_not_called()
    # Check DB was called with 'dry_run' status
    call_sql = db.fetchval.call_args[0][0]
    assert "dry_run" in str(db.fetchval.call_args)


@pytest.mark.asyncio
async def test_place_order_live_calls_clob():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    clob = AsyncMock()
    clob.submit_order = AsyncMock(return_value="order-abc")
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db,
                              fill_timeout_seconds=120, clob=clob, dry_run=False)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1)
    assert result is not None
    assert result["order_id"] == "order-abc"
    clob.submit_order.assert_called_once()


@pytest.mark.asyncio
async def test_place_order_clob_failure_cancels_trade():
    db = AsyncMock()
    db.fetchval = AsyncMock(return_value=1)
    db.execute = AsyncMock()
    wallet = MagicMock()
    wallet.compute_shares = MagicMock(return_value=10.0)
    clob = AsyncMock()
    clob.submit_order = AsyncMock(side_effect=Exception("CLOB down"))
    executor = OrderExecutor(scanner=MagicMock(), wallet=wallet, db=db,
                              fill_timeout_seconds=120, clob=clob, dry_run=False)
    result = await executor.place_order(
        token_id="tok", side="YES", size_usd=5.0, price=0.50,
        market_id=1, analysis_id=1)
    assert result is None  # failed
    # Verify trade was cancelled in DB
    cancel_call = [c for c in db.execute.call_args_list if "cancelled" in str(c)]
    assert len(cancel_call) > 0
```

- [ ] **Step 2: Run tests, confirm FAIL**

Run: `uv run pytest tests/test_executor.py::test_place_order_dry_run_skips_clob -v`

- [ ] **Step 3: Rewrite executor.py**

Replace the entire `polybot/trading/executor.py`:

```python
import structlog
from datetime import datetime, timezone

log = structlog.get_logger()


def compute_limit_price(side: str, best_bid: float, best_ask: float,
                        is_exit: bool = False, cross_spread: bool = False) -> float:
    if is_exit:
        return round(best_bid, 4)
    if cross_spread:
        return round(best_ask, 4)
    spread = best_ask - best_bid
    tick = max(0.001, spread * 0.1)
    price = best_bid + tick
    price = min(price, best_ask)
    return round(price, 4)


class OrderExecutor:
    def __init__(self, scanner, wallet, db, fill_timeout_seconds: int = 120,
                 clob=None, dry_run: bool = False):
        self._scanner = scanner
        self._wallet = wallet
        self._db = db
        self._fill_timeout_seconds = fill_timeout_seconds
        self._clob = clob
        self._dry_run = dry_run

    def should_cancel_order(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds > self._fill_timeout_seconds

    async def place_order(self, token_id, side, size_usd, price, market_id, analysis_id,
                          strategy: str = "forecast"):
        shares = self._wallet.compute_shares(size_usd, price)
        if shares <= 0:
            return None

        status = "dry_run" if self._dry_run else "open"
        order_data = {
            "token_id": token_id, "side": "BUY",
            "size": str(shares), "price": str(price), "type": "GTC",
        }
        log.info("placing_order", market_id=market_id, side=side, size_usd=size_usd,
                 price=price, shares=shares, strategy=strategy, dry_run=self._dry_run)

        trade_id = await self._db.fetchval(
            """INSERT INTO trades (market_id, analysis_id, side, entry_price, position_size_usd,
               shares, kelly_inputs, status, strategy)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
            market_id, analysis_id, side, price, size_usd, shares, "{}", status, strategy)

        clob_order_id = None
        if not self._dry_run and self._clob is not None:
            try:
                clob_order_id = await self._clob.submit_order(
                    token_id=token_id, side=side, price=price, size=shares)
                await self._db.execute(
                    "UPDATE trades SET clob_order_id = $1 WHERE id = $2",
                    clob_order_id, trade_id)
            except Exception as e:
                log.error("clob_submit_failed", trade_id=trade_id, error=str(e))
                await self._db.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade_id)
                return None

        return {"trade_id": trade_id, "order_id": clob_order_id, "shares": shares}

    async def place_multi_leg_order(self, legs: list[dict], strategy: str = "arbitrage") -> list[dict | None]:
        results = []
        for leg in legs:
            result = await self.place_order(
                token_id=leg["token_id"], side=leg["side"],
                size_usd=leg["size_usd"], price=leg["price"],
                market_id=leg["market_id"], analysis_id=leg.get("analysis_id"),
                strategy=strategy)
            results.append(result)
        return results

    async def close_position(self, trade_id, exit_price, exit_reason, shares, entry_price, side):
        if side == "YES":
            pnl = shares * (exit_price - entry_price)
        else:
            pnl = shares * ((1 - exit_price) - (1 - entry_price))
        await self._db.execute(
            """UPDATE trades SET status='closed', exit_price=$1, exit_reason=$2, pnl=$3, closed_at=$4 WHERE id=$5""",
            exit_price, exit_reason, pnl, datetime.now(timezone.utc), trade_id)
        log.info("position_closed", trade_id=trade_id, pnl=pnl, reason=exit_reason)
        return pnl
```

- [ ] **Step 4: Run ALL executor tests**

Run: `uv run pytest tests/test_executor.py -v`

Fix any existing tests that break due to the new `clob`/`dry_run` constructor params (add `clob=None, dry_run=False` to existing test constructors).

- [ ] **Step 5: Commit**

```bash
git add polybot/trading/executor.py tests/test_executor.py
git commit -m "feat: wire CLOB submission and dry-run mode into executor"
```

---

### Task 5: Email Dry-Run Prefix

**Files:**
- Modify: `polybot/notifications/email.py`
- Modify: `tests/test_notifications.py`

- [ ] **Step 1: Write test**

Add to `tests/test_notifications.py`:

```python
def test_email_dry_run_prefix():
    from polybot.notifications.email import EmailNotifier
    notifier = EmailNotifier(api_key="test", to_email="test@test.com", dry_run=True)
    # The send method should prepend [DRY RUN] — we test the subject transformation
    assert notifier._format_subject("Trade executed") == "[Polybot] [DRY RUN] Trade executed"


def test_email_live_no_prefix():
    from polybot.notifications.email import EmailNotifier
    notifier = EmailNotifier(api_key="test", to_email="test@test.com", dry_run=False)
    assert notifier._format_subject("Trade executed") == "[Polybot] Trade executed"
```

- [ ] **Step 2: Run tests, confirm FAIL**

- [ ] **Step 3: Update EmailNotifier**

In `polybot/notifications/email.py`, update the `EmailNotifier` class:

```python
class EmailNotifier:
    def __init__(self, api_key: str, to_email: str, dry_run: bool = False):
        resend.api_key = api_key
        self._to = to_email
        self._dry_run = dry_run

    def _format_subject(self, subject: str) -> str:
        prefix = "[Polybot] [DRY RUN] " if self._dry_run else "[Polybot] "
        return f"{prefix}{subject}"

    async def send(self, subject: str, html: str) -> None:
        formatted_subject = self._format_subject(subject)
        try:
            resend.Emails.send({
                "from": "Polybot <alerts@polybot.dev>",
                "to": [self._to],
                "subject": formatted_subject,
                "html": html,
            })
            log.info("email_sent", subject=formatted_subject)
        except Exception as e:
            log.error("email_failed", subject=formatted_subject, error=str(e))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_notifications.py -v`

- [ ] **Step 5: Commit**

```bash
git add polybot/notifications/email.py tests/test_notifications.py
git commit -m "feat: add dry-run prefix to email notifications"
```

---

### Task 6: Fill Monitor

**Files:**
- Modify: `polybot/core/engine.py`
- Create: `tests/test_fill_monitor.py`

- [ ] **Step 1: Write fill monitor tests**

Create `tests/test_fill_monitor.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from polybot.core.engine import Engine


def make_engine(**kwargs):
    defaults = dict(
        db=AsyncMock(), scanner=MagicMock(), researcher=MagicMock(),
        ensemble=MagicMock(), executor=MagicMock(), recorder=MagicMock(),
        risk_manager=MagicMock(),
        settings=MagicMock(
            health_check_interval=60, heartbeat_warn_seconds=600,
            heartbeat_critical_seconds=1800, balance_divergence_pct=0.05,
            strategy_kill_min_trades=50, daily_loss_limit_pct=0.15,
            fill_timeout_seconds=120, arb_fill_timeout_seconds=30,
            dry_run=False, post_breaker_cooldown_hours=24),
        email_notifier=AsyncMock(), position_manager=MagicMock(),
        clob=AsyncMock())
    defaults.update(kwargs)
    return Engine(**defaults)


@pytest.mark.asyncio
async def test_fill_monitor_matched_order():
    """When CLOB reports matched, trade should transition to filled."""
    engine = make_engine()
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "clob_order_id": "order-1", "strategy": "forecast",
         "position_size_usd": 5.0, "opened_at": datetime.now(timezone.utc)},
    ])
    engine._clob = AsyncMock()
    engine._clob.get_order_status = AsyncMock(return_value={"status": "matched", "size_matched": 10.0})
    engine._clob.get_balance = AsyncMock(return_value=95.0)
    engine._db.execute = AsyncMock()

    await engine._fill_monitor()

    # Should have updated trade to 'filled' and synced bankroll
    calls = [str(c) for c in engine._db.execute.call_args_list]
    assert any("filled" in c for c in calls)
    assert any("bankroll" in c for c in calls)


@pytest.mark.asyncio
async def test_fill_monitor_timeout_cancels():
    """Order past timeout should be cancelled."""
    engine = make_engine()
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "clob_order_id": "order-1", "strategy": "forecast",
         "position_size_usd": 5.0,
         "opened_at": datetime.now(timezone.utc) - timedelta(seconds=200)},
    ])
    engine._clob = AsyncMock()
    engine._clob.get_order_status = AsyncMock(return_value={"status": "live", "size_matched": 0})
    engine._clob.cancel_order = AsyncMock(return_value=True)
    engine._db.execute = AsyncMock()

    await engine._fill_monitor()

    engine._clob.cancel_order.assert_called_once_with("order-1")
    calls = [str(c) for c in engine._db.execute.call_args_list]
    assert any("cancelled" in c for c in calls)
    assert any("total_deployed" in c for c in calls)


@pytest.mark.asyncio
async def test_fill_monitor_arb_shorter_timeout():
    """Arb orders should use arb_fill_timeout_seconds (30s)."""
    engine = make_engine()
    # 35s old arb order — should be timed out (arb timeout = 30s)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "clob_order_id": "order-1", "strategy": "arbitrage",
         "position_size_usd": 5.0,
         "opened_at": datetime.now(timezone.utc) - timedelta(seconds=35)},
    ])
    engine._clob = AsyncMock()
    engine._clob.get_order_status = AsyncMock(return_value={"status": "live", "size_matched": 0})
    engine._clob.cancel_order = AsyncMock(return_value=True)
    engine._db.execute = AsyncMock()

    await engine._fill_monitor()

    engine._clob.cancel_order.assert_called_once()
```

- [ ] **Step 2: Run tests, confirm FAIL**

Run: `uv run pytest tests/test_fill_monitor.py -v`

- [ ] **Step 3: Add _fill_monitor to engine.py**

Add this method to the `Engine` class in `polybot/core/engine.py`:

```python
    async def _fill_monitor(self):
        """Poll CLOB for order status. Transition open orders to filled/cancelled."""
        open_orders = await self._db.fetch(
            "SELECT * FROM trades WHERE status = 'open' AND clob_order_id IS NOT NULL")

        for trade in open_orders:
            try:
                status = await self._clob.get_order_status(trade["clob_order_id"])
            except Exception as e:
                log.error("fill_check_failed", trade_id=trade["id"], error=str(e))
                continue

            if status["status"] == "matched":
                await self._db.execute(
                    "UPDATE trades SET status = 'filled' WHERE id = $1", trade["id"])
                # Sync bankroll from wallet
                try:
                    balance = await self._clob.get_balance()
                    await self._db.execute(
                        "UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                except Exception as e:
                    log.error("bankroll_sync_failed", error=str(e))
                log.info("order_filled", trade_id=trade["id"],
                         clob_order_id=trade["clob_order_id"])

            elif status["status"] == "cancelled":
                await self._db.execute(
                    "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade["id"])
                await self._db.execute(
                    "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                    float(trade["position_size_usd"]))
                log.info("order_cancelled_externally", trade_id=trade["id"])

            elif status["status"] == "live":
                elapsed = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
                timeout = (self._settings.arb_fill_timeout_seconds
                           if trade["strategy"] == "arbitrage"
                           else self._settings.fill_timeout_seconds)
                if elapsed > timeout:
                    await self._clob.cancel_order(trade["clob_order_id"])
                    await self._db.execute(
                        "UPDATE trades SET status = 'cancelled' WHERE id = $1", trade["id"])
                    await self._db.execute(
                        "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                        float(trade["position_size_usd"]))
                    log.info("order_timed_out", trade_id=trade["id"], elapsed=elapsed)
```

Also update `Engine.__init__` to accept and store `clob`:

```python
    def __init__(self, db, scanner, researcher, ensemble, executor, recorder,
                 risk_manager, settings, email_notifier, position_manager, clob=None):
        # ... existing fields ...
        self._clob = clob
```

And update `run_forever()` to add the fill monitor (only in live mode):

```python
    async def run_forever(self):
        log.info("engine_starting", strategies=[s.name for s in self._strategies])
        await self._reconcile_on_startup()
        tasks = [self._run_strategy(s) for s in self._strategies]
        tasks.append(self._run_periodic(self._health_check, self._settings.health_check_interval))
        tasks.append(self._run_periodic(self._maybe_self_assess, 60))
        if not self._settings.dry_run and self._clob:
            tasks.append(self._run_periodic(self._fill_monitor, 30))
        tasks.append(self._run_periodic(self._resolution_monitor, 60))
        await asyncio.gather(*tasks)
```

Note: `_resolution_monitor` is added in Task 7. For now, add the reference — it will be implemented next.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_fill_monitor.py -v`

- [ ] **Step 5: Commit**

```bash
git add polybot/core/engine.py tests/test_fill_monitor.py
git commit -m "feat: add fill monitor — 30s CLOB polling with auto-cancel on timeout"
```

---

### Task 7: Resolution Monitor

**Files:**
- Modify: `polybot/core/engine.py`
- Create: `tests/test_resolution_monitor.py`

- [ ] **Step 1: Write resolution monitor tests**

Create `tests/test_resolution_monitor.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from polybot.core.engine import Engine


def make_engine(**kwargs):
    defaults = dict(
        db=AsyncMock(), scanner=MagicMock(), researcher=MagicMock(),
        ensemble=MagicMock(), executor=MagicMock(), recorder=MagicMock(),
        risk_manager=MagicMock(),
        settings=MagicMock(
            health_check_interval=60, heartbeat_warn_seconds=600,
            heartbeat_critical_seconds=1800, balance_divergence_pct=0.05,
            strategy_kill_min_trades=50, daily_loss_limit_pct=0.15,
            fill_timeout_seconds=120, arb_fill_timeout_seconds=30,
            dry_run=False, post_breaker_cooldown_hours=24),
        email_notifier=AsyncMock(), position_manager=MagicMock(),
        clob=AsyncMock())
    defaults.update(kwargs)
    return Engine(**defaults)


@pytest.mark.asyncio
async def test_resolution_monitor_resolves_filled_trade():
    engine = make_engine()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 1, "market_id": 10, "side": "YES", "entry_price": 0.60,
         "shares": 10.0, "position_size_usd": 6.0, "status": "filled",
         "strategy": "forecast"},
    ])
    engine._db.fetchrow = AsyncMock(return_value={
        "polymarket_id": "mkt-1", "resolution_time": past})
    engine._scanner.fetch_market_resolution = AsyncMock(return_value=1)  # YES
    engine._recorder.record_resolution = AsyncMock()
    engine._clob.get_balance = AsyncMock(return_value=106.0)
    engine._db.execute = AsyncMock()

    await engine._resolution_monitor()

    engine._recorder.record_resolution.assert_called_once_with(1, 1)
    # Should have synced bankroll and decremented total_deployed
    calls = [str(c) for c in engine._db.execute.call_args_list]
    assert any("bankroll" in c for c in calls)
    assert any("total_deployed" in c for c in calls)


@pytest.mark.asyncio
async def test_resolution_monitor_dry_run_simulates_pnl():
    engine = make_engine()
    engine._settings.dry_run = True
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 2, "market_id": 10, "side": "YES", "entry_price": 0.60,
         "shares": 10.0, "position_size_usd": 6.0, "status": "dry_run",
         "strategy": "snipe"},
    ])
    engine._db.fetchrow = AsyncMock(return_value={
        "polymarket_id": "mkt-1", "resolution_time": past})
    engine._scanner.fetch_market_resolution = AsyncMock(return_value=1)  # YES
    engine._db.execute = AsyncMock()

    await engine._resolution_monitor()

    # Should have updated to dry_run_resolved with simulated P&L
    calls = [str(c) for c in engine._db.execute.call_args_list]
    assert any("dry_run_resolved" in c for c in calls)
    assert any("bankroll" in c for c in calls)


@pytest.mark.asyncio
async def test_resolution_monitor_skips_unresolved():
    engine = make_engine()
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    engine._db.fetch = AsyncMock(return_value=[
        {"id": 3, "market_id": 10, "side": "YES", "entry_price": 0.60,
         "shares": 10.0, "position_size_usd": 6.0, "status": "filled",
         "strategy": "forecast"},
    ])
    engine._db.fetchrow = AsyncMock(return_value={
        "polymarket_id": "mkt-1", "resolution_time": future})
    engine._db.execute = AsyncMock()

    await engine._resolution_monitor()

    # Should not have called execute (market not yet due)
    engine._db.execute.assert_not_called()
```

- [ ] **Step 2: Run tests, confirm FAIL**

Run: `uv run pytest tests/test_resolution_monitor.py -v`

- [ ] **Step 3: Add _resolution_monitor to engine.py**

Add this method to the `Engine` class:

```python
    async def _resolution_monitor(self):
        """Check if any filled/dry_run positions have resolved."""
        resolvable = await self._db.fetch(
            "SELECT * FROM trades WHERE status IN ('filled', 'dry_run')")

        now = datetime.now(timezone.utc)
        for trade in resolvable:
            market = await self._db.fetchrow(
                "SELECT * FROM markets WHERE id = $1", trade["market_id"])
            if not market or market["resolution_time"] > now:
                continue

            try:
                outcome = await self._scanner.fetch_market_resolution(
                    market["polymarket_id"])
            except Exception as e:
                log.error("resolution_check_failed", trade_id=trade["id"], error=str(e))
                continue

            if outcome is None:
                continue  # not yet resolved on-chain

            if trade["status"] == "filled":
                # Real resolution
                await self._recorder.record_resolution(trade["id"], outcome)
                await self._db.execute(
                    "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                    float(trade["position_size_usd"]))
                # Sync bankroll from wallet
                if self._clob:
                    try:
                        balance = await self._clob.get_balance()
                        await self._db.execute(
                            "UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                    except Exception as e:
                        log.error("bankroll_sync_failed", error=str(e))
                log.info("trade_resolved", trade_id=trade["id"], outcome=outcome)

            elif trade["status"] == "dry_run":
                # Simulated resolution
                entry = float(trade["entry_price"])
                shares = float(trade["shares"])
                if trade["side"] == "YES":
                    pnl = shares * (outcome - entry)
                else:
                    pnl = shares * ((1 - outcome) - (1 - entry))
                await self._db.execute(
                    """UPDATE trades SET status='dry_run_resolved', pnl=$1, exit_price=$2,
                       exit_reason='resolution', closed_at=$3 WHERE id=$4""",
                    pnl, float(outcome), now, trade["id"])
                await self._db.execute(
                    """UPDATE system_state SET
                       bankroll = bankroll + $1,
                       total_deployed = total_deployed - $2,
                       daily_pnl = daily_pnl + $1
                       WHERE id = 1""",
                    pnl, float(trade["position_size_usd"]))
                log.info("dry_run_resolved", trade_id=trade["id"],
                         outcome=outcome, simulated_pnl=pnl)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_resolution_monitor.py -v`

- [ ] **Step 5: Commit**

```bash
git add polybot/core/engine.py tests/test_resolution_monitor.py
git commit -m "feat: add resolution monitor — live and dry-run P&L resolution"
```

---

### Task 8: Startup Bankroll Sync + Daily PnL Reset

**Files:**
- Modify: `polybot/core/engine.py`

- [ ] **Step 1: Add bankroll sync to _reconcile_on_startup**

Update `_reconcile_on_startup` in `polybot/core/engine.py` to sync bankroll from CLOB at startup (live mode only):

After the existing reconciliation loop, add:

```python
        # Sync bankroll from wallet on startup (live mode only)
        if not self._settings.dry_run and self._clob:
            try:
                balance = await self._clob.get_balance()
                await self._db.execute(
                    "UPDATE system_state SET bankroll = $1 WHERE id = 1", balance)
                log.info("startup_bankroll_synced", balance=balance)
            except Exception as e:
                log.error("startup_bankroll_sync_failed", error=str(e))
```

- [ ] **Step 2: Add daily_pnl reset to _maybe_self_assess**

At the end of `_maybe_self_assess`, after sending the daily report email and before `self._last_self_assess = now`, add:

```python
        # Reset daily P&L for next day
        await self._db.execute("UPDATE system_state SET daily_pnl = 0 WHERE id = 1")
```

- [ ] **Step 3: Run existing engine tests**

Run: `uv run pytest tests/test_engine.py tests/test_fill_monitor.py tests/test_resolution_monitor.py -v`

- [ ] **Step 4: Commit**

```bash
git add polybot/core/engine.py
git commit -m "feat: startup bankroll sync from CLOB, daily P&L reset at midnight"
```

---

### Task 9: Dashboard Dry-Run Labels

**Files:**
- Modify: `polybot/dashboard/app.py`

- [ ] **Step 1: Update /trades endpoint to show dry_run status**

In the `/trades` endpoint in `polybot/dashboard/app.py`, the existing code already returns `"status": t["status"]` — so `dry_run` and `dry_run_resolved` will naturally appear. No code change needed, but verify by adding a label field:

In the trade dict comprehension, add:

```python
"dry_run": t["status"] in ("dry_run", "dry_run_resolved"),
```

- [ ] **Step 2: Commit**

```bash
git add polybot/dashboard/app.py
git commit -m "feat: add dry_run label to dashboard trades endpoint"
```

---

### Task 10: Entry Point Wiring

**Files:**
- Modify: `polybot/__main__.py`

- [ ] **Step 1: Wire ClobGateway into __main__.py**

Update `polybot/__main__.py`:

Add import at top:
```python
from polybot.trading.clob import ClobGateway
```

After `wallet = WalletManager(...)` and before `executor = OrderExecutor(...)`, add:

```python
    # CLOB gateway (None if dry-run without credentials)
    clob = None
    if settings.polymarket_api_secret and settings.polymarket_api_passphrase:
        clob = ClobGateway(
            host="https://clob.polymarket.com",
            chain_id=settings.polymarket_chain_id,
            private_key=settings.polymarket_private_key,
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase,
        )

    if not settings.dry_run and clob is None:
        log.error("live_mode_requires_clob_credentials")
        return
```

Update `executor` construction to pass `clob` and `dry_run`:
```python
    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=settings.fill_timeout_seconds,
        clob=clob, dry_run=settings.dry_run)
```

Update `email_notifier` to pass `dry_run`:
```python
    email_notifier = EmailNotifier(
        api_key=settings.resend_api_key, to_email=settings.alert_email,
        dry_run=settings.dry_run)
```

Update `Engine` construction to pass `clob`:
```python
    engine = Engine(
        db=db, scanner=scanner, researcher=researcher, ensemble=ensemble,
        executor=executor, recorder=recorder, risk_manager=risk_manager,
        settings=settings, email_notifier=email_notifier,
        position_manager=position_manager, clob=clob)
```

Add startup log for mode:
```python
    log.info("polybot_mode", dry_run=settings.dry_run,
             clob_connected=clob is not None)
```

- [ ] **Step 2: Verify imports**

Run: `uv run python -c "from polybot.__main__ import main; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add polybot/__main__.py
git commit -m "feat: wire ClobGateway into entry point with dry-run guard"
```

---

### Task 11: Credential Derivation Script

**Files:**
- Create: `scripts/derive_creds.py`

- [ ] **Step 1: Create the script**

Create `scripts/derive_creds.py`:

```python
#!/usr/bin/env python3
"""Derive Polymarket CLOB API credentials from your private key.

Usage:
    uv run python scripts/derive_creds.py

Reads POLYMARKET_PRIVATE_KEY from .env and prints credentials to add to .env.
"""
from dotenv import load_dotenv
import os
import sys

load_dotenv()

private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
if not private_key:
    print("ERROR: POLYMARKET_PRIVATE_KEY not found in .env")
    sys.exit(1)

from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=private_key,
)

print("Deriving CLOB API credentials...")
creds = client.create_or_derive_api_creds()
if not creds:
    print("ERROR: Failed to derive credentials")
    sys.exit(1)

print(f"\nSuccess! Add these to your .env file:\n")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
print(f"\nThen set DRY_RUN=false when you're ready for live trading.")
```

- [ ] **Step 2: Commit**

```bash
git add scripts/derive_creds.py
git commit -m "feat: add CLOB credential derivation script"
```

---

### Task 12: Full Test Suite + README Update

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short 2>&1 | tail -40`

Expected: All PASS. Fix any failures from the engine constructor change (`clob=None` param) in existing test files.

- [ ] **Step 2: Fix broken tests**

Common fixes:
- Any test constructing `Engine()` needs `clob=None` param
- Any test constructing `OrderExecutor()` needs `clob=None, dry_run=False`
- Any test constructing `EmailNotifier()` needs `dry_run=False`
- `tests/test_engine.py` `make_engine()` needs `clob=AsyncMock()`

- [ ] **Step 3: Update README**

Add a "Going Live" section to `README.md` after the "Running locally" section:

```markdown
### Going live

1. Derive your CLOB credentials:

```bash
uv run python scripts/derive_creds.py
# Copy the output into your .env file
```

2. Run in observation mode first (default):

```bash
DRY_RUN=true uv run python -m polybot
# Monitor daily report emails for 24-48h
```

3. When ready for live trading with real money:

```bash
DRY_RUN=false STARTING_BANKROLL=20 uv run python -m polybot
```
```

- [ ] **Step 4: Commit and push**

```bash
git add -A
git commit -m "fix: update tests for go-live API changes, add going-live docs to README"
git push origin main
```

- [ ] **Step 5: Final verification**

Run: `uv run pytest tests/ -v`

Expected: All PASS, zero failures.

---

### Task 13: Fix Gap — total_deployed Tracking

Self-review found that `total_deployed` is never incremented on trade placement and never decremented during startup reconciliation.

**Files:**
- Modify: `polybot/trading/executor.py`
- Modify: `polybot/core/engine.py`

- [ ] **Step 1: Add total_deployed increment to executor.place_order()**

In `polybot/trading/executor.py`, after the successful DB insert (the `trade_id = await self._db.fetchval(...)` line) and before the CLOB submission block, add:

```python
        # Lock deployed capital
        await self._db.execute(
            "UPDATE system_state SET total_deployed = total_deployed + $1 WHERE id = 1",
            size_usd)
```

This runs in both live and dry-run modes — capital is "locked" regardless.

- [ ] **Step 2: Add total_deployed decrement to startup reconciliation**

In `polybot/core/engine.py`, inside `_reconcile_on_startup()`, after `await self._recorder.record_resolution(trade["id"], resolved)`, add:

```python
                        await self._db.execute(
                            "UPDATE system_state SET total_deployed = total_deployed - $1 WHERE id = 1",
                            float(trade["position_size_usd"]))
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/ -v --tb=short`

- [ ] **Step 4: Commit**

```bash
git add polybot/trading/executor.py polybot/core/engine.py
git commit -m "fix: track total_deployed on trade placement and startup reconciliation"
```

---

### Task 14: Fix Gap — Fill Email + daily_pnl on Live Resolution

Self-review found: (1) fill monitor doesn't send email on fill, (2) live resolution path doesn't update daily_pnl.

**Files:**
- Modify: `polybot/core/engine.py`

- [ ] **Step 1: Add email notification to fill monitor matched branch**

In `_fill_monitor()`, after the `log.info("order_filled", ...)` line, add:

```python
                await self._context.email_notifier.send(
                    f"Trade filled: order {trade['clob_order_id']}",
                    f"<p>Trade #{trade['id']} filled. Strategy: {trade['strategy']}</p>")
```

- [ ] **Step 2: Add daily_pnl update to live resolution path**

In `_resolution_monitor()`, in the `if trade["status"] == "filled":` branch, after `recorder.record_resolution()`, add:

```python
                # Update daily P&L (record_resolution computes pnl in the trade row)
                resolved_trade = await self._db.fetchrow(
                    "SELECT pnl FROM trades WHERE id = $1", trade["id"])
                if resolved_trade and resolved_trade["pnl"] is not None:
                    await self._db.execute(
                        "UPDATE system_state SET daily_pnl = daily_pnl + $1 WHERE id = 1",
                        float(resolved_trade["pnl"]))
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_fill_monitor.py tests/test_resolution_monitor.py -v`

- [ ] **Step 4: Commit**

```bash
git add polybot/core/engine.py
git commit -m "fix: fill email notification, daily_pnl tracking on live resolution"
```

---

### Task 15: Final Verification + Push

- [ ] **Step 1: Full test suite**

Run: `uv run pytest tests/ -v`

Expected: All PASS.

- [ ] **Step 2: Import smoke test**

Run: `uv run python -c "from polybot.__main__ import main; print('All imports OK')"`

- [ ] **Step 3: Commit and push**

```bash
git add -A
git commit -m "chore: final go-live verification"
git push origin main
```
