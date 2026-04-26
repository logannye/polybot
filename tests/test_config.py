"""v12 config smoke tests — verify Settings loads with v12 keys."""
import pytest
from polybot.core.config import Settings


def _base_env() -> dict[str, str]:
    return {
        "POLYMARKET_API_KEY": "test",
        "POLYMARKET_PRIVATE_KEY": "0x" + "a" * 64,
        "GOOGLE_API_KEY": "test",
        "DATABASE_URL": "postgresql://localhost/test",
        "RESEND_API_KEY": "test",
        "ALERT_EMAIL": "test@test.com",
    }


def test_settings_loads_defaults(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.starting_bankroll == 2000.0
    assert s.dry_run is True
    assert s.snipe_enabled is True
    # v12.2: dropped from 0.96 to 0.92
    assert s.snipe_min_price == 0.92
    assert s.snipe_kelly_mult == 0.25
    assert s.snipe_min_verifier_confidence == 0.95
    assert s.killswitch_window == 50
    assert s.killswitch_min_hit_rate == 0.97
    assert s.killswitch_min_n == 50
    assert s.live_deployment_stage == "dry_run"
    # v12.2 maker-fill simulation defaults on
    assert s.dry_run_assume_maker_fill is True
    assert s.snipe_skip_spread_gate is True
    # v12.2 retuned tiers
    assert s.snipe_tier_high_min_edge == 0.02
    assert s.snipe_tier_high_max_pct == 0.005
    assert s.snipe_tier_mid_min_edge == 0.04
    assert s.snipe_tier_mid_max_pct == 0.01
    assert s.snipe_tier_low_min_edge == 0.06
    assert s.snipe_tier_low_max_pct == 0.02


def test_settings_ignores_legacy_env_keys(monkeypatch):
    """Stale keys from deleted v10/v11 strategies must not crash startup."""
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FORECAST_YES_MAX_ENTRY", "0.15")
    monkeypatch.setenv("LG_ENABLED", "true")
    monkeypatch.setenv("PG_ENABLED", "true")
    monkeypatch.setenv("MR_MIN_ENTRY_PRICE", "0.25")
    s = Settings(_env_file=None)
    assert s is not None


def test_settings_dry_run_overrides(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SNIPE_MAX_HOURS_DRYRUN", "240")
    s = Settings(_env_file=None)
    assert s.snipe_max_hours_dryrun == 240.0
