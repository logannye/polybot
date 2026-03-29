import pytest
from polybot.strategies.snipe import classify_snipe_tier, compute_snipe_edge


def test_tier0_high_price():
    assert classify_snipe_tier(price=0.95, hours_remaining=3.0, max_hours=6.0) == 0


def test_tier1_medium_price():
    assert classify_snipe_tier(price=0.85, hours_remaining=2.0, max_hours=6.0) == 1


def test_no_snipe_low_price():
    assert classify_snipe_tier(price=0.70, hours_remaining=2.0, max_hours=6.0) is None


def test_no_snipe_too_far():
    assert classify_snipe_tier(price=0.95, hours_remaining=10.0, max_hours=6.0) is None


def test_tier0_no_side():
    assert classify_snipe_tier(price=0.05, hours_remaining=2.0, max_hours=6.0) == 0


def test_tier1_no_side():
    assert classify_snipe_tier(price=0.15, hours_remaining=2.0, max_hours=6.0) == 1


def test_snipe_edge_yes():
    edge = compute_snipe_edge(buy_price=0.95, fee_rate=0.02)
    assert abs(edge - 0.03) < 1e-9


def test_snipe_edge_negative():
    edge = compute_snipe_edge(buy_price=0.99, fee_rate=0.02)
    assert edge < 0
