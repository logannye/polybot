from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Secrets
    polymarket_api_key: str
    polymarket_private_key: str
    anthropic_api_key: str
    openai_api_key: str
    google_api_key: str
    brave_api_key: str
    database_url: str
    resend_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    alert_email: str
    alert_phone: str

    # Bot parameters
    starting_bankroll: float = 300.0
    kelly_mult: float = 0.25
    edge_threshold: float = 0.05
    scan_interval_seconds: int = 300

    # Portfolio limits
    max_single_position_pct: float = 0.15
    max_total_deployed_pct: float = 0.50
    max_per_category_pct: float = 0.25
    min_trade_size: float = 2.0
    max_concurrent_positions: int = 8
    daily_loss_limit_pct: float = 0.20
    circuit_breaker_hours: int = 12

    # Market filters
    resolution_hours_max: int = 72
    min_book_depth: float = 500.0
    min_price: float = 0.05
    max_price: float = 0.95
    cooldown_minutes: int = 30
    price_move_threshold: float = 0.03

    # Position management
    early_exit_edge: float = 0.02
    fill_timeout_seconds: int = 120
    book_depth_max_pct: float = 0.10

    # Quant signal weights
    quant_weights: dict[str, float] = {
        "line_movement": 0.30,
        "volume_spike": 0.25,
        "book_imbalance": 0.20,
        "spread": 0.15,
        "time_decay": 0.10,
    }

    # Ensemble confidence thresholds
    ensemble_stdev_low: float = 0.05
    ensemble_stdev_high: float = 0.12
    confidence_mult_low: float = 1.0
    confidence_mult_mid: float = 0.7
    confidence_mult_high: float = 0.4
    quant_negative_mult: float = 0.75

    # Learning
    cold_start_trades: int = 30
    brier_ema_alpha: float = 0.1
    category_min_trades: int = 20

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
