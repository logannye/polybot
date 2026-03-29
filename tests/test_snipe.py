import pytest
from polybot.strategies.snipe import classify_snipe_tier, compute_snipe_edge


# --- Tier 0: Very extreme prices, close to resolution (<=24h) ---

def test_tier0_high_price():
    assert classify_snipe_tier(price=0.95, hours_remaining=3.0) == 0


def test_tier0_high_price_24h():
    assert classify_snipe_tier(price=0.96, hours_remaining=23.0) == 0


def test_tier0_no_side():
    assert classify_snipe_tier(price=0.05, hours_remaining=2.0) == 0


def test_tier0_no_side_boundary():
    assert classify_snipe_tier(price=0.04, hours_remaining=20.0) == 0


# --- Tier 1: Extreme prices, moderate time (<=12h) ---

def test_tier1_medium_price():
    assert classify_snipe_tier(price=0.85, hours_remaining=2.0) == 1


def test_tier1_no_side():
    assert classify_snipe_tier(price=0.15, hours_remaining=10.0) == 1


def test_tier1_boundary():
    """0.85 at 12h should be tier 1."""
    assert classify_snipe_tier(price=0.85, hours_remaining=12.0) == 1


# --- Tier 2: Strong lean, wider window (<=48h) ---

def test_tier2_high_price():
    assert classify_snipe_tier(price=0.90, hours_remaining=36.0) == 2


def test_tier2_low_price():
    assert classify_snipe_tier(price=0.10, hours_remaining=40.0) == 2


def test_tier2_boundary():
    assert classify_snipe_tier(price=0.92, hours_remaining=47.0) == 2


# --- Not a snipe candidate ---

def test_no_snipe_low_price():
    assert classify_snipe_tier(price=0.70, hours_remaining=2.0) is None


def test_no_snipe_too_far():
    assert classify_snipe_tier(price=0.95, hours_remaining=50.0) is None


def test_no_snipe_moderate_price_far():
    """0.85 at 30h: beyond tier 1 window (12h), below tier 2 threshold (0.90)."""
    assert classify_snipe_tier(price=0.85, hours_remaining=30.0) is None


def test_no_snipe_zero_hours():
    assert classify_snipe_tier(price=0.95, hours_remaining=0) is None


def test_no_snipe_negative_hours():
    assert classify_snipe_tier(price=0.95, hours_remaining=-1.0) is None


def test_snipe_edge_yes():
    edge = compute_snipe_edge(buy_price=0.95, fee_rate=0.02)
    assert abs(edge - 0.03) < 1e-9


def test_snipe_edge_negative():
    edge = compute_snipe_edge(buy_price=0.99, fee_rate=0.02)
    assert edge < 0
