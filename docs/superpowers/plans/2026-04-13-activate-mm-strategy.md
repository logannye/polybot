# Activate Market Making Strategy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the existing market making strategy as the primary revenue engine, disable unprofitable MR, and tune config for a $464 bankroll.

**Architecture:** All config changes — the MM code, quote manager, inventory tracker, and engine wiring are already built and passing 20 tests. Enable MM, disable MR, tune MM parameters for our bankroll and the current Polymarket liquidity landscape.

**Tech Stack:** Python config only

---

### Task 1: Config Changes

**Files:**
- Modify: `polybot/core/config.py` (MM and MR defaults)
- Modify: `~/polybot/.env` (override keys)
- Modify: `tests/test_config.py` (if any assertions break)

- [ ] **Step 1: Update config defaults**

In `polybot/core/config.py`:

1. `mm_enabled: bool = True` (line 172, was False)
2. `mm_min_volume_24h: float = 2000.0` (line 187, was 5000 — more markets qualify)
3. `mm_min_book_depth: float = 500.0` (line 191, was 1000 — more markets qualify)
4. `mr_enabled: bool = False` (line 194, was False already but confirm)

Keep all other MM defaults as-is — they're conservative and appropriate:
- `mm_quote_size_usd = 10.0` ($10 per side — safe for $464 bankroll)
- `mm_max_inventory_per_market = 50.0` (max $50 exposed per market)
- `mm_max_total_inventory = 200.0` (max ~43% of bankroll)
- `mm_max_markets = 8` (up to 8 markets simultaneously)
- `mm_base_spread_bps = 150` (1.5% spread — competitive but profitable)
- `mm_cycle_seconds = 5.0` (requote every 5 seconds)

- [ ] **Step 2: Update .env**

```
MM_ENABLED=true
MM_MIN_VOLUME_24H=2000
MM_MIN_BOOK_DEPTH=500
```

Also ensure MR is disabled in .env:
```
MR_ENABLED=false
```

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run python -m pytest tests/ --tb=short -q`
Expected: All pass. Fix any config assertion failures.

- [ ] **Step 4: Verify config loads**

Run: `cd ~/polybot && uv run python -c "from polybot.core.config import Settings; s = Settings(); print(f'mm_enabled={s.mm_enabled}, mm_min_volume={s.mm_min_volume_24h}, mm_min_depth={s.mm_min_book_depth}, mr_enabled={s.mr_enabled}')"`

Expected: `mm_enabled=True, mm_min_volume=2000.0, mm_min_depth=500.0, mr_enabled=False`

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/core/config.py tests/test_config.py
git commit -m "config: activate market making, disable MR

MM is the natural strategy for Polymarket's order book structure —
earn spread by providing liquidity instead of demanding it.
MR disabled — 89% timeout rate on live orders due to thin books.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Update .env, Push, and Restart

- [ ] **Step 1: Update .env file**

Add/update these keys:
```
MM_ENABLED=true
MM_MIN_VOLUME_24H=2000
MM_MIN_BOOK_DEPTH=500
MR_ENABLED=false
```

- [ ] **Step 2: Push to GitHub**

```bash
cd ~/polybot && git push origin main
```

- [ ] **Step 3: Restart polybot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 4: Verify MM is active**

Check logs for:
- `engine_starting` with `market_maker` in strategies list
- `mm_markets_selected` showing which markets are being quoted
- `mm_requoted` showing bid/ask quotes being placed
- `clob_order_submitted` with `post_only: true` (MM always uses maker orders)

```bash
grep "mm_markets_selected\|mm_requoted\|engine_starting" ~/polybot/data/polybot_stdout.log | tail -10
```
