import os
import pytest
from polybot.core.config import Settings


def test_settings_loads_defaults(monkeypatch):
    monkeypatch.delenv("STARTING_BANKROLL", raising=False)
    settings = Settings(
        polymarket_api_key="test",
        polymarket_private_key="0x" + "ab" * 32,
        anthropic_api_key="test",
        openai_api_key="test",
        google_api_key="test",
        brave_api_key="test",
        database_url="postgresql://localhost/test",
        resend_api_key="test",
        alert_email="test@test.com",
        _env_file=None,
    )
    assert settings.starting_bankroll == 300.0
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
    assert settings.quant_weights == {
        "line_movement": 0.30,
        "volume_spike": 0.25,
        "book_imbalance": 0.20,
        "spread": 0.15,
        "time_decay": 0.10,
    }
    assert settings.ensemble_stdev_low == 0.05
    assert settings.ensemble_stdev_high == 0.12
    assert settings.confidence_mult_low == 1.0
    assert settings.confidence_mult_mid == 0.7
    assert settings.confidence_mult_high == 0.4
    assert settings.quant_negative_mult == 0.75
    assert settings.cold_start_trades == 30
    assert settings.brier_ema_alpha == 0.15
    assert settings.category_min_trades == 20


def test_settings_overrides():
    settings = Settings(
        polymarket_api_key="test",
        polymarket_private_key="0x" + "ab" * 32,
        anthropic_api_key="test",
        openai_api_key="test",
        google_api_key="test",
        brave_api_key="test",
        database_url="postgresql://localhost/test",
        resend_api_key="test",
        alert_email="test@test.com",
        starting_bankroll=500.0,
        kelly_mult=0.30,
        edge_threshold=0.08,
    )
    assert settings.starting_bankroll == 500.0
    assert settings.kelly_mult == 0.30
    assert settings.edge_threshold == 0.08


def test_v2_strategy_settings_defaults():
    """Verify all v2 settings have correct defaults."""
    required = {
        "POLYMARKET_API_KEY": "test",
        "POLYMARKET_PRIVATE_KEY": "0x" + "a" * 64,
        "ANTHROPIC_API_KEY": "test",
        "OPENAI_API_KEY": "test",
        "GOOGLE_API_KEY": "test",
        "BRAVE_API_KEY": "test",
        "DATABASE_URL": "postgresql://localhost/test",
        "RESEND_API_KEY": "test",
        "ALERT_EMAIL": "test@test.com",
    }
    for k, v in required.items():
        os.environ[k] = v
    s = Settings()
    assert s.arb_interval_seconds == 45
    assert s.snipe_interval_seconds == 60   # v5 10x: 120 → 60
    assert s.forecast_interval_seconds == 300  # v4 conservative: 180 → 300
    assert s.arb_kelly_mult == 0.80
    assert s.snipe_kelly_mult == 0.50     # v4 conservative: 0.65 → 0.50
    assert s.forecast_kelly_mult == 0.15   # lean-into-winners: 0.20 → 0.15
    assert s.arb_max_single_pct == 0.40
    assert s.snipe_max_single_pct == 0.05  # capital realloc: 0.25 → 0.05 (redirect to MR)
    assert s.forecast_max_single_pct == 0.05  # lean-into-winners: 0.15 → 0.05
    assert s.use_maker_orders is True
    assert s.max_total_deployed_pct == 0.70  # v4 conservative: 0.90 → 0.70
    assert s.max_concurrent_positions == 12  # v4 conservative: 20 → 12
    assert s.max_per_category_pct == 0.50    # capital turnover: 0.25 → 0.50
    assert s.daily_loss_limit_pct == 0.20
    assert s.circuit_breaker_hours == 6
    assert s.min_trade_size == 1.0
    # v5 10x: new strategies
    assert s.mm_enabled is False  # disabled — no real edge at $500 scale
    assert s.mr_enabled is True   # lean-into-winners: re-enabled with mid-range filter
    assert s.snipe_cooldown_hours == 0.5     # lean-into-winners: 1.0 → 0.5
    assert s.snipe_max_entries_per_market == 6  # lean-into-winners: 4 → 6
    assert s.snipe_max_market_exposure_pct == 0.30
    assert not hasattr(s, "twilio_account_sid")
    assert not hasattr(s, "alert_phone")


def test_v2_bankroll_tier_settings():
    """Verify bankroll tier settings have correct defaults."""
    required = {
        "POLYMARKET_API_KEY": "test",
        "POLYMARKET_PRIVATE_KEY": "0x" + "a" * 64,
        "ANTHROPIC_API_KEY": "test",
        "OPENAI_API_KEY": "test",
        "GOOGLE_API_KEY": "test",
        "BRAVE_API_KEY": "test",
        "DATABASE_URL": "postgresql://localhost/test",
        "RESEND_API_KEY": "test",
        "ALERT_EMAIL": "test@test.com",
    }
    for k, v in required.items():
        os.environ[k] = v
    s = Settings()
    assert s.bankroll_survival_threshold == 50.0
    assert s.bankroll_growth_threshold == 1000.0  # raised to stay aggressive during compounding
    assert s.post_breaker_cooldown_hours == 24
    assert s.post_breaker_kelly_reduction == 0.50
