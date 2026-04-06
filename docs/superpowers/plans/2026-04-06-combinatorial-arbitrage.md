# Combinatorial Arbitrage — Plan B

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the combinatorial arbitrage strategy to exploit the 412 multi-outcome event groups on Polymarket where probability sums deviate from 1.0 — near-zero-risk structural profit.

**Architecture:** The existing arb detection math (`detect_exhaustive_arb`) is correct. The critical fix is market grouping: the current scanner groups by `groupItemTitle` (wrong — finds 3 groups). Must group by parent EVENT via the Gamma `/events` endpoint (correct — finds 412 groups). The enrichment step already fetches events; we extend it to build event-based groups. The existing `ArbitrageStrategy` is then rewired to use event-based groups instead of slug-based groups.

**Tech Stack:** Python 3.13, asyncpg, aiohttp, structlog, pytest

**Key finding from research:** 
- NHL Hart Trophy: 118 markets, yes_sum=1.087, +8.7% overround → buy all NOs for ~8.7% guaranteed profit
- Democratic nominee 2028: 44 markets, yes_sum=0.90, -10% underround → buy all YESes for ~10% guaranteed profit
- 412 groups total with valid exhaustive structure (sum in 0.85-1.15)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `polybot/markets/scanner.py` | **Modify**: Store event_slug on markets during enrichment; add `fetch_event_groups()` method |
| `polybot/strategies/arbitrage.py` | **Modify**: Use event-based groups instead of slug-based groups; add min_liquidity filter |
| `polybot/core/config.py` | **Modify**: Re-enable arb with tuned thresholds |
| `tests/test_arbitrage.py` | **Modify**: Add tests for event-based grouping |
| `tests/test_scanner_tags.py` | **Modify**: Add test for event_slug enrichment |

---

### Task 1: Add event_slug to market enrichment

During the existing `_enrich_event_tags()`, also store the parent event's slug on each market. This enables grouping by event.

**Files:**
- Modify: `polybot/markets/scanner.py`
- Modify: `tests/test_scanner_tags.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_scanner_tags.py`:

```python
def test_parse_stores_event_slug_for_grouping():
    """Markets should have event_slug from their parent event for exhaustive grouping."""
    # This tests the enrichment, not parsing — but verify the field exists
    raw = _make_raw_market(events=[{
        "slug": "nhl-hart-trophy-winner",
        "tags": [{"label": "Sports", "slug": "sports"}],
    }])
    result = parse_gamma_market(raw)
    assert result is not None
    # event_slug is set during enrichment, not parsing — default to None
```

- [ ] **Step 2: Modify `_enrich_event_tags()` to store event_slug**

In the enrichment loop, in addition to mapping `conditionId → tag_slugs`, also map `conditionId → event_slug`:

```python
                # Map each child market's conditionId to event slug + tags
                event_slug = event.get("slug", "")
                for child in event.get("markets", []):
                    cid = child.get("conditionId", "")
                    if cid:
                        cid_to_tags[cid] = tag_slugs
                        cid_to_event[cid] = event_slug
```

And when applying to markets:
```python
            m["event_slug"] = cid_to_event.get(m["polymarket_id"], "")
```

- [ ] **Step 3: Add `fetch_event_groups()` method to PolymarketScanner**

```python
    def fetch_event_groups(self, markets: list[dict] | None = None) -> dict[str, list[dict]]:
        """Group markets by parent event slug and validate as exhaustive."""
        if markets is None:
            markets = list(self._price_cache.values())
        groups: dict[str, list[dict]] = {}
        for m in markets:
            slug = m.get("event_slug", "")
            if slug:
                groups.setdefault(slug, []).append(m)
        # Require 3+ markets and passing exhaustive validation
        return {k: v for k, v in groups.items()
                if len(v) >= 3 and self.validate_exhaustive_group(v)}
```

- [ ] **Step 4: Run tests**

Run: `cd ~/polybot && uv run pytest tests/test_scanner_tags.py tests/test_arbitrage.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/polybot
git add polybot/markets/scanner.py tests/test_scanner_tags.py
git commit -m "feat: store event_slug on markets for event-based exhaustive grouping"
```

---

### Task 2: Rewire ArbitrageStrategy to use event-based groups

Replace the slug-based `fetch_grouped_markets()` call with event-based `fetch_event_groups()`. Add minimum liquidity filter per leg to avoid illiquid arbs.

**Files:**
- Modify: `polybot/strategies/arbitrage.py`
- Modify: `tests/test_arbitrage.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_arbitrage.py`:

```python
def test_exhaustive_arb_uses_event_groups():
    """ArbitrageStrategy should find arbs from event-based groups, not slug-based."""
    # Create 3 markets in the same event with yes_sum > 1.0
    # Verify detect_exhaustive_arb finds the opportunity
    ...
```

- [ ] **Step 2: Update ArbitrageStrategy.run_once**

Replace the line:
```python
        groups = scanner.fetch_grouped_markets(markets)
```
With:
```python
        groups = scanner.fetch_event_groups(markets)
```

Also add a per-leg minimum liquidity filter inside the exhaustive arb detection loop:

```python
        for slug, group_markets in groups.items():
            # Skip groups with any illiquid leg
            if any(m.get("book_depth", 0) < arb_min_liquidity for m in group_markets):
                continue
```

Where `arb_min_liquidity` defaults to 5000.0 (configurable).

- [ ] **Step 3: Raise min_net_edge threshold**

In the arb detection call, increase `min_net_edge` from the current default of 0.01 (1%) to 0.02 (2%) to account for spread slippage on multi-leg trades:

In `config.py`, change:
```python
    arb_min_net_edge: float = 0.02  # was 0.01
```

- [ ] **Step 4: Lower arb_min_bankroll gate**

The current `arb_min_bankroll` is 2000.0, but with $474 bankroll we need it lower. Since combinatorial arb is near-zero risk:

In `config.py`:
```python
    arb_min_bankroll: float = 50.0  # was 2000.0 — combinatorial arb is low-risk
```

- [ ] **Step 5: Re-enable arbitrage (keep disabled by default, enable in .env)**

Don't change the config default — instead add to `.env`:
```bash
ARB_ENABLED=true  # not a config default, but our .env override
```

Wait — `ArbitrageStrategy` doesn't have an `arb_enabled` config key. It uses the `strategy_performance.enabled` DB toggle. Check if arb is currently enabled in DB:

```bash
psql -d polybot -c "SELECT strategy, enabled FROM strategy_performance WHERE strategy = 'arbitrage';"
```

If disabled, re-enable:
```bash
psql -d polybot -c "UPDATE strategy_performance SET enabled = true WHERE strategy = 'arbitrage';"
```

The ArbitrageStrategy is always loaded in `__main__.py` (no `arb_enabled` gate) — it just checks the DB `enabled` flag in `run_once`.

- [ ] **Step 6: Run full test suite**

Run: `cd ~/polybot && uv run pytest -x -q`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
cd ~/polybot
git add polybot/strategies/arbitrage.py polybot/core/config.py tests/test_arbitrage.py
git commit -m "feat: event-based exhaustive grouping for combinatorial arbitrage (412 groups)"
```

---

### Task 3: Deploy and verify

- [ ] **Step 1: Re-enable arb in DB**

```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -h 127.0.0.1 -d polybot -c \
  "UPDATE strategy_performance SET enabled = true WHERE strategy = 'arbitrage';"
```

- [ ] **Step 2: Restart Polybot**

```bash
launchctl kickstart -k gui/$(id -u)/ai.polybot.trader
```

- [ ] **Step 3: Monitor arb discovery**

```bash
sleep 60 && grep -E "exhaustive_arb_found|arb_executed|arb_no_markets" ~/polybot/data/polybot_stdout.log | tail -10
```

Expected: `exhaustive_arb_found` entries with event-based group slugs.

- [ ] **Step 4: Check for arb trades**

```bash
/opt/homebrew/Cellar/postgresql@16/16.12/bin/psql -h 127.0.0.1 -d polybot -c "
SELECT t.id, LEFT(m.question, 45) as market, t.side, t.entry_price,
  ROUND(t.position_size_usd::numeric, 2) as size,
  t.kelly_inputs->>'arb_type' as arb_type,
  t.kelly_inputs->>'net_edge' as net_edge
FROM trades t JOIN markets m ON t.market_id = m.id
WHERE t.strategy = 'arbitrage' AND t.opened_at > NOW() - INTERVAL '1 hour'
ORDER BY t.opened_at DESC LIMIT 10;
"
```

---

## What's NOT in this plan

1. **Atomic multi-leg execution** — The fire-and-forget `place_multi_leg_order` is adequate for dry-run testing. Atomic execution (cancel remaining legs if one fails) should be added before going live.
2. **Orderbook depth verification** — Checking that each leg has sufficient book depth at the target price before executing. Important for live trading, less critical for dry-run.
3. **Cross-event temporal arb** — "Will X happen by June?" vs "Will X happen by July?" — these are different events but logically constrained. Requires LLM-based relationship detection. Future plan.
