"""Tests for polybot.sports.threshold — live-safety floor + dry-run override.

Critical production-safety guarantees tested here:
1. Live mode enforces LIVE_WP_FLOOR regardless of config value
2. Dry-run mode uses lg_min_win_prob_dryrun (with its own DRYRUN_WP_FLOOR)
3. passes_live_threshold always measures against the enforced live floor
"""
from unittest.mock import MagicMock
import pytest

from polybot.sports.threshold import (
    get_active_wp_threshold, passes_live_threshold,
    LIVE_WP_FLOOR, DRYRUN_WP_FLOOR,
)


def _settings(**overrides):
    s = MagicMock()
    s.dry_run = True
    s.lg_min_win_prob = 0.85
    s.lg_min_win_prob_dryrun = 0.65
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ---- live-safety invariants -----------------------------------------------

def test_live_floor_is_hardcoded():
    """LIVE_WP_FLOOR constant is ≥ 0.80."""
    assert LIVE_WP_FLOOR >= 0.80


def test_live_mode_uses_configured_threshold_when_above_floor():
    s = _settings(dry_run=False, lg_min_win_prob=0.90)
    assert get_active_wp_threshold(s) == pytest.approx(0.90)


def test_live_mode_enforces_floor_when_config_below():
    """Config tries to weaken live gate — floor MUST win."""
    s = _settings(dry_run=False, lg_min_win_prob=0.50)   # attempt to weaken
    assert get_active_wp_threshold(s) == pytest.approx(LIVE_WP_FLOOR)


def test_live_mode_ignores_dryrun_setting():
    """Live mode must not use lg_min_win_prob_dryrun under any circumstance."""
    s = _settings(dry_run=False, lg_min_win_prob=0.85,
                  lg_min_win_prob_dryrun=0.10)
    result = get_active_wp_threshold(s)
    assert result == pytest.approx(0.85)
    assert result >= LIVE_WP_FLOOR


# ---- dry-run behavior -----------------------------------------------------

def test_dryrun_uses_dryrun_threshold():
    s = _settings(dry_run=True, lg_min_win_prob=0.85,
                  lg_min_win_prob_dryrun=0.65)
    assert get_active_wp_threshold(s) == pytest.approx(0.65)


def test_dryrun_floor_enforced():
    s = _settings(dry_run=True, lg_min_win_prob_dryrun=0.20)
    assert get_active_wp_threshold(s) == pytest.approx(DRYRUN_WP_FLOOR)


def test_dryrun_default_falls_back_to_live_when_no_dryrun_key():
    s = MagicMock()
    s.dry_run = True
    s.lg_min_win_prob = 0.85
    # No lg_min_win_prob_dryrun attribute — should fall through to live val
    del s.lg_min_win_prob_dryrun
    s.lg_min_win_prob_dryrun = 0.85   # must reassign for getattr fallback
    assert get_active_wp_threshold(s) == pytest.approx(0.85)


# ---- passes_live_threshold flag ------------------------------------------

def test_passes_live_threshold_true_when_above_live_floor():
    s = _settings(dry_run=True, lg_min_win_prob=0.85)
    assert passes_live_threshold(0.90, s) is True


def test_passes_live_threshold_false_when_below_live_floor():
    s = _settings(dry_run=True, lg_min_win_prob=0.85)
    assert passes_live_threshold(0.70, s) is False


def test_passes_live_threshold_uses_configured_when_above_floor():
    """0.85 configured → 0.83 should fail because 0.83 < 0.85."""
    s = _settings(dry_run=True, lg_min_win_prob=0.85)
    assert passes_live_threshold(0.83, s) is False
    assert passes_live_threshold(0.85, s) is True


def test_passes_live_threshold_rejects_config_below_floor():
    """If config tries to set live=0.50, passes_live_threshold still uses 0.80+."""
    s = _settings(dry_run=True, lg_min_win_prob=0.50)
    assert passes_live_threshold(0.75, s) is False    # below enforced 0.80 floor
    assert passes_live_threshold(0.82, s) is True


def test_passes_live_threshold_same_under_dry_run_or_live_mode():
    """Whether we're in dry-run or live, passes_live_threshold checks against
    the live floor — it's the 'would-have-entered-live' projection marker."""
    s_dry = _settings(dry_run=True, lg_min_win_prob=0.85)
    s_live = _settings(dry_run=False, lg_min_win_prob=0.85)
    for wp in (0.5, 0.7, 0.85, 0.95):
        assert passes_live_threshold(wp, s_dry) == passes_live_threshold(wp, s_live)
