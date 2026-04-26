"""Polybot v12 settings — snipe-only."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Secrets ────────────────────────────────────────────────────────
    polymarket_api_key: str
    polymarket_private_key: str
    google_api_key: str
    database_url: str
    resend_api_key: str
    alert_email: str = "logan@galenhealth.org"

    # CLOB L2 credentials
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_chain_id: int = 137

    # ── Mode ──────────────────────────────────────────────────────────
    dry_run: bool = True
    dry_run_realistic: bool = True
    dry_run_taker_fee_pct: float = 0.02
    dry_run_max_spread: float = 0.15

    # ── Bankroll & deployment ─────────────────────────────────────────
    starting_bankroll: float = 2000.0
    min_trade_size: float = 1.0
    max_total_deployed_pct: float = 0.20      # snipe-wide deployed cap
    max_total_drawdown_pct: float = 0.30
    max_capital_divergence_pct: float = 0.10
    live_deployment_stage: str = "dry_run"    # dry_run → micro_test → ramp → full

    # ── Snipe strategy (the only strategy) ────────────────────────────
    snipe_enabled: bool = True
    snipe_interval_seconds: int = 60
    snipe_max_concurrent: int = 4
    snipe_max_total_deployed_pct: float = 0.20

    # Entry gates
    snipe_min_price: float = 0.96             # buy threshold (mirrors ≤0.04 to NO)
    snipe_max_hours: float = 12.0             # live ceiling
    snipe_max_hours_dryrun: float = 168.0     # 7d for observation
    snipe_min_net_edge: float = 0.02
    snipe_min_book_depth: float = 1000.0
    snipe_min_book_depth_dryrun: float = 500.0

    # Sizing — tiered by verifier confidence (v12.1).
    # The static snipe_min_net_edge floor is now interpreted as the LOW-tier
    # floor; mid- and high-confidence verdicts get tighter floors and tighter
    # per-trade caps so a single false-positive can't blow up bankroll.
    snipe_kelly_mult: float = 0.25
    snipe_max_single_pct: float = 0.05      # legacy; equals low-tier cap

    # High-confidence tier: structurally locked + 0.99+ confidence. Trade
    # even at razor-thin edges (e.g. price=0.998 → edge=0.002), but cap each
    # trade at 1% of bankroll. Worst-case: -0.998% per false positive.
    snipe_tier_high_min_conf: float = 0.99
    snipe_tier_high_min_edge: float = 0.002
    snipe_tier_high_max_pct: float = 0.01

    # Mid-confidence tier: 0.97-0.99. Real edge needed.
    snipe_tier_mid_min_conf: float = 0.97
    snipe_tier_mid_min_edge: float = 0.01
    snipe_tier_mid_max_pct: float = 0.02

    # Low-confidence tier: 0.95-0.97. Treat like v12 default snipe.
    snipe_tier_low_min_conf: float = 0.95
    snipe_tier_low_min_edge: float = 0.02
    snipe_tier_low_max_pct: float = 0.05

    # Verifier
    snipe_min_verifier_confidence: float = 0.95
    snipe_min_verifier_reason_chars: int = 30
    snipe_gemini_daily_cap_usd: float = 2.0

    # Verifier cache (v12.1): kills 60x duplicate LLM calls per market.
    snipe_cache_ttl_seconds: float = 1800.0     # 30 min
    snipe_cache_price_drift: float = 0.01
    snipe_cache_hours_drift: float = 1.0

    # ── Hit-rate killswitch (the only adaptive component) ─────────────
    killswitch_window: int = 50
    killswitch_min_hit_rate: float = 0.97
    killswitch_min_n: int = 50

    # ── Executor ──────────────────────────────────────────────────────
    use_maker_orders: bool = True
    fill_timeout_seconds: int = 60            # cancel unfilled limit after 60s

    # ── Monitoring ────────────────────────────────────────────────────
    health_check_interval: int = 60
    heartbeat_warn_seconds: int = 600
    heartbeat_critical_seconds: int = 1800
    position_check_interval: int = 60

    # ── Misc legacy keys still referenced ─────────────────────────────
    # The engine and position_manager still read a couple of these via
    # getattr; default values keep them inert in v12 but avoid AttributeError
    # on call paths we haven't fully gutted.
    enable_hourly_learning: bool = True

    # Tolerate legacy .env keys from deleted strategies.
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
