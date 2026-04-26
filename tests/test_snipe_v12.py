"""Tests for the v12 snipe strategy: classify_snipe + verifier integration."""
import pytest
from polybot.strategies.snipe import (
    classify_snipe, compute_net_edge, SnipeCandidate,
)


# ── classify_snipe — single tier, mirror NO ─────────────────────────────
def test_classify_yes_at_threshold_passes():
    c = classify_snipe(yes_price=0.97, hours_remaining=4.0)
    assert c is not None
    assert c.side == "YES"
    assert c.buy_price == pytest.approx(0.97)


def test_classify_no_mirror_at_low_yes():
    c = classify_snipe(yes_price=0.03, hours_remaining=4.0)
    assert c is not None
    assert c.side == "NO"
    assert c.buy_price == pytest.approx(0.97)


def test_classify_below_min_price_rejected():
    assert classify_snipe(yes_price=0.94, hours_remaining=4.0) is None
    assert classify_snipe(yes_price=0.06, hours_remaining=4.0) is None


def test_classify_past_resolution_rejected():
    assert classify_snipe(yes_price=0.99, hours_remaining=0.0) is None
    assert classify_snipe(yes_price=0.99, hours_remaining=-1.0) is None


def test_classify_too_far_out_rejected():
    assert classify_snipe(yes_price=0.99, hours_remaining=24.0,
                          max_hours=12.0) is None


def test_classify_dry_run_extended_window_passes():
    c = classify_snipe(yes_price=0.99, hours_remaining=72.0, max_hours=168.0)
    assert c is not None
    assert c.hours_remaining == 72.0


def test_classify_min_price_param_respected():
    # Stricter cutoff
    assert classify_snipe(yes_price=0.96, hours_remaining=4.0,
                          min_price=0.98) is None
    assert classify_snipe(yes_price=0.98, hours_remaining=4.0,
                          min_price=0.98) is not None


# ── compute_net_edge ────────────────────────────────────────────────────
def test_compute_net_edge_basic():
    assert compute_net_edge(0.97) == pytest.approx(0.03)


def test_compute_net_edge_with_fee():
    assert compute_net_edge(0.97, fee_per_dollar=0.01) == pytest.approx(0.02)


def test_compute_net_edge_at_one_is_zero():
    assert compute_net_edge(1.0) == pytest.approx(0.0)


# ── T0 deletion: confirm classify never returns a candidate at no-LLM tier ──
def test_no_t0_path_exists():
    """v12 has no T0; every candidate must go through verifier."""
    c = classify_snipe(yes_price=0.99, hours_remaining=2.0)
    assert c is not None
    # SnipeCandidate has no `tier` field in v12 (single tier only).
    assert not hasattr(c, "tier")
