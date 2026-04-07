# Fix Snipe Odds Verification Credit Spam

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate `odds_api_credits_low` log spam from snipe's per-candidate odds verification when Odds API credits are exhausted.

**Architecture:** Add a `credits_exhausted` property to `OddsClient`, then short-circuit `verify_snipe_via_odds()` before the 5-sport loop when credits are gone. Refactor `fetch_all_sports()` to use the same property (DRY).

**Tech Stack:** Python 3.13, asyncio, pytest, structlog, aiohttp

---

### Task 1: Add `credits_exhausted` property to OddsClient

**Problem:** The credit-exhaustion check (`_credits_remaining is not None and _credits_remaining <= _credit_reserve`) is duplicated in `fetch_odds()` and `fetch_all_sports()`, and callers outside the class (like `verify_snipe_via_odds`) can't access it without reaching into private attributes.

**Files:**
- Modify: `polybot/analysis/odds_client.py:191-193`
- Modify: `polybot/analysis/odds_client.py:142-146` (fetch_odds guard)
- Modify: `polybot/analysis/odds_client.py:176-182` (fetch_all_sports guard)
- Test: `tests/test_odds_client.py`

- [ ] **Step 1: Write failing test for the property**

In `tests/test_odds_client.py`, add at the bottom of the file:

```python
class TestOddsClientCreditsExhausted:
    def test_exhausted_when_zero(self):
        client = OddsClient(api_key="test", sports=[])
        client._credits_remaining = 0
        assert client.credits_exhausted is True

    def test_exhausted_when_at_reserve(self):
        client = OddsClient(api_key="test", sports=[], credit_reserve=10)
        client._credits_remaining = 10
        assert client.credits_exhausted is True

    def test_not_exhausted_when_above_reserve(self):
        client = OddsClient(api_key="test", sports=[], credit_reserve=10)
        client._credits_remaining = 50
        assert client.credits_exhausted is False

    def test_not_exhausted_when_unknown(self):
        """Before any API call, credits_remaining is None — should not be treated as exhausted."""
        client = OddsClient(api_key="test", sports=[])
        assert client.credits_exhausted is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/polybot && uv run pytest tests/test_odds_client.py::TestOddsClientCreditsExhausted -v`

Expected: FAIL — `credits_exhausted` property doesn't exist yet.

- [ ] **Step 3: Add the property and refactor existing guards**

In `polybot/analysis/odds_client.py`, add the property after the existing `credits_remaining` property (after line 193):

```python
    @property
    def credits_exhausted(self) -> bool:
        """True when credits are known to be at or below the reserve threshold."""
        return (self._credits_remaining is not None
                and self._credits_remaining <= self._credit_reserve)
```

Then refactor `fetch_odds()` guard (lines 142-146) from:

```python
        if (self._credits_remaining is not None
                and self._credits_remaining <= self._credit_reserve):
            log.warning("odds_api_credits_low", credits_remaining=self._credits_remaining,
                        credit_reserve=self._credit_reserve)
            return []
```

To:

```python
        if self.credits_exhausted:
            log.warning("odds_api_credits_low", credits_remaining=self._credits_remaining,
                        credit_reserve=self._credit_reserve)
            return []
```

And refactor `fetch_all_sports()` guard (lines 176-180) from:

```python
        if (self._credits_remaining is not None
                and self._credits_remaining <= self._credit_reserve):
            log.info("odds_credits_exhausted", credits_remaining=self._credits_remaining,
                     credit_reserve=self._credit_reserve)
            return []
```

To:

```python
        if self.credits_exhausted:
            log.info("odds_credits_exhausted", credits_remaining=self._credits_remaining,
                     credit_reserve=self._credit_reserve)
            return []
```

- [ ] **Step 4: Run all odds client tests**

Run: `cd ~/polybot && uv run pytest tests/test_odds_client.py -v`

Expected: All pass (new property tests + existing tests unchanged behavior).

- [ ] **Step 5: Commit**

```bash
cd ~/polybot && git add polybot/analysis/odds_client.py tests/test_odds_client.py
git commit -m "$(cat <<'EOF'
refactor: add credits_exhausted property to OddsClient

Encapsulates the credit-check condition into a public property so callers
outside the class can check without accessing private attributes. Refactors
fetch_odds() and fetch_all_sports() to use it (DRY).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Short-circuit `verify_snipe_via_odds()` when credits exhausted

**Problem:** `verify_snipe_via_odds()` loops through 5 sports, calling `odds_client.fetch_odds()` for each. When credits are 0, each call logs `odds_api_credits_low` and returns `[]`. This produces 5 warning logs per snipe candidate — hundreds per scan cycle.

**Files:**
- Modify: `polybot/strategies/snipe.py:89-139`
- Test: `tests/test_snipe.py`

- [ ] **Step 1: Write failing test**

In `tests/test_snipe.py`, add:

```python
@pytest.mark.asyncio
async def test_odds_verify_short_circuits_when_credits_exhausted():
    """verify_snipe_via_odds should return False immediately when credits are exhausted."""
    from polybot.strategies.snipe import verify_snipe_via_odds
    from unittest.mock import AsyncMock, MagicMock, PropertyMock

    odds_client = MagicMock()
    type(odds_client).credits_exhausted = PropertyMock(return_value=True)
    odds_client.fetch_odds = AsyncMock()

    result = await verify_snipe_via_odds(
        odds_client=odds_client,
        question="Will the Los Angeles Lakers win?",
        side="YES",
        min_consensus=0.85,
    )

    assert result is False
    odds_client.fetch_odds.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/polybot && uv run pytest tests/test_snipe.py::test_odds_verify_short_circuits_when_credits_exhausted -v`

Expected: FAIL — currently `verify_snipe_via_odds` doesn't check `credits_exhausted`.

- [ ] **Step 3: Add the short-circuit**

In `polybot/strategies/snipe.py`, modify `verify_snipe_via_odds()` (line 89). Add a credit check right after the docstring, before the sport loop:

```python
async def verify_snipe_via_odds(
    odds_client,
    question: str,
    side: str,
    min_consensus: float = 0.85,
) -> bool:
    """Verify a snipe candidate using sportsbook consensus instead of LLM.

    Searches The Odds API events for a matching event by team name,
    then checks if the sportsbook consensus supports the snipe direction.

    Returns True if sportsbook consensus confirms the snipe, False otherwise.
    """
    if odds_client.credits_exhausted:
        return False

    q_lower = question.lower()
```

That's it — one line added after the docstring.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/polybot && uv run pytest tests/test_snipe.py::test_odds_verify_short_circuits_when_credits_exhausted -v`

Expected: PASS

- [ ] **Step 5: Run full snipe + odds client test suites**

Run: `cd ~/polybot && uv run pytest tests/test_snipe.py tests/test_odds_client.py -v`

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
cd ~/polybot && git add polybot/strategies/snipe.py tests/test_snipe.py
git commit -m "$(cat <<'EOF'
fix: short-circuit snipe odds verification when API credits exhausted

verify_snipe_via_odds looped through 5 sports calling fetch_odds() each
time, producing 5 warning logs per snipe candidate when credits were 0.
Now checks credits_exhausted upfront and returns False immediately.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Run full test suite and restart bot

- [ ] **Step 1: Run all tests**

Run: `cd ~/polybot && uv run pytest tests/ -v --tb=short`

Expected: 492+ tests pass, 0 failures.

- [ ] **Step 2: Restart bot**

```bash
launchctl stop ai.polybot.trader && sleep 2 && launchctl start ai.polybot.trader
```

- [ ] **Step 3: Verify no more credit spam after first cycle**

Wait ~90 seconds for one full snipe scan cycle, then:

```bash
tail -40 ~/polybot/data/polybot_stdout.log
```

Expected: No `odds_api_credits_low` warnings after the initial NBA fetch (which sets `_credits_remaining=0`). Snipe candidates should fall through to LLM verification directly.
