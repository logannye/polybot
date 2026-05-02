"""Tests for the v12 verifier grounding regex."""
import pytest
from polybot.analysis.gemini_client import (
    validate_reason, _parse_verdict, GeminiResult,
)


# ── validate_reason — pure function ──────────────────────────────────────
def test_concrete_reason_with_time_passes():
    ok, why = validate_reason("Trump conceded at 8:14pm ET; AP called the race.")
    assert ok is True


def test_concrete_reason_with_score_passes():
    ok, why = validate_reason("Game ended 7-2 in the 9th inning, MLB final.")
    assert ok is True


def test_concrete_reason_with_proper_noun_passes():
    ok, why = validate_reason("AP called the race for X candidate at the polling close.")
    assert ok is True


def test_too_short_reason_rejected():
    ok, why = validate_reason("Race over.")
    assert ok is False
    assert why == "reason_too_short"


def test_hedge_without_grounding_rejected():
    ok, why = validate_reason(
        "It seems likely that the favorite probably wins this market here.")
    assert ok is False
    assert why == "hedge_without_grounding"


def test_hedge_with_grounding_passes():
    ok, why = validate_reason(
        "Trump probably won — AP called Pennsylvania at 11:23pm with 99% reporting.")
    assert ok is True


def test_min_chars_param_is_respected():
    short = "AP called race at 9pm."
    ok, _ = validate_reason(short, min_chars=10)
    assert ok is True
    ok, _ = validate_reason(short, min_chars=100)
    assert ok is False


# ── Non-lock semantic gate (added after T+1h shadow log surfaced this) ───
# These reasons were observed in production at conf=1.0 NO_LOCKED. They
# describe the prior (event hasn't happened yet) not a lock (event is
# impossible). All must be rejected.

@pytest.mark.parametrize("bad_reason", [
    # Direct quotes from observed bad verdicts
    "The 2026 West Bengal Legislative Assembly election has not yet occurred, "
    "and its results will not be announced before April 30.",
    "The resolution date for the market is April 30, 2026, which is in the "
    "future. The condition for the market's outcome can still happen.",
    "The resolution deadline of April 30, 2026, has not yet passed, meaning "
    "the event could still occur within the remaining 95.3 hours.",
    # Variants that should also be caught
    "The event has not yet happened — resolution is on May 5, 2026.",
    "The election is in the future and the outcome could still occur.",
    "The Federal Reserve announcement remains in the future.",
    "Outcome is not yet determined as of this morning.",
    "The result has not yet been announced by the AP.",
    "There are remaining 12 hours before the deadline passes.",
])
def test_non_lock_phrasing_rejected(bad_reason):
    ok, why = validate_reason(bad_reason)
    assert ok is False, f"Should reject: {bad_reason!r}"
    assert why == "non_lock_phrasing"


def test_genuine_lock_with_structural_prerequisite_passes():
    """The FOMC case: 'no April 2026 meeting' is a real lock — structural
    prerequisite missing means YES is impossible. Must NOT be flagged."""
    ok, why = validate_reason(
        "The Federal Open Market Committee has no scheduled meeting in April "
        "2026, so a rate cut at that meeting is structurally impossible.")
    assert ok is True, f"FOMC structural-impossibility lock rejected: {why}"


def test_genuine_lock_event_already_occurred_passes():
    ok, why = validate_reason(
        "AP called the race for Smith at 8:14pm ET on April 15, 2026; "
        "concession speech delivered before resolution.")
    assert ok is True


def test_deadline_passed_without_occurrence_passes():
    ok, why = validate_reason(
        "The April 1, 2026 deadline elapsed two weeks ago without any "
        "qualifying announcement from the SEC.")
    assert ok is True


# ── _parse_verdict — JSON parsing ────────────────────────────────────────
def test_parse_valid_yes_locked():
    r = _parse_verdict('{"verdict":"YES_LOCKED","confidence":0.95,"reason":"AP called race at 9pm"}')
    assert r.verdict == "YES_LOCKED"
    assert r.confidence == 0.95
    assert "AP" in r.reason


def test_parse_with_markdown_fence():
    r = _parse_verdict('```json\n{"verdict":"NO_LOCKED","confidence":0.99,"reason":"x"}\n```')
    assert r.verdict == "NO_LOCKED"
    assert r.confidence == 0.99


def test_parse_malformed_returns_uncertain():
    r = _parse_verdict("not json at all")
    assert r.verdict == "UNCERTAIN"
    assert r.confidence == 0.0
    assert r.reason == "malformed_json"


def test_parse_invalid_verdict_defaults_uncertain():
    r = _parse_verdict('{"verdict":"MAYBE","confidence":0.5,"reason":"x"}')
    assert r.verdict == "UNCERTAIN"


def test_parse_confidence_clamped():
    r = _parse_verdict('{"verdict":"YES_LOCKED","confidence":1.5,"reason":"x"}')
    assert r.confidence == 1.0
    r = _parse_verdict('{"verdict":"YES_LOCKED","confidence":-0.2,"reason":"x"}')
    assert r.confidence == 0.0
