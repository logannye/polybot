from polybot.analysis.prompts import (
    build_snipe_prompt, parse_snipe_response,
    build_quick_screen_prompt,
)


def test_build_snipe_prompt_contains_key_fields():
    prompt = build_snipe_prompt("Will X happen?", "2026-03-29T00:00:00Z", 3.5, 0.94)
    assert "Will X happen?" in prompt
    assert "3.5" in prompt
    assert "0.94" in prompt
    assert "ALREADY DETERMINED" in prompt


def test_parse_snipe_response_valid():
    raw = '{"determined": true, "outcome": "YES", "confidence": 0.95, "reason": "Event occurred"}'
    result = parse_snipe_response(raw)
    assert result is not None
    assert result["determined"] is True
    assert result["outcome"] == "YES"
    assert result["confidence"] == 0.95


def test_parse_snipe_response_invalid():
    result = parse_snipe_response("I think yes")
    assert result is None


def test_parse_snipe_response_code_block():
    raw = '```json\n{"determined": false, "outcome": "UNKNOWN", "confidence": 0.3, "reason": "Pending"}\n```'
    result = parse_snipe_response(raw)
    assert result is not None
    assert result["determined"] is False


def test_parse_snipe_response_clamps_confidence():
    raw = '{"determined": true, "outcome": "YES", "confidence": 1.5, "reason": "Sure"}'
    result = parse_snipe_response(raw)
    assert result["confidence"] == 1.0


def test_parse_snipe_response_normalizes_outcome():
    raw = '{"determined": true, "outcome": "MAYBE", "confidence": 0.5, "reason": "Unsure"}'
    result = parse_snipe_response(raw)
    assert result["outcome"] == "UNKNOWN"


def test_build_quick_screen_prompt():
    prompt = build_quick_screen_prompt("Will Y happen?", 0.65, "2026-03-30T12:00:00Z")
    assert "Will Y happen?" in prompt
    assert "0.65" in prompt
    assert "probability" in prompt.lower()
