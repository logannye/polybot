import pytest
from polybot.strategies.arbitrage import detect_complement_arb, detect_exhaustive_arb, detect_temporal_arb


def test_complement_arb_detected():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.40, fee_rate=0.02)
    assert result is not None
    assert abs(result.gross_edge - 0.05) < 1e-4
    assert result.net_edge > 0

def test_complement_arb_none_when_sum_is_one():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.55, no_price=0.45, fee_rate=0.02)
    assert result is None

def test_complement_arb_none_when_fee_eats_edge():
    result = detect_complement_arb(polymarket_id="test", yes_price=0.505, no_price=0.49, fee_rate=0.02)
    assert result is None

def test_exhaustive_arb_overpriced():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.45, "no_price": 0.60},
        {"polymarket_id": "b", "yes_price": 0.40, "no_price": 0.65},
        {"polymarket_id": "c", "yes_price": 0.25, "no_price": 0.78},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is not None
    assert result.side == "NO"
    assert result.gross_edge > 0.09

def test_exhaustive_arb_underpriced():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.30, "no_price": 0.72},
        {"polymarket_id": "b", "yes_price": 0.25, "no_price": 0.77},
        {"polymarket_id": "c", "yes_price": 0.20, "no_price": 0.82},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is not None
    assert result.side == "YES"

def test_exhaustive_arb_none_when_fair():
    markets = [
        {"polymarket_id": "a", "yes_price": 0.50, "no_price": 0.51},
        {"polymarket_id": "b", "yes_price": 0.50, "no_price": 0.51},
    ]
    result = detect_exhaustive_arb(markets, fee_rate=0.02, min_net_edge=0.01)
    assert result is None

def test_temporal_arb_detected():
    assert detect_temporal_arb("by June?", 0.50, "by July?", 0.40) is True

def test_temporal_arb_none_when_correct():
    assert detect_temporal_arb("by June?", 0.40, "by July?", 0.50) is False
