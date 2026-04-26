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
    # v12.2: maker-fill simulation — match deployed behavior (post_only=True).
    # When true + post_only, fill at limit price with 0% fee, no spread cap.
    # Set false to stress-test against worst-case taker fills.
    dry_run_assume_maker_fill: bool = True

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

    # Entry gates — v12.2 widened universe.
    # Pre-v12.2 the floor was 0.96; data showed the universe at 0.96+ was
    # dominated by 0.998+ markets that are negative-EV after fees even at
    # 100% accuracy. 0.92 floor expands the universe to markets with real
    # gross edge (8%+) where the verifier+killswitch combo can actually
    # produce learning signal. The variance is higher, so per-trade caps
    # in the sizing tiers shrink to compensate.
    snipe_min_price: float = 0.92             # was 0.96 in v12.0–v12.1
    snipe_max_hours: float = 12.0             # live ceiling
    snipe_max_hours_dryrun: float = 168.0     # 7d for observation
    snipe_min_net_edge: float = 0.02          # legacy; superseded by tier floors
    snipe_min_book_depth: float = 1000.0
    snipe_min_book_depth_dryrun: float = 500.0
    # v12.2: bypass dry-run spread cap for snipe trades. Snipe holds to
    # resolution (no exit transaction), so spread is irrelevant for exit;
    # the spread cap was a v10 inheritance for round-trip strategies.
    snipe_skip_spread_gate: bool = True

    # Sizing — tiered by verifier confidence (v12.1).
    # The static snipe_min_net_edge floor is now interpreted as the LOW-tier
    # floor; mid- and high-confidence verdicts get tighter floors and tighter
    # per-trade caps so a single false-positive can't blow up bankroll.
    snipe_kelly_mult: float = 0.25
    snipe_max_single_pct: float = 0.05      # legacy; equals low-tier cap

    # Tier floors and caps re-tuned for v12.2's wider universe (price ≥0.92).
    # At 0.92 entry, gross edge is 8% but a wrong call loses 92% of position,
    # so per-trade caps shrink and edge floors rise vs the v12.1 values
    # (which were calibrated for 0.96+ where wrong-call loss is ≤4%).
    #
    # Worst-case single-trade loss (cap × max_buy_price = cap × 0.92):
    #   high: 0.005 × 0.92 = 0.46% bankroll
    #   mid:  0.010 × 0.92 = 0.92% bankroll
    #   low:  0.020 × 0.92 = 1.84% bankroll
    # Killswitch (97% / 50) catches systematic mispricing within ~1 day.

    # High-confidence tier: conf ≥0.99. 2% min edge (price ≤0.98).
    snipe_tier_high_min_conf: float = 0.99
    snipe_tier_high_min_edge: float = 0.02
    snipe_tier_high_max_pct: float = 0.005

    # Mid-confidence tier: conf 0.97–0.99. 4% min edge (price ≤0.96).
    snipe_tier_mid_min_conf: float = 0.97
    snipe_tier_mid_min_edge: float = 0.04
    snipe_tier_mid_max_pct: float = 0.01

    # Low-confidence tier: conf 0.95–0.97. 6% min edge (price ≤0.94).
    snipe_tier_low_min_conf: float = 0.95
    snipe_tier_low_min_edge: float = 0.06
    snipe_tier_low_max_pct: float = 0.02

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
