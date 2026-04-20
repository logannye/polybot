import os
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
    monkeypatch.delenv("STARTING_BANKROLL", raising=False)
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    settings = Settings(_env_file=None)
    assert settings.starting_bankroll == 2000.0
    assert settings.kelly_mult == 0.25
    assert settings.edge_threshold == 0.05
    assert settings.scan_interval_seconds == 300
    assert settings.max_single_position_pct == 0.15
    assert settings.max_total_deployed_pct == 0.70
    assert settings.max_per_category_pct == 0.50
    assert settings.min_trade_size == 1.0
    assert settings.max_concurrent_positions == 12
    assert settings.daily_loss_limit_pct == 0.20
    assert settings.circuit_breaker_hours == 6
    assert settings.resolution_hours_max == 168
    assert settings.min_book_depth == 500.0
    assert settings.min_price == 0.05
    assert settings.max_price == 0.95
    assert settings.cooldown_minutes == 30
    assert settings.price_move_threshold == 0.03
    assert settings.early_exit_edge == 0.02
    assert settings.fill_timeout_seconds == 120
    assert settings.book_depth_max_pct == 0.10
    assert settings.cold_start_trades == 30
    assert settings.brier_ema_alpha == 0.15
    assert settings.category_min_trades == 20


def test_settings_overrides(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    settings = Settings(
        starting_bankroll=500.0,
        kelly_mult=0.30,
        edge_threshold=0.08,
        _env_file=None,
    )
    assert settings.starting_bankroll == 500.0
    assert settings.kelly_mult == 0.30
    assert settings.edge_threshold == 0.08


def test_v10_safeguard_defaults(monkeypatch):
    """v10 safeguard settings have correct defaults per spec §6."""
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.max_total_drawdown_pct == 0.30
    assert s.max_capital_divergence_pct == 0.10
    assert s.live_deployment_stage == "dry_run"
    assert s.post_breaker_cooldown_hours == 24
    assert s.post_breaker_kelly_reduction == 0.50


def test_v10_snipe_defaults(monkeypatch):
    """Snipe settings preserved across v10 Phase A (rewritten in PR C)."""
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.snipe_kelly_mult == 0.50
    assert s.snipe_max_single_pct == 0.05
    assert s.snipe_max_concurrent == 3
    assert s.snipe_hours_max == 72.0
    assert s.snipe_min_confidence == 0.90
    # Odds verification disabled after v10 Phase A (odds_client deleted)
    assert s.snipe_odds_verification_enabled is False


def test_v10_live_game_defaults(monkeypatch):
    """Live Game Closer settings (evolves to Live Sports v10 in PR B)."""
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.lg_enabled is True
    assert s.lg_kelly_mult == 0.50
    assert s.lg_min_edge == 0.04
    assert s.lg_min_win_prob == 0.85
    assert s.lg_min_book_depth == 10000.0
    assert s.lg_max_concurrent == 6


def test_v10_deleted_strategies_absent(monkeypatch):
    """Strategies deleted in v10 Phase A should not have config keys."""
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    for key in (
        # Forecast
        "forecast_enabled", "forecast_kelly_mult", "forecast_max_single_pct",
        "forecast_interval_seconds", "forecast_yes_max_entry", "forecast_max_spread",
        # Market Maker
        "mm_enabled", "mm_kelly_mult", "mm_max_single_pct",
        # Mean Reversion
        "mr_enabled", "mr_trigger_threshold", "mr_kelly_mult",
        # Cross Venue
        "cv_enabled", "cv_kelly_mult", "odds_api_key",
        # Political
        "pol_enabled", "pol_kelly_mult",
        # Arbitrage (the strategy — arb_fill_timeout_seconds + arb_max_hold_days
        # retained as transitional engine keys per Phase A plan)
        "arb_enabled", "arb_kelly_mult", "arb_max_single_pct",
    ):
        assert not hasattr(s, key), f"Deleted strategy key still present: {key}"
