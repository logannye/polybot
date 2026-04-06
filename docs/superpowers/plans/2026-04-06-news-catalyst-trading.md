# News Catalyst Speed Trading — Plan C

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a strategy that detects sudden price moves on Polymarket (>5% in <5 min), uses LLM to interpret whether the move is justified, and trades the gap if the market hasn't fully adjusted — capturing the 30s-5min news reaction window.

**Architecture:** A `NewsCatalystStrategy` monitors all markets for large price moves via the existing `PriceStreamHub` WebSocket or by comparing consecutive scanner snapshots. When a move is detected: (1) fetch news context via Brave API, (2) LLM quick_screen estimates new fair probability, (3) if market hasn't fully adjusted (>5% gap remaining), trade aggressively with a tight 15-30min time-stop. This is the highest per-trade return strategy (10-30% on right calls) with asymmetric risk (exit quickly on wrong calls for 1-3% loss).

**Tech Stack:** Python 3.13, asyncpg, aiohttp, structlog, pytest

**Research findings:**
- Polymarket prices take 30s-5min to fully adjust after major news
- LLM ensemble can estimate new probabilities within ~3-5s
- Example from research: Trump witness recantation — bot bought at $0.29, market repriced to $0.42 in 8 min (45% return)
- The `PriceStreamHub` WebSocket already exists and provides real-time price callbacks
- `BraveResearcher.search()` takes ~500ms with freshness="past day" filter
- `EnsembleAnalyzer.quick_screen()` takes ~1s (Gemini Flash only)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `polybot/analysis/move_detector.py` | **Create**: Detects large price moves by comparing scanner snapshots |
| `polybot/strategies/news_catalyst.py` | **Create**: NewsCatalystStrategy — reacts to price moves with LLM-informed trades |
| `polybot/core/config.py` | **Modify**: Add news catalyst config keys |
| `polybot/__main__.py` | **Modify**: Wire up NewsCatalystStrategy |
| `tests/test_move_detector.py` | **Create**: Tests for move detection |
| `tests/test_news_catalyst.py` | **Create**: Tests for news catalyst strategy |

---

### Task 1: Price move detector

Detects markets where price has moved >N% since the last scanner snapshot. Simpler and more reliable than WebSocket-based detection (which requires maintaining subscription state for 4,759 markets).

**Files:**
- Create: `polybot/analysis/move_detector.py`
- Create: `tests/test_move_detector.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_move_detector.py`:

```python
"""Tests for price move detection."""
import pytest
from polybot.analysis.move_detector import MoveDetector


def test_detects_large_upward_move():
    detector = MoveDetector(threshold=0.05)
    detector.update("0xabc", 0.50)
    moves = detector.update("0xabc", 0.58)
    assert len(moves) == 1
    assert moves[0]["direction"] == "up"
    assert abs(moves[0]["magnitude"] - 0.08) < 0.01


def test_detects_large_downward_move():
    detector = MoveDetector(threshold=0.05)
    detector.update("0xabc", 0.60)
    moves = detector.update("0xabc", 0.52)
    assert len(moves) == 1
    assert moves[0]["direction"] == "down"


def test_ignores_small_moves():
    detector = MoveDetector(threshold=0.05)
    detector.update("0xabc", 0.50)
    moves = detector.update("0xabc", 0.53)
    assert len(moves) == 0


def test_first_update_no_moves():
    detector = MoveDetector(threshold=0.05)
    moves = detector.update("0xabc", 0.50)
    assert len(moves) == 0


def test_cooldown_prevents_repeated_triggers():
    detector = MoveDetector(threshold=0.05, cooldown_seconds=300)
    detector.update("0xabc", 0.50)
    moves1 = detector.update("0xabc", 0.58)
    assert len(moves1) == 1
    # Immediate re-trigger should be suppressed
    moves2 = detector.update("0xabc", 0.65)
    assert len(moves2) == 0


def test_batch_update_returns_all_movers():
    detector = MoveDetector(threshold=0.05)
    detector.update("0x1", 0.50)
    detector.update("0x2", 0.70)
    detector.update("0x3", 0.30)
    all_moves = detector.batch_update({
        "0x1": 0.58,  # +0.08 — triggers
        "0x2": 0.72,  # +0.02 — too small
        "0x3": 0.20,  # -0.10 — triggers
    })
    assert len(all_moves) == 2
```

- [ ] **Step 2: Implement MoveDetector**

Create `polybot/analysis/move_detector.py`:

```python
"""Detects large price moves between scanner snapshots.

Compares current prices against the last-seen price for each market.
When a move exceeds the threshold, emits a move event with direction
and magnitude. Per-market cooldown prevents repeated triggers on
the same sustained move.
"""

import time
import structlog

log = structlog.get_logger()


class MoveDetector:
    """Tracks price snapshots and detects large moves."""

    def __init__(self, threshold: float = 0.05, cooldown_seconds: float = 300.0):
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._last_prices: dict[str, float] = {}
        self._last_trigger: dict[str, float] = {}  # market_id → timestamp

    def update(self, market_id: str, price: float) -> list[dict]:
        """Update price for a market. Returns list of detected moves (0 or 1)."""
        now = time.monotonic()
        moves = []

        if market_id in self._last_prices:
            prev = self._last_prices[market_id]
            delta = price - prev

            if abs(delta) >= self._threshold:
                # Check cooldown
                last = self._last_trigger.get(market_id, 0)
                if now - last >= self._cooldown_seconds:
                    moves.append({
                        "market_id": market_id,
                        "previous_price": prev,
                        "current_price": price,
                        "magnitude": abs(delta),
                        "direction": "up" if delta > 0 else "down",
                    })
                    self._last_trigger[market_id] = now

        self._last_prices[market_id] = price
        return moves

    def batch_update(self, prices: dict[str, float]) -> list[dict]:
        """Update prices for multiple markets. Returns all detected moves."""
        all_moves = []
        for market_id, price in prices.items():
            all_moves.extend(self.update(market_id, price))
        return all_moves
```

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_move_detector.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
cd ~/polybot
git add polybot/analysis/move_detector.py tests/test_move_detector.py
git commit -m "feat: add MoveDetector for large price move detection between snapshots"
```

---

### Task 2: NewsCatalystStrategy

The core strategy that reacts to detected price moves with LLM-informed trades.

**Files:**
- Create: `polybot/strategies/news_catalyst.py`
- Create: `tests/test_news_catalyst.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_news_catalyst.py` with tests for:

1. `test_detects_and_trades_large_move` — mock a 10% price drop, LLM says new fair value is 5% above current price, trade fires
2. `test_skips_when_llm_agrees_with_move` — price dropped 10%, LLM says the move is justified (new prob close to current price), no trade
3. `test_respects_position_cap` — at max positions, no new trades
4. `test_cooldown_prevents_repeated_triggers` — same market can't trigger twice in 5 min
5. `test_skips_illiquid_markets` — markets below min liquidity are ignored

- [ ] **Step 2: Implement NewsCatalystStrategy**

Create `polybot/strategies/news_catalyst.py`:

```python
"""News Catalyst Speed Trading.

Detects sudden price moves (>5% between scanner snapshots), interprets
them via LLM, and trades the gap if the market hasn't fully adjusted.
Targets the 30s-5min news reaction window.
"""

import json
import structlog
from datetime import datetime, timezone

from polybot.strategies.base import Strategy, TradingContext
from polybot.analysis.move_detector import MoveDetector
from polybot.trading.kelly import compute_kelly, compute_position_size
from polybot.trading.risk import PortfolioState, TradeProposal, bankroll_kelly_adjustment
from polybot.notifications.email import format_trade_email

log = structlog.get_logger()


class NewsCatalystStrategy(Strategy):
    name = "news_catalyst"

    def __init__(self, settings, ensemble, researcher):
        self.interval_seconds = float(getattr(settings, "nc_interval_seconds", 120.0))
        self.kelly_multiplier = float(getattr(settings, "nc_kelly_mult", 0.30))
        self.max_single_pct = float(getattr(settings, "nc_max_single_pct", 0.15))
        self._move_threshold = float(getattr(settings, "nc_move_threshold", 0.05))
        self._min_gap = float(getattr(settings, "nc_min_gap", 0.05))
        self._min_liquidity = float(getattr(settings, "nc_min_liquidity", 50000.0))
        self._max_positions = int(getattr(settings, "nc_max_positions", 3))
        self._cooldown_seconds = float(getattr(settings, "nc_cooldown_seconds", 300.0))
        self._settings = settings
        self._ensemble = ensemble
        self._researcher = researcher
        self._detector = MoveDetector(
            threshold=self._move_threshold,
            cooldown_seconds=self._cooldown_seconds)

    async def run_once(self, ctx: TradingContext) -> None:
        # Check enabled
        enabled = await ctx.db.fetchval(
            "SELECT enabled FROM strategy_performance WHERE strategy = $1",
            self.name)
        if enabled is False:
            return

        state = await ctx.db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return
        bankroll = float(state["bankroll"])

        # Position cap
        open_count = await ctx.db.fetchval(
            "SELECT COUNT(*) FROM trades WHERE strategy = $1 AND status IN ('open','filled','dry_run')",
            self.name)
        if (open_count or 0) >= self._max_positions:
            return

        # Scan markets and detect moves
        markets = await ctx.scanner.fetch_markets()
        if not markets:
            return

        price_map = {m["polymarket_id"]: m["yes_price"] for m in markets}
        moves = self._detector.batch_update(price_map)

        if not moves:
            return

        log.info("nc_moves_detected", count=len(moves))

        # Build lookup for full market data
        market_lookup = {m["polymarket_id"]: m for m in markets}

        for move in moves:
            mid = move["market_id"]
            m = market_lookup.get(mid)
            if not m:
                continue
            if m.get("book_depth", 0) < self._min_liquidity:
                continue

            # Get news context + LLM estimate
            try:
                research = await self._researcher.search(m["question"])
                quick_prob = await self._ensemble.quick_screen(
                    m["question"], m["yes_price"],
                    m["resolution_time"].isoformat() if hasattr(m["resolution_time"], "isoformat") else "")
            except Exception as e:
                log.error("nc_llm_error", market=mid, error=str(e))
                continue

            if quick_prob is None:
                continue

            # Compute gap between LLM estimate and current market price
            gap = quick_prob - m["yes_price"]

            if abs(gap) < self._min_gap:
                log.debug("nc_gap_too_small", market=mid, gap=round(gap, 4))
                continue

            # Trade toward the LLM estimate
            side = "YES" if gap > 0 else "NO"
            edge = abs(gap)
            buy_price = m["yes_price"] if side == "YES" else (1.0 - m["yes_price"])

            log.info("nc_opportunity", market=mid,
                     question=m["question"][:50], side=side,
                     move_direction=move["direction"],
                     move_magnitude=round(move["magnitude"], 4),
                     llm_prob=round(quick_prob, 4),
                     market_price=m["yes_price"],
                     gap=round(gap, 4))

            # Kelly sizing
            kelly_result = compute_kelly(quick_prob, m["yes_price"], fee_per_dollar=0.0)
            if kelly_result.kelly_fraction <= 0:
                continue

            adjusted_kelly = bankroll_kelly_adjustment(
                bankroll=bankroll,
                base_kelly=self.kelly_multiplier,
                post_breaker_until=state.get("circuit_breaker_until"),
                post_breaker_reduction=self._settings.post_breaker_kelly_reduction,
                survival_threshold=self._settings.bankroll_survival_threshold,
                growth_threshold=self._settings.bankroll_growth_threshold)

            size = compute_position_size(
                bankroll=bankroll,
                kelly_fraction=kelly_result.kelly_fraction,
                kelly_mult=adjusted_kelly,
                max_single_pct=self.max_single_pct,
                min_trade_size=self._settings.min_trade_size)
            if size <= 0:
                continue

            async with ctx.portfolio_lock:
                # Dedup
                existing = await ctx.db.fetchval(
                    "SELECT COUNT(*) FROM trades WHERE market_id IN "
                    "(SELECT id FROM markets WHERE polymarket_id = $1) "
                    "AND strategy = $2 AND status IN ('open','filled','dry_run')",
                    mid, self.name)
                if existing and existing > 0:
                    continue

                market_id = await ctx.db.fetchval(
                    """INSERT INTO markets (polymarket_id, question, category, resolution_time,
                           current_price, volume_24h, book_depth)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (polymarket_id) DO UPDATE SET
                           current_price=$5, volume_24h=$6, book_depth=$7, last_updated=NOW()
                       RETURNING id""",
                    mid, m["question"], m.get("category", "unknown"),
                    m["resolution_time"], m["yes_price"],
                    m.get("volume_24h"), m.get("book_depth"))

                analysis_id = await ctx.db.fetchval(
                    """INSERT INTO analyses (market_id, model_estimates, ensemble_probability,
                       ensemble_stdev, quant_signals, edge, web_research_summary)
                       VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                    market_id, json.dumps([]),
                    quick_prob, 0.0,
                    json.dumps({"source": "news_catalyst",
                                "move_direction": move["direction"],
                                "move_magnitude": move["magnitude"]}),
                    edge, research)

                token_id = m["yes_token_id"] if side == "YES" else m["no_token_id"]

                # Store time-stop in kelly_inputs for position manager
                await ctx.executor.place_order(
                    token_id=token_id, side=side, size_usd=size, price=buy_price,
                    market_id=market_id, analysis_id=analysis_id, strategy=self.name,
                    kelly_inputs={
                        "llm_prob": round(quick_prob, 4),
                        "market_price": round(m["yes_price"], 4),
                        "edge": round(edge, 4),
                        "move_direction": move["direction"],
                        "move_magnitude": round(move["magnitude"], 4),
                        "source": "news_catalyst",
                        "max_hold_hours": 0.5,  # 30 min time-stop
                    },
                    post_only=self._settings.use_maker_orders)

            await ctx.email_notifier.send(
                f"[POLYBOT] News catalyst: {m['question'][:50]}",
                format_trade_email(event="executed", market=m["question"],
                                   side=side, size=size, price=buy_price, edge=edge))
```

- [ ] **Step 3: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_news_catalyst.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
cd ~/polybot
git add polybot/strategies/news_catalyst.py tests/test_news_catalyst.py
git commit -m "feat: add NewsCatalystStrategy — reacts to large price moves with LLM interpretation"
```

---

### Task 3: Config keys and wiring

**Files:**
- Modify: `polybot/core/config.py`
- Modify: `polybot/__main__.py`

- [ ] **Step 1: Add config keys**

```python
    # News catalyst strategy
    nc_enabled: bool = True
    nc_interval_seconds: float = 120.0         # 2 min scan (fast reaction)
    nc_kelly_mult: float = 0.30                # moderate sizing — news trades are uncertain
    nc_max_single_pct: float = 0.15
    nc_move_threshold: float = 0.05            # 5% move triggers investigation
    nc_min_gap: float = 0.05                   # 5% gap between LLM and market to trade
    nc_min_liquidity: float = 50000.0
    nc_max_positions: int = 3                  # max 3 concurrent news trades
    nc_cooldown_seconds: float = 300.0         # 5 min cooldown per market
```

- [ ] **Step 2: Add `news_catalyst` to DB constraint**

```sql
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_strategy_check;
ALTER TABLE trades ADD CONSTRAINT trades_strategy_check
  CHECK (strategy = ANY (ARRAY['arbitrage', 'snipe', 'forecast', 'market_maker',
                                'mean_reversion', 'cross_venue', 'political', 'news_catalyst']));
```

- [ ] **Step 3: Wire into __main__.py**

```python
    if getattr(settings, 'nc_enabled', True):
        from polybot.strategies.news_catalyst import NewsCatalystStrategy
        nc_strategy = NewsCatalystStrategy(
            settings=settings, ensemble=ensemble, researcher=researcher)
        engine.add_strategy(nc_strategy)
        await db.execute(
            """INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled)
               VALUES ('news_catalyst', 0, 0, 0, 0, true) ON CONFLICT (strategy) DO NOTHING""")
```

- [ ] **Step 4: Run full test suite, restart, verify**

Run: `cd ~/polybot && uv run pytest -x -q`
Expected: ALL PASS

Restart and monitor:
```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
sleep 120 && grep "nc_" ~/polybot/data/polybot_stdout.log | tail -5
```

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/core/config.py polybot/__main__.py
git commit -m "feat: wire NewsCatalystStrategy with config and DB constraint"
```

---

### Task 4: Position manager time-stop for news catalyst trades

News catalyst trades should have a tight 30-min time-stop (stored in `kelly_inputs.max_hold_hours`). Add strategy-specific handling in position_manager.py.

**Files:**
- Modify: `polybot/trading/position_manager.py`

- [ ] **Step 1: Add news_catalyst exit logic**

In `check_positions()`, after the political strategy block, add:

```python
            # News catalyst: tight time-stop from kelly_inputs
            if pos["strategy"] == "news_catalyst" and pos.get("kelly_inputs"):
                ki = json.loads(pos["kelly_inputs"]) if isinstance(pos["kelly_inputs"], str) else pos["kelly_inputs"]
                max_hold = float(ki.get("max_hold_hours", 0.5))
                hold_hours = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 3600
                if hold_hours > max_hold:
                    # Execute time-stop
                    ...
                continue  # Skip generic exit logic
```

- [ ] **Step 2: Run tests, commit**

```bash
cd ~/polybot && uv run pytest -x -q
git add polybot/trading/position_manager.py
git commit -m "feat: news catalyst 30-min time-stop via kelly_inputs.max_hold_hours"
```
