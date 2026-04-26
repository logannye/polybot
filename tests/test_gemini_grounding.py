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
