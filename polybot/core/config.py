from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Secrets
    polymarket_api_key: str
    polymarket_private_key: str
    google_api_key: str                 # Gemini Flash for Snipe T1 verification (v10 PR C)
    database_url: str
    resend_api_key: str
    alert_email: str = "logan@galenhealth.org"

    # CLOB L2 credentials
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_chain_id: int = 137

    # Dry-run mode
    dry_run: bool = True
    dry_run_realistic: bool = True           # use real order books for dry-run pricing
    dry_run_taker_fee_pct: float = 0.02      # simulated taker fee (2%)
    dry_run_max_spread: float = 0.15         # reject dry-run orders on markets with > 15% spread

    # Bot parameters
    starting_bankroll: float = 2000.0
    kelly_mult: float = 0.25
    edge_threshold: float = 0.05
    scan_interval_seconds: int = 300

    # Fee model: makers pay 0%, takers pay category-specific rates
    use_maker_orders: bool = True   # post_only flag → guaranteed 0% maker fee
    fee_rate_default: float = 0.04  # fallback taker rate for unknown categories

    # Portfolio limits
    max_single_position_pct: float = 0.15
    max_total_deployed_pct: float = 0.70
    max_per_category_pct: float = 0.50
    min_trade_size: float = 1.0
    max_concurrent_positions: int = 12
    max_positions_per_market: int = 1
    daily_loss_limit_pct: float = 0.20
    circuit_breaker_hours: int = 6
    post_breaker_cooldown_hours: int = 24
    post_breaker_kelly_reduction: float = 0.50

    # Total drawdown + divergence protection + deployment stage (v10 safeguards)
    max_total_drawdown_pct: float = 0.30     # halt all trading at 30% total loss from high-water
    max_capital_divergence_pct: float = 0.10  # halt if CLOB vs DB diverges > 10%
    live_deployment_stage: str = "dry_run"    # dry_run → micro_test → ramp → full

    # Market filters
    resolution_hours_max: int = 168
    min_book_depth: float = 500.0
    min_price: float = 0.05
    max_price: float = 0.95
    cooldown_minutes: int = 30
    price_move_threshold: float = 0.03

    # Position management
    early_exit_edge: float = 0.02
    fill_timeout_seconds: int = 120
    book_depth_max_pct: float = 0.10
    take_profit_threshold: float = 0.20
    stop_loss_threshold: float = 0.15
    position_check_interval: int = 60
    universal_max_hold_hours: float = 12.0

    # Bankroll tiers (snipe uses direct attribute access; v10 PR C will move
    # this logic into the strategy itself or delete it with simplified Kelly)
    bankroll_survival_threshold: float = 50.0
    bankroll_growth_threshold: float = 500.0

    # Transitional keys still referenced by engine/position_manager via
    # getattr with defaults. These will be cleaned up in PR B/C.
    arb_fill_timeout_seconds: int = 30
    arb_max_hold_days: float = 3.0

    # Snipe strategy (to be rewritten to 2-tier in PR C — keys retained for
    # transitional v8 snipe that ships in Phase A)
    # Snipe v10 — 2-tier resolution-convergence (spec §4)
    snipe_enabled: bool = True
    snipe_interval_seconds: int = 120
    snipe_max_concurrent: int = 3
    snipe_min_net_edge: float = 0.02
    snipe_min_book_depth: float = 2000.0
    snipe_gemini_daily_cap_usd: float = 2.0
    snipe_t0_kelly_mult: float = 0.50    # T0: 0.50× Kelly
    snipe_t0_max_single_pct: float = 0.10
    snipe_t1_kelly_mult: float = 0.30    # T1: 0.30× Kelly (needs LLM verify)
    snipe_t1_max_single_pct: float = 0.07
    snipe_t1_min_confidence: float = 0.85

    # Live Sports v10 engine (spec §3)
    lg_enabled: bool = True
    lg_interval_seconds: float = 15.0          # spec: 15s ESPN polling cadence
    lg_kelly_mult: float = 0.50                # half-Kelly
    lg_max_single_pct: float = 0.20            # spec: max 20% per market (was 25%)
    lg_min_edge: float = 0.04                  # min 4% edge vs Polymarket price
    lg_min_win_prob: float = 0.85              # LIVE only: trade when calibrated WP ≥ this. Hardcoded floor = 0.80 (cannot be bypassed by config).
    lg_min_win_prob_dryrun: float = 0.65       # DRY-RUN only: looser gate for data collection. Floor = 0.55.
    lg_min_book_depth: float = 10000.0         # min $10K liquidity at entry (live)
    lg_min_book_depth_dryrun: float = 1000.0   # DRY-RUN only: looser gate for flow observation. Floor = $500.
    lg_max_concurrent: int = 6                 # max concurrent live_sports positions
    lg_sports: str = "mlb,nba,nhl,ncaab,ucl,epl,laliga,bundesliga,mls"
    lg_max_staleness_s: float = 60.0           # reject data older than 60s
    lg_matcher_min_confidence: float = 0.95    # 3-pass matcher confidence floor
    lg_take_profit_price: float = 0.97         # exit when price hits this
    lg_emergency_exit_wp: float = 0.70         # exit if calibrated WP drops below
    lg_max_hold_hours: float = 6.0             # hard time stop

    # Spread-market trading (higher variance than moneyline; conservative defaults)
    lg_spread_min_edge: float = 0.06           # higher edge bar than moneyline's 0.04
    lg_spread_kelly_reduction: float = 0.50    # multiply base Kelly by this for spread trades

    # Totals (O/U) market trading — lower per-period variance than spreads but
    # set by the same retail flow that misprices spreads, so still half-Kelly.
    lg_total_min_edge: float = 0.05            # between moneyline's 0.04 and spread's 0.06
    lg_total_kelly_reduction: float = 0.50     # multiply base Kelly by this for total trades

    # Online calibrator (spec §5 Loop 2)
    sports_calibrator_min_obs: int = 30
    sports_calibrator_fallback_shrinkage: float = 0.10

    # Learning (TradeLearner still in use; replaced by v10 learning layer in PR C)
    enable_hourly_learning: bool = True
    enable_adaptive_thresholds: bool = True
    adaptive_threshold_min_trades: int = 10
    enable_snipe_learning: bool = True
    enable_proxy_trust_learning: bool = True
    proxy_brier_alpha_tp: float = 0.05
    proxy_brier_alpha_sl: float = 0.08
    proxy_brier_alpha_weak: float = 0.03
    cold_start_trades: int = 30
    brier_ema_alpha: float = 0.15
    category_min_trades: int = 20
    calibration_min_trades: int = 50
    strategy_kill_min_trades: int = 50

    # WebSocket streaming
    enable_websocket_streaming: bool = True
    ws_reconnect_max_delay: float = 30.0

    # Monitoring
    health_check_interval: int = 60
    heartbeat_warn_seconds: int = 600
    heartbeat_critical_seconds: int = 1800
    balance_divergence_pct: float = 0.05

    # "extra=ignore" so stale .env keys from deleted v10 strategies (forecast_*,
    # mm_*, mr_*, cv_*, pol_*, arb_*) don't crash startup. .env cleanup is a
    # separate follow-up.
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
