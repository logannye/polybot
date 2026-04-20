"""Tests for polybot.analysis.gemini_client — pure logic only (no network)."""
import pytest
from polybot.analysis.gemini_client import (
    GeminiClient, GeminiResult, _parse_verdict, DailySpendTracker,
)


def test_parse_valid_yes_locked():
    result = _parse_verdict('{"verdict": "YES_LOCKED", "confidence": 0.95}')
    assert result.verdict == "YES_LOCKED"
    assert result.confidence == 0.95


def test_parse_valid_with_markdown_fence():
    result = _parse_verdict('```json\n{"verdict": "NO_LOCKED", "confidence": 0.88}\n```')
    assert result.verdict == "NO_LOCKED"
    assert result.confidence == 0.88


def test_parse_invalid_json_returns_uncertain():
    result = _parse_verdict("not json")
    assert result.verdict == "UNCERTAIN"
    assert result.confidence == 0.0


def test_parse_invalid_verdict_value_defaults_uncertain():
    result = _parse_verdict('{"verdict": "MAYBE", "confidence": 0.5}')
    assert result.verdict == "UNCERTAIN"
    assert result.confidence == 0.5


def test_parse_confidence_clamped():
    result = _parse_verdict('{"verdict": "YES_LOCKED", "confidence": 1.5}')
    assert result.confidence == 1.0


def test_parse_negative_confidence_clamped():
    result = _parse_verdict('{"verdict": "YES_LOCKED", "confidence": -0.2}')
    assert result.confidence == 0.0


def test_can_spend_initially_true():
    client = GeminiClient(api_key="fake", cap_usd=2.0)
    assert client.can_spend() is True


def test_daily_spend_tracker_accumulates():
    t = DailySpendTracker()
    t.accumulate(0.5)
    t.accumulate(0.3)
    assert t.current_spend() == pytest.approx(0.8)


def test_daily_spend_tracker_resets_on_new_day(monkeypatch):
    t = DailySpendTracker()
    t.accumulate(0.5)
    # Force date rollover by setting date_utc to yesterday
    t.date_utc = "2000-01-01"
    assert t.current_spend() == 0.0


@pytest.mark.asyncio
async def test_verify_snipe_returns_uncertain_when_cap_hit():
    client = GeminiClient(api_key="fake", cap_usd=0.001)   # effectively zero
    # Force spend to exceed cap
    client._spend.accumulate(0.01)
    result = await client.verify_snipe(
        question="test?", resolution_time_iso="2026-04-30T00:00:00Z",
        hours_remaining=2.0, yes_price=0.98,
    )
    assert result.verdict == "UNCERTAIN"
    assert result.confidence == 0.0
