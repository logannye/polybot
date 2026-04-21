"""Tests for scanner.fetch_live_sports_events + _flatten_event_to_markets.

Shape of Gamma /events response was captured live 2026-04-20.
"""
import json
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock

from polybot.markets.scanner import (
    _flatten_event_to_markets, _maybe_json_list, _parse_isoformat,
)


def _nba_event_live() -> dict:
    """Observed real shape 2026-04-20: Raptors vs. Cavaliers."""
    return {
        "slug": "nba-tor-cle-2026-04-20",
        "title": "Raptors vs. Cavaliers",
        "endDate": "2026-04-20T23:00:00Z",
        "tags": [{"slug": "sports"}, {"slug": "nba"}, {"slug": "games"}],
        "markets": [
            {
                "conditionId": "0xda955a9e3195606bd9" + "a" * 46,
                "question": "Raptors vs. Cavaliers",
                "slug": "nba-tor-cle-2026-04-20",
                "endDate": "2026-04-20T23:00:00Z",
                "outcomes": '["Raptors", "Cavaliers"]',
                "outcomePrices": '["0.125", "0.875"]',
                "clobTokenIds": '["151430577", "909876543"]',
                "active": True, "closed": False, "acceptingOrders": True,
                "volume": 1546162.3, "liquidity": 191135.2,
            },
            {
                # Total market — same event, O/U line
                "conditionId": "0x1111" + "b" * 60,
                "question": "Raptors vs. Cavaliers: O/U 221.5",
                "slug": "nba-tor-cle-2026-04-20-ou-221-5",
                "endDate": "2026-04-20T23:00:00Z",
                "outcomes": '["Over", "Under"]',
                "outcomePrices": '["0.51", "0.49"]',
                "clobTokenIds": '["111", "222"]',
                "active": True, "closed": False, "acceptingOrders": True,
                "volume": 50000.0, "liquidity": 30000.0,
            },
        ],
    }


def test_maybe_json_list_parses_string():
    assert _maybe_json_list('["a", "b"]') == ["a", "b"]
    assert _maybe_json_list(["a", "b"]) == ["a", "b"]
    assert _maybe_json_list(None) == []
    assert _maybe_json_list("not json") == []


def test_parse_isoformat_z_suffix():
    dt = _parse_isoformat("2026-04-20T23:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 4 and dt.day == 20
    assert dt.tzinfo is not None


def test_parse_isoformat_handles_none():
    assert _parse_isoformat(None) is None
    assert _parse_isoformat("") is None
    assert _parse_isoformat("bogus") is None


def test_flatten_event_returns_both_markets():
    event = _nba_event_live()
    flat = _flatten_event_to_markets(event)
    assert len(flat) == 2


def test_flatten_event_populates_canonical_schema():
    event = _nba_event_live()
    flat = _flatten_event_to_markets(event)
    m = flat[0]
    assert m["polymarket_id"].startswith("0x")
    assert m["question"] == "Raptors vs. Cavaliers"
    assert m["slug"] == "nba-tor-cle-2026-04-20"
    assert m["category"] == "sports"
    assert m["outcomes"] == ["Raptors", "Cavaliers"]
    assert m["yes_token_id"] == "151430577"
    assert m["no_token_id"] == "909876543"
    assert m["yes_price"] == pytest.approx(0.125)
    assert m["no_price"] == pytest.approx(0.875)
    assert m["resolution_time"] is not None
    assert m["book_depth"] == 191135.2
    assert m["volume_24h"] == 1546162.3


def test_flatten_event_skips_closed_markets():
    event = _nba_event_live()
    event["markets"][0]["closed"] = True
    flat = _flatten_event_to_markets(event)
    # Only the O/U market remains
    assert len(flat) == 1
    assert flat[0]["outcomes"] == ["Over", "Under"]


def test_flatten_event_skips_inactive_markets():
    event = _nba_event_live()
    event["markets"][0]["active"] = False
    flat = _flatten_event_to_markets(event)
    assert len(flat) == 1


def test_flatten_event_skips_not_accepting_orders():
    event = _nba_event_live()
    event["markets"][0]["acceptingOrders"] = False
    flat = _flatten_event_to_markets(event)
    assert len(flat) == 1


def test_flatten_event_handles_missing_markets_list():
    event = {"slug": "empty", "title": "Empty"}
    flat = _flatten_event_to_markets(event)
    assert flat == []


def test_flatten_event_skips_odd_outcome_count():
    event = _nba_event_live()
    # Three-way market (unusual) should be skipped
    event["markets"][0]["outcomes"] = '["A", "B", "C"]'
    event["markets"][0]["outcomePrices"] = '["0.3", "0.4", "0.3"]'
    event["markets"][0]["clobTokenIds"] = '["1", "2", "3"]'
    flat = _flatten_event_to_markets(event)
    # Only the O/U market passes
    assert len(flat) == 1


def test_flatten_event_skips_bad_price_parsing():
    event = _nba_event_live()
    event["markets"][0]["outcomePrices"] = '["notanumber", "0.5"]'
    flat = _flatten_event_to_markets(event)
    assert len(flat) == 1   # ou market still OK


@pytest.mark.asyncio
async def test_fetch_live_sports_events_flattens_and_logs():
    """End-to-end test on the async fetcher. Mocks _get to avoid network."""
    from polybot.markets.scanner import PolymarketScanner

    scanner = PolymarketScanner(api_key="test")

    captured_calls = []
    async def mock_get(path, params):
        captured_calls.append((path, dict(params)))
        tag = params.get("tag_slug")
        if tag == "nba":
            return (200, [_nba_event_live()])
        if tag in ("mlb", "nhl", "ncaab", "ucl", "epl", "la-liga", "bundesliga", "mls"):
            return (200, [])
        return (404, None)

    scanner._get = mock_get
    import aiohttp
    scanner._session = aiohttp.ClientSession()
    try:
        markets = await scanner.fetch_live_sports_events()
    finally:
        await scanner._session.close()

    assert len(markets) == 2   # 2 NBA markets
    # Soccer uses "la-liga" hyphenated tag slug
    assert any(c[1].get("tag_slug") == "la-liga" for c in captured_calls)
    assert any(c[1].get("tag_slug") == "nba" for c in captured_calls)


@pytest.mark.asyncio
async def test_fetch_live_sports_events_accepts_sports_filter():
    from polybot.markets.scanner import PolymarketScanner
    scanner = PolymarketScanner(api_key="test")
    captured = []
    async def mock_get(path, params):
        captured.append(params.get("tag_slug"))
        return (200, [])
    scanner._get = mock_get
    import aiohttp
    scanner._session = aiohttp.ClientSession()
    try:
        await scanner.fetch_live_sports_events(sports=["nba", "mlb"])
    finally:
        await scanner._session.close()
    assert set(captured) == {"nba", "mlb"}
