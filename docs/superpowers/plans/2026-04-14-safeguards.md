# Safeguards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect capital with total drawdown halt, capital divergence monitor, live preflight tests, and graduated deployment stages — preventing the catastrophic $623 loss from ever recurring.

**Architecture:** Add `high_water_bankroll` and `drawdown_halt_until` columns to `system_state`. Engine checks drawdown before running strategies. A new periodic task monitors CLOB balance vs DB. A preflight script validates all CLOB APIs before live mode starts. A `live_deployment_stage` config gates deployment progression.

**Tech Stack:** Python 3.13, PostgreSQL, pytest, py-clob-client

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/core/config.py` | Modify | Add `max_total_drawdown_pct`, `max_capital_divergence_pct`, `live_deployment_stage` |
| `polybot/core/engine.py` | Modify | Add drawdown check in strategy loop, capital divergence monitor |
| `scripts/live_preflight.py` | Create | Pre-live validation of all CLOB API interactions |
| `polybot/__main__.py` | Modify | Run preflight on startup when `dry_run=false` |
| `tests/test_safeguards.py` | Create | Tests for drawdown halt and capital divergence |

---

### Task 1: Database Schema — Add Drawdown Columns

**Files:**
- Modify: PostgreSQL `system_state` table (via psql)

- [ ] **Step 1: Add columns**

```sql
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS high_water_bankroll NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS drawdown_halt_until TIMESTAMPTZ;
-- Initialize high_water to current bankroll
UPDATE system_state SET high_water_bankroll = bankroll WHERE id = 1;
```

Run: `cd ~/polybot && /opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -d polybot -f -` with the above SQL.

- [ ] **Step 2: Verify**

```sql
SELECT bankroll, high_water_bankroll, drawdown_halt_until FROM system_state WHERE id = 1;
```

Expected: `high_water_bankroll` matches `bankroll`, `drawdown_halt_until` is NULL.

- [ ] **Step 3: Commit** (no code files — just note the migration)

---

### Task 2: Config Keys

**Files:**
- Modify: `polybot/core/config.py`

- [ ] **Step 1: Add config keys**

After the `circuit_breaker_hours` line (around line 75), add:

```python
    post_breaker_cooldown_hours: int = 24
    post_breaker_kelly_reduction: float = 0.50

    # Total drawdown protection
    max_total_drawdown_pct: float = 0.30     # halt all trading at 30% total loss from high-water
    max_capital_divergence_pct: float = 0.10  # halt if CLOB vs DB diverges > 10%
    live_deployment_stage: str = "dry_run"    # dry_run → micro_test → full
```

- [ ] **Step 2: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
Expected: All pass. Fix config test assertions if needed.

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py
git commit -m "config: add drawdown halt, capital divergence, deployment stage keys"
```

---

### Task 3: Drawdown Halt in Engine

**Files:**
- Modify: `polybot/core/engine.py:60-91` (strategy loop)
- Create: `tests/test_safeguards.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_safeguards.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_drawdown_halt_stops_strategy():
    """Strategy should not run when drawdown halt is active."""
    from polybot.core.engine import TradingEngine

    db = AsyncMock()
    # drawdown_halt_until is in the future = halted
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 300, "high_water_bankroll": 500,
        "drawdown_halt_until": datetime.now(timezone.utc) + timedelta(hours=24),
        "total_deployed": 0, "daily_pnl": 0, "circuit_breaker_until": None,
        "post_breaker_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = TradingEngine.__new__(TradingEngine)
    engine._db = db
    engine._settings = settings
    engine._context = MagicMock()

    result = await engine._check_drawdown_halt()
    assert result is True  # halted


@pytest.mark.asyncio
async def test_drawdown_triggers_when_below_threshold():
    """Should trigger halt when bankroll drops 30%+ below high-water."""
    from polybot.core.engine import TradingEngine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 300, "high_water_bankroll": 500,
        "drawdown_halt_until": None,
        "total_deployed": 0, "daily_pnl": 0, "circuit_breaker_until": None,
        "post_breaker_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = TradingEngine.__new__(TradingEngine)
    engine._db = db
    engine._settings = settings
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()

    result = await engine._check_drawdown_halt()
    assert result is True  # 300/500 = 40% drawdown > 30% threshold
    # Should have set drawdown_halt_until in DB
    db.execute.assert_called()


@pytest.mark.asyncio
async def test_no_halt_when_within_threshold():
    """Should not trigger halt when drawdown is within limits."""
    from polybot.core.engine import TradingEngine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 400, "high_water_bankroll": 500,
        "drawdown_halt_until": None,
        "total_deployed": 0, "daily_pnl": 0, "circuit_breaker_until": None,
        "post_breaker_until": None,
    })

    settings = MagicMock()
    settings.dry_run = False
    settings.max_total_drawdown_pct = 0.30

    engine = TradingEngine.__new__(TradingEngine)
    engine._db = db
    engine._settings = settings

    result = await engine._check_drawdown_halt()
    assert result is False  # 400/500 = 20% drawdown < 30% threshold
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run python -m pytest tests/test_safeguards.py -v`
Expected: FAIL — `_check_drawdown_halt` doesn't exist.

- [ ] **Step 3: Implement drawdown check in engine**

In `polybot/core/engine.py`, add this method to `TradingEngine`:

```python
    async def _check_drawdown_halt(self) -> bool:
        """Check if total drawdown halt is active or should be triggered.
        Returns True if trading should be halted."""
        state = await self._db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return False

        bankroll = float(state["bankroll"])
        high_water = float(state.get("high_water_bankroll", bankroll) or bankroll)
        halt_until = state.get("drawdown_halt_until")

        # Already halted?
        if halt_until and halt_until > datetime.now(timezone.utc):
            return True

        # Update high-water mark
        if bankroll > high_water:
            await self._db.execute(
                "UPDATE system_state SET high_water_bankroll = $1 WHERE id = 1", bankroll)
            return False

        # Check drawdown
        if high_water > 0:
            drawdown = 1.0 - (bankroll / high_water)
            max_drawdown = getattr(self._settings, 'max_total_drawdown_pct', 0.30)
            if drawdown >= max_drawdown:
                halt_time = datetime.now(timezone.utc) + timedelta(days=365)
                await self._db.execute(
                    "UPDATE system_state SET drawdown_halt_until = $1 WHERE id = 1",
                    halt_time)
                log.critical("DRAWDOWN_HALT", bankroll=bankroll, high_water=high_water,
                             drawdown_pct=round(drawdown * 100, 1))
                try:
                    await self._context.email_notifier.send(
                        "[POLYBOT CRITICAL] DRAWDOWN HALT — ALL TRADING STOPPED",
                        f"<p>Bankroll ${bankroll:.2f} is {drawdown*100:.1f}% below "
                        f"high-water ${high_water:.2f}. Threshold: {max_drawdown*100:.0f}%.</p>"
                        f"<p>All trading halted. Manual DB reset required to resume.</p>")
                except Exception:
                    pass
                return True

        return False
```

Then add the drawdown check to `_run_strategy` at line 65, right before `await strategy.run_once(...)`:

```python
            try:
                # Check drawdown halt before every strategy execution
                if await self._check_drawdown_halt():
                    await asyncio.sleep(60)
                    continue
                await strategy.run_once(self._context)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/test_safeguards.py tests/ --tb=short -q`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/core/engine.py tests/test_safeguards.py
git commit -m "feat: total drawdown halt — stops all trading at 30% loss from high-water

Checks bankroll vs high_water_bankroll before every strategy run.
If drawdown >= 30%, halts all trading and sends email alert.
Halt persists across restarts — manual DB reset required to resume.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Capital Divergence Monitor

**Files:**
- Modify: `polybot/core/engine.py` (add periodic task)
- Modify: `tests/test_safeguards.py` (add test)

- [ ] **Step 1: Write failing test**

Append to `tests/test_safeguards.py`:

```python
@pytest.mark.asyncio
async def test_capital_divergence_triggers_halt():
    """Should halt when CLOB balance diverges > 10% from DB bankroll."""
    from polybot.core.engine import TradingEngine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 500, "total_deployed": 0,
    })

    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=100.0)  # $100 vs $500 expected = 80% divergence

    settings = MagicMock()
    settings.max_capital_divergence_pct = 0.10

    engine = TradingEngine.__new__(TradingEngine)
    engine._db = db
    engine._clob = clob
    engine._settings = settings
    engine._context = MagicMock()
    engine._context.email_notifier = AsyncMock()
    engine._capital_divergence_halted = False

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is True


@pytest.mark.asyncio
async def test_capital_divergence_ok_when_close():
    """Should not halt when CLOB balance is close to DB bankroll."""
    from polybot.core.engine import TradingEngine

    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 500, "total_deployed": 50,
    })

    clob = AsyncMock()
    clob.get_balance = AsyncMock(return_value=445.0)  # $445 vs $450 expected = 1% divergence

    settings = MagicMock()
    settings.max_capital_divergence_pct = 0.10

    engine = TradingEngine.__new__(TradingEngine)
    engine._db = db
    engine._clob = clob
    engine._settings = settings
    engine._capital_divergence_halted = False

    await engine._check_capital_divergence()
    assert engine._capital_divergence_halted is False
```

- [ ] **Step 2: Implement capital divergence monitor**

Add to `TradingEngine`:

```python
    async def _check_capital_divergence(self):
        """Compare CLOB balance vs DB bankroll. Halt if divergence > threshold."""
        if not self._clob or self._settings.dry_run:
            return
        try:
            state = await self._db.fetchrow("SELECT bankroll, total_deployed FROM system_state WHERE id = 1")
            clob_balance = await self._clob.get_balance()
            expected_cash = float(state["bankroll"]) - float(state["total_deployed"])
            if expected_cash <= 0:
                return
            divergence = abs(clob_balance - expected_cash) / expected_cash
            max_div = getattr(self._settings, 'max_capital_divergence_pct', 0.10)
            if divergence > max_div:
                self._capital_divergence_halted = True
                log.critical("CAPITAL_DIVERGENCE_HALT", clob=clob_balance,
                             expected=expected_cash, divergence_pct=round(divergence * 100, 1))
                await self._context.email_notifier.send(
                    "[POLYBOT CRITICAL] Capital divergence halt",
                    f"<p>CLOB: ${clob_balance:.2f}, Expected: ${expected_cash:.2f}, "
                    f"Divergence: {divergence*100:.1f}%</p>")
        except Exception as e:
            log.error("capital_divergence_check_error", error=str(e))
```

Initialize `self._capital_divergence_halted = False` in `__init__`.

Add to the drawdown check in `_run_strategy`:

```python
                if await self._check_drawdown_halt():
                    await asyncio.sleep(60)
                    continue
                if self._capital_divergence_halted:
                    await asyncio.sleep(60)
                    continue
```

Register the periodic task in `run_forever` (after the fill_monitor):

```python
        if not self._settings.dry_run and self._clob:
            tasks.append(self._run_periodic(self._fill_monitor, 30))
            tasks.append(self._run_periodic(self._check_capital_divergence, 60))
```

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/test_safeguards.py tests/ --tb=short -q`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add polybot/core/engine.py tests/test_safeguards.py
git commit -m "feat: capital divergence monitor — halts if CLOB vs DB diverges > 10%

Polls CLOB balance every 60s, compares to DB expected cash.
Prevents untracked capital consumption (the MM $419 scenario).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Live Preflight Script

**Files:**
- Create: `scripts/live_preflight.py`

- [ ] **Step 1: Create preflight script**

```python
#!/usr/bin/env python3
"""Pre-live validation of all CLOB API interactions.

Runs automatically before live trading starts. Tests every API
endpoint that caused bugs during our first live deployment.

Usage:
    cd ~/polybot && uv run python scripts/live_preflight.py
"""
import os
import sys
import asyncio
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()

CHECKS_PASSED = 0
CHECKS_FAILED = 0


def check(name: str, passed: bool, detail: str = ""):
    global CHECKS_PASSED, CHECKS_FAILED
    if passed:
        CHECKS_PASSED += 1
        print(f"  ✓ {name}")
    else:
        CHECKS_FAILED += 1
        print(f"  ✗ {name}: {detail}")


def main():
    global CHECKS_PASSED, CHECKS_FAILED
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    print("=== Polybot Live Preflight ===\n")

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        print("FAIL: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    client = ClobClient(
        host="https://clob.polymarket.com", chain_id=137, key=pk,
        creds=ApiCreds(
            api_key=os.environ.get("POLYMARKET_API_KEY", ""),
            api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
            api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", "")))

    # 1. Balance check
    print("1. Balance API")
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = client.get_balance_allowance(params)
        balance = int(result.get("balance", 0)) / 1e6
        check("get_balance_allowance returns dollars", 0 <= balance < 1_000_000,
              f"got {balance}")
        check("balance > $0", balance > 0, f"balance is ${balance:.2f}")
    except Exception as e:
        check("get_balance_allowance", False, str(e))

    # 2. Heartbeat chain
    print("\n2. Heartbeat API")
    try:
        # First heartbeat — extract server ID from error
        hb_id = str(uuid4())
        try:
            result = client.post_heartbeat(hb_id)
            hb_id = result.get("heartbeat_id", hb_id) if isinstance(result, dict) else result
        except Exception as e:
            import re
            match = re.search(r"'heartbeat_id': '([^']+)'", str(e))
            if match:
                hb_id = match.group(1)
                result = client.post_heartbeat(hb_id)
                hb_id = result.get("heartbeat_id", hb_id) if isinstance(result, dict) else result
            else:
                raise

        check("heartbeat #1", True)

        # Second and third
        for i in [2, 3]:
            result = client.post_heartbeat(hb_id)
            hb_id = result.get("heartbeat_id", hb_id) if isinstance(result, dict) else result
            check(f"heartbeat #{i}", True)
    except Exception as e:
        check("heartbeat chain", False, str(e))

    # 3. Order book access
    print("\n3. Order Book API")
    try:
        markets = client.get_sampling_simplified_markets(next_cursor="")
        if markets and len(markets) > 0:
            first = markets[0] if isinstance(markets, list) else markets.get("data", [{}])[0]
            check("get_markets", True)
        else:
            check("get_markets", True, "empty but no error")
    except Exception as e:
        check("get_markets", False, str(e))

    # 4. Conditional token approval
    print("\n4. Token Approvals")
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        address = Account.from_key(pk).address

        CT = w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        ct_abi = [{"constant": True, "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"}],
            "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
            "type": "function"}]
        ct = w3.eth.contract(address=CT, abi=ct_abi)

        for name, addr in [
            ("Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
            ("NegRisk", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
            ("NegRiskAdapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
        ]:
            approved = ct.functions.isApprovedForAll(
                address, w3.to_checksum_address(addr)).call()
            check(f"CT approval: {name}", approved, "not approved")
    except Exception as e:
        check("token approvals", False, str(e))

    # 5. Deployment stage check
    print("\n5. Deployment Stage")
    from polybot.core.config import Settings
    settings = Settings()
    stage = getattr(settings, "live_deployment_stage", "dry_run")
    check(f"deployment stage is '{stage}'",
          stage in ("micro_test", "full"),
          f"stage is '{stage}' — must be 'micro_test' or 'full' for live trading")

    # Summary
    print(f"\n=== Results: {CHECKS_PASSED} passed, {CHECKS_FAILED} failed ===")
    if CHECKS_FAILED > 0:
        print("\nFAIL: Fix the above issues before enabling live trading.")
        sys.exit(1)
    else:
        print("\nPASS: All preflight checks passed. Safe to trade live.")
        sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
cd ~/polybot && git add scripts/live_preflight.py
git commit -m "feat: live preflight script — validates all CLOB APIs before trading

Tests balance, heartbeat chain, order book access, token approvals,
and deployment stage. Refuses to pass unless all checks succeed.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Wire Preflight Into Startup

**Files:**
- Modify: `polybot/__main__.py`

- [ ] **Step 1: Add preflight check on startup**

In `polybot/__main__.py`, after the CLOB gateway initialization and before the engine is created (around line 105-107), add:

```python
    if not settings.dry_run and clob is not None:
        # Run preflight checks before starting live trading
        log.info("running_live_preflight")
        from scripts.live_preflight import main as run_preflight
        try:
            run_preflight()
        except SystemExit as e:
            if e.code != 0:
                log.critical("PREFLIGHT_FAILED — refusing to start live trading")
                return
```

Note: if the import path doesn't work, adjust to:
```python
        import subprocess
        result = subprocess.run(
            ["uv", "run", "python", "scripts/live_preflight.py"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
        if result.returncode != 0:
            log.critical("PREFLIGHT_FAILED", output=result.stdout[-500:])
            return
        log.info("preflight_passed")
```

- [ ] **Step 2: Run full tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
cd ~/polybot && git add polybot/__main__.py
git commit -m "feat: run preflight checks on startup when live mode enabled

Bot refuses to start strategies if any CLOB API check fails.
Prevents deploying with broken API integrations.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
