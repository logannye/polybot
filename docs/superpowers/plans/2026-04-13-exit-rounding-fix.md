# Exit Rounding Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the rounding error that prevents the bot from exiting live positions, and add a retry mechanism so exit failures never block the position manager permanently.

**Architecture:** Two defenses: (1) Apply a 0.1% haircut to shares when selling to guarantee we never exceed the on-chain balance. (2) If a sell still fails with a balance error, retry once with 1% reduction. If both fail, force-close the position in the DB at breakeven to free capital — a stuck position blocking all trading for 16+ hours is worse than losing dust.

**Tech Stack:** Python 3.13, pytest

---

### File Map

| File | Action | Responsibility |
|---|---|---|
| `polybot/trading/executor.py:128-142` | Modify | Haircut shares on sell, retry on balance error, force-close fallback |
| `tests/test_executor.py` | Modify | Test sell haircut and retry logic |

---

### Task 1: Fix exit_position sell with haircut + retry + force-close

**Files:**
- Modify: `polybot/trading/executor.py:128-142`

- [ ] **Step 1: Read the current exit_position method**

Read `polybot/trading/executor.py` from line 103 to 180. The sell logic is at lines 128-142:

```python
        if trade["status"] == "filled" and not self._dry_run and self._clob:
            market = await self._db.fetchrow(
                "SELECT polymarket_id FROM markets WHERE id = $1", trade["market_id"])
            if market:
                market_data = self._scanner.get_cached_price(market["polymarket_id"])
                if market_data:
                    token_id = market_data.get("yes_token_id") if side == "YES" else market_data.get("no_token_id")
                    if token_id:
                        try:
                            await self._clob.sell_shares(
                                token_id=token_id, price=exit_price, size=shares)
                        except Exception as e:
                            log.error("exit_sell_failed", trade_id=trade_id, error=str(e))
                            return None
```

- [ ] **Step 2: Replace with haircut + retry + force-close**

Replace lines 128-142 with:

```python
        if trade["status"] == "filled" and not self._dry_run and self._clob:
            market = await self._db.fetchrow(
                "SELECT polymarket_id FROM markets WHERE id = $1", trade["market_id"])
            if market:
                market_data = self._scanner.get_cached_price(market["polymarket_id"])
                if market_data:
                    token_id = market_data.get("yes_token_id") if side == "YES" else market_data.get("no_token_id")
                    if token_id:
                        # Haircut: sell 99.9% of shares to avoid exceeding on-chain balance.
                        # The CLOB fills can settle with slightly fewer shares than computed
                        # due to rounding in on-chain token transfers.
                        sell_size = round(shares * 0.999, 6)
                        sold = False
                        for attempt, size_mult in enumerate([1.0, 0.99], start=1):
                            try:
                                await self._clob.sell_shares(
                                    token_id=token_id, price=exit_price,
                                    size=round(sell_size * size_mult, 6))
                                sold = True
                                break
                            except Exception as e:
                                err = str(e)
                                log.warning("exit_sell_attempt_failed", trade_id=trade_id,
                                            attempt=attempt, size=round(sell_size * size_mult, 6),
                                            error=err)
                                if "not enough balance" not in err.lower():
                                    break  # Non-balance error, don't retry
                        if not sold:
                            # Force-close in DB to free capital. A stuck position blocking
                            # all trading for hours is worse than losing dust.
                            log.error("exit_sell_force_close", trade_id=trade_id,
                                      shares=shares, exit_price=exit_price)
                            pnl = 0.0  # Assume breakeven — actual shares still on-chain
```

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
All tests must pass.

- [ ] **Step 4: Commit**

```bash
cd ~/polybot && git add polybot/trading/executor.py
git commit -m "fix: exit sell haircut + retry + force-close to prevent stuck positions

Apply 0.1% haircut to shares when selling (CLOB fills settle with
slightly fewer shares than computed). If sell still fails with balance
error, retry at 99% size. If both fail, force-close in DB to free
capital — a stuck position blocking all trading for 16h is catastrophic.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Manually unstick trade 808

**Files:** None (DB operation only)

- [ ] **Step 1: Force-close trade 808 in the DB**

The McIlroy Masters market is likely near resolution anyway. Force-close at entry price (breakeven):

```sql
UPDATE trades SET status = 'closed', exit_price = 0.48, exit_reason = 'force_close',
  pnl = 0, closed_at = NOW()
WHERE id = 808;
UPDATE system_state SET total_deployed = total_deployed - 16.01 WHERE id = 1;
```

- [ ] **Step 2: Restart the bot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 3: Verify the position manager is no longer stuck**

Check logs for `exit_sell_failed` — should stop appearing.
Check that MR is finding and filling new trades.
