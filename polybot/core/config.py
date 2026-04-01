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
    alert_email: str = "logan@galenhealth.org"

    # CLOB L2 credentials
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_chain_id: int = 137

    # Dry-run mode
    dry_run: bool = True

    # Bot parameters
    starting_bankroll: float = 300.0
    kelly_mult: float = 0.25
    edge_threshold: float = 0.05
    scan_interval_seconds: int = 300

    # Strategy intervals
    arb_interval_seconds: int = 45
    snipe_interval_seconds: int = 120
    forecast_interval_seconds: int = 300

    # Strategy Kelly multipliers
    arb_kelly_mult: float = 0.80
    snipe_kelly_mult: float = 0.50
    forecast_kelly_mult: float = 0.25

    # Strategy position limits
    arb_max_single_pct: float = 0.40
    snipe_max_single_pct: float = 0.25
    forecast_max_single_pct: float = 0.15

    # Fee
    polymarket_fee_rate: float = 0.02

    # Snipe thresholds
    snipe_hours_max: float = 72.0
    snipe_min_confidence: float = 0.90
    snipe_min_net_edge: float = 0.02

    # Arb thresholds
    arb_min_net_edge: float = 0.01
    arb_fill_timeout_seconds: int = 30
    arb_max_net_edge: float = 0.20

    # Pre-scoring
    prescore_top_n: int = 5
    quick_screen_max_edge_gap: float = 0.03

    # Portfolio limits
    max_single_position_pct: float = 0.15
    max_total_deployed_pct: float = 0.70
    max_per_category_pct: float = 0.25
    min_trade_size: float = 1.0
    max_concurrent_positions: int = 12
    max_positions_per_market: int = 1
    daily_loss_limit_pct: float = 0.15
    circuit_breaker_hours: int = 6
    post_breaker_cooldown_hours: int = 24
    post_breaker_kelly_reduction: float = 0.50

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

    # Active position management
    take_profit_threshold: float = 0.20
    stop_loss_threshold: float = 0.25
    position_check_interval: int = 60

    # Snipe cooldown & re-entry
    snipe_cooldown_hours: float = 4.0
    snipe_reentry_threshold: float = 0.03
    snipe_max_entries_per_market: int = 3

    # Arb bankroll gate
    arb_min_bankroll: float = 2000.0
    arb_max_hold_days: float = 7.0

    # Forecast time-stop
    forecast_time_stop_minutes: float = 60.0

    # Learning system
    enable_proxy_trust_learning: bool = True
    proxy_brier_alpha_tp: float = 0.05
    proxy_brier_alpha_sl: float = 0.08
    proxy_brier_alpha_weak: float = 0.03
    enable_adaptive_thresholds: bool = True
    adaptive_threshold_min_trades: int = 10
    enable_snipe_learning: bool = True
    enable_hourly_learning: bool = True

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

    # Bankroll tiers
    bankroll_survival_threshold: float = 50.0
    bankroll_normal_low: float = 50.0
    bankroll_normal_high: float = 150.0
    bankroll_growth_threshold: float = 500.0

    # Learning
    cold_start_trades: int = 30
    brier_ema_alpha: float = 0.15
    category_min_trades: int = 20
    calibration_min_trades: int = 50
    strategy_kill_min_trades: int = 50

    # Monitoring
    health_check_interval: int = 60
    heartbeat_warn_seconds: int = 600
    heartbeat_critical_seconds: int = 1800
    balance_divergence_pct: float = 0.05

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
