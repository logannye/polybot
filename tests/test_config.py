import os
import pytest
from polybot.core.config import Settings


def test_settings_loads_defaults():
    settings = Settings(
        polymarket_api_key="test",
        polymarket_private_key="0x" + "ab" * 32,
        anthropic_api_key="test",
        openai_api_key="test",
        google_api_key="test",
        brave_api_key="test",
        database_url="postgresql://localhost/test",
        resend_api_key="test",
        twilio_account_sid="test",
        twilio_auth_token="test",
        twilio_from_number="+10000000000",
        alert_email="test@test.com",
        alert_phone="+10000000000",
    )
    assert settings.starting_bankroll == 300.0
    assert settings.kelly_mult == 0.25
    assert settings.edge_threshold == 0.05
    assert settings.scan_interval_seconds == 300
    assert settings.max_single_position_pct == 0.15
    assert settings.max_total_deployed_pct == 0.50
    assert settings.max_per_category_pct == 0.25
    assert settings.min_trade_size == 2.0
    assert settings.max_concurrent_positions == 8
    assert settings.daily_loss_limit_pct == 0.20
    assert settings.circuit_breaker_hours == 12
    assert settings.resolution_hours_max == 72
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
    assert settings.brier_ema_alpha == 0.1
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
        twilio_account_sid="test",
        twilio_auth_token="test",
        twilio_from_number="+10000000000",
        alert_email="test@test.com",
        alert_phone="+10000000000",
        starting_bankroll=500.0,
        kelly_mult=0.30,
        edge_threshold=0.08,
    )
    assert settings.starting_bankroll == 500.0
    assert settings.kelly_mult == 0.30
    assert settings.edge_threshold == 0.08
