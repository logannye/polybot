"""Tests for v12.1 verifier-confidence-tiered sizing."""
import pytest
from types import SimpleNamespace
from polybot.strategies.snipe import select_tier, SizingTier


def _settings(**overrides):
    base = dict(
        snipe_tier_high_min_conf=0.99, snipe_tier_high_min_edge=0.002,
        snipe_tier_high_max_pct=0.01,
        snipe_tier_mid_min_conf=0.97, snipe_tier_mid_min_edge=0.01,
        snipe_tier_mid_max_pct=0.02,
        snipe_tier_low_min_conf=0.95, snipe_tier_low_min_edge=0.02,
        snipe_tier_low_max_pct=0.05,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_high_tier_at_99_conf():
    t = select_tier(0.99, _settings())
    assert t.name == "high"
    assert t.min_edge == 0.002
    assert t.max_pct == 0.01


def test_high_tier_at_one():
    t = select_tier(1.0, _settings())
    assert t.name == "high"


def test_mid_tier_at_97_conf():
    t = select_tier(0.97, _settings())
    assert t.name == "mid"
    assert t.min_edge == 0.01
    assert t.max_pct == 0.02


def test_mid_tier_at_98_conf():
    t = select_tier(0.98, _settings())
    assert t.name == "mid"


def test_low_tier_at_95_conf():
    t = select_tier(0.95, _settings())
    assert t.name == "low"
    assert t.min_edge == 0.02
    assert t.max_pct == 0.05


def test_below_low_returns_none():
    assert select_tier(0.94, _settings()) is None
    assert select_tier(0.0, _settings()) is None


def test_tier_caps_decrease_with_confidence():
    """Sanity: higher confidence → smaller per-trade cap (so a wrong call
    on a thin edge can't blow up bankroll)."""
    s = _settings()
    high = select_tier(0.99, s)
    mid = select_tier(0.98, s)
    low = select_tier(0.96, s)
    assert high.max_pct < mid.max_pct < low.max_pct


def test_tier_edge_floors_decrease_with_confidence():
    """Higher confidence → thinner edge OK (we trust the verifier more)."""
    s = _settings()
    high = select_tier(0.99, s)
    mid = select_tier(0.98, s)
    low = select_tier(0.96, s)
    assert high.min_edge < mid.min_edge < low.min_edge


def test_custom_tier_thresholds_respected():
    s = _settings(snipe_tier_high_min_conf=0.999,
                  snipe_tier_high_max_pct=0.005)
    assert select_tier(0.99, s).name == "mid"     # no longer high
    assert select_tier(0.999, s).max_pct == 0.005


def test_high_tier_thin_edge_economics():
    """At 0.998 buy_price, edge=0.002 → high tier passes its own floor.
    This is the case v12.1 was designed to unlock."""
    t = select_tier(1.0, _settings())
    assert t is not None
    edge = 1.0 - 0.998     # 0.002
    assert edge >= t.min_edge      # passes high-tier floor
    # Worst-case loss: 0.01 (cap) × 0.998 (price) ≈ 0.998% of bankroll
    worst_case = t.max_pct * 0.998
    assert worst_case < 0.01    # < 1% bankroll per failed trade
