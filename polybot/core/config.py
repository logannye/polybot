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
    # v12.5: maker-fill realism gate. The pre-v12.5 simulation always
    # filled maker orders at the limit price. Phase 0 audit (2026-05-02)
    # showed this was wildly optimistic — the LOCKED markets we'd been
    # simulating profitably had bid 0.001 / ask 0.999 (no real liquidity).
    # Real maker fills require best_ask close enough to our limit that
    # a small market move could match. Reject when:
    #   best_ask - our_limit > dry_run_maker_fill_tolerance
    # Default 0.02 (2pp): permits realistic small moves, rejects wide
    # books where no one is selling near our price.
    dry_run_maker_fill_tolerance: float = 0.02

    # ── Bankroll & deployment ─────────────────────────────────────────
    starting_bankroll: float = 2000.0
    min_trade_size: float = 1.0
    # v12.3: deployed cap raised 0.20 → 0.30 so the higher concurrency cap
    # below can actually fill. Killswitch (3-loss tolerance over 50 trades
    # at 4% per trade ≈ 11% drawdown) is still well under 30% halt.
    max_total_deployed_pct: float = 0.30      # snipe-wide deployed cap
    max_total_drawdown_pct: float = 0.30
    max_capital_divergence_pct: float = 0.10
    live_deployment_stage: str = "dry_run"    # dry_run → micro_test → ramp → full

    # ── Snipe strategy (the only strategy) ────────────────────────────
    snipe_enabled: bool = True
    snipe_interval_seconds: int = 60
    # v12.3: 4 → 10. Was the binding throttle (28h in max_concurrent_reached
    # over the 3-day v12.2 observation window). 10 × 4% = 40% deployed peak,
    # bounded by snipe_max_total_deployed_pct=0.30 below (≈7-8 fills before
    # the deployed gate trips).
    snipe_max_concurrent: int = 10
    snipe_max_total_deployed_pct: float = 0.30

    # Entry gates — v12.4.3 expanded universe further (0.92 → 0.85).
    # The 0.92 floor missed the highest-EV opportunity: verifier-confirmed
    # structural locks where the market HASN'T YET converged. Yesterday's
    # 4/4 trades were all 0.927-0.935 (post-convergence). At 0.85 entry,
    # gross edge is 0.15 (vs 0.07 at 0.93) — 2.3× per-dollar EV at the
    # same verifier accuracy. Verifier prompt is price-agnostic so
    # accuracy should generalize, but band is empirically unvalidated.
    # Per-trade worst-case at 0.85 × 4% = 3.4% bankroll (vs 3.68% at
    # 0.92), still inside the 30% drawdown halt.
    snipe_min_price: float = 0.85             # was 0.92 in v12.2-v12.4
    snipe_max_hours: float = 12.0             # live ceiling
    # v12.4.1 (2026-04-30): reverted 72h → 168h. The 72h entry filter
    # crushed the universe (~14-100× fewer signaling markets) without
    # adding value, because the v12.4 48h time-stop AT EXIT already
    # caps hold duration. Restricting at both entry AND exit was
    # redundant; the entry restriction was strictly destructive.
    snipe_max_hours_dryrun: float = 168.0     # 7d entry ceiling, 48h exit cap
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

    # Tier caps doubled in v12.3. The v12.2 caps were Kelly-conservative by
    # ~12× (proper quarter-Kelly at p_win=0.97, payoff 13:1 is ~24%); the
    # binding constraint was killswitch headroom, not Kelly. The killswitch
    # at 97%/50 tolerates 3 losses in 50 trades; at 4% low-tier cap × 0.92
    # max buy price, that's 3 × 3.68% = 11% drawdown — well inside the 30%
    # `max_total_drawdown_pct` halt.
    #
    # Worst-case single-trade loss (cap × max_buy_price = cap × 0.92):
    #   high: 0.010 × 0.92 = 0.92% bankroll
    #   mid:  0.020 × 0.92 = 1.84% bankroll
    #   low:  0.040 × 0.92 = 3.68% bankroll
    # 3-loss killswitch drawdown caps:
    #   high: 3 × 0.92% = 2.76%
    #   mid:  3 × 1.84% = 5.52%
    #   low:  3 × 3.68% = 11.04%

    # High-confidence tier: conf ≥0.99. 2% min edge (price ≤0.98).
    snipe_tier_high_min_conf: float = 0.99
    snipe_tier_high_min_edge: float = 0.02
    snipe_tier_high_max_pct: float = 0.01

    # Mid-confidence tier: conf 0.97–0.99. 4% min edge (price ≤0.96).
    snipe_tier_mid_min_conf: float = 0.97
    snipe_tier_mid_min_edge: float = 0.04
    snipe_tier_mid_max_pct: float = 0.02

    # Low-confidence tier: conf 0.95–0.97. 6% min edge (price ≤0.94).
    snipe_tier_low_min_conf: float = 0.95
    snipe_tier_low_min_edge: float = 0.06
    snipe_tier_low_max_pct: float = 0.04

    # Verifier
    snipe_min_verifier_confidence: float = 0.95
    snipe_min_verifier_reason_chars: int = 30
    snipe_gemini_daily_cap_usd: float = 2.0

    # Verifier cache (v12.1): kills 60x duplicate LLM calls per market.
    snipe_cache_ttl_seconds: float = 1800.0     # 30 min
    snipe_cache_price_drift: float = 0.01
    snipe_cache_hours_drift: float = 1.0

    # ── Exit rules (v12.4: asymmetric upside) ─────────────────────────
    # Three exit triggers, evaluated in priority order each cycle:
    #
    #   1. STOP-LOSS — within the first `stop_loss_window_hours` of entry,
    #      if the YES price has moved ≥ `stop_loss_adverse_pp` AGAINST our
    #      thesis, close at mark. Caps per-trade loss at ~5% of position
    #      instead of 100% on the rare verifier-wrong call.
    #
    #   2. TAKE-PROFIT — when the move toward our thesis has captured
    #      ≥ `early_exit_capture_pct` (default 80%) of the max-possible
    #      move (entry → 0 for NO, entry → 1 for YES), close at mark.
    #      The 3-day v12.3 data showed verifier-correct trades moving
    #      80+pp in 1-3d; capturing 75% in 1-2d beats holding 7d to
    #      resolution by ~3× in time-yield.
    #
    #   3. TIME-STOP — any open trade past `max_hold_hours` closes at
    #      mark. Forces unrealized into realized; frees the slot.
    #
    # Combined, these turn the win/loss size ratio from ~1:1 (full +93%
    # vs full -100% per position) to ~15:1 (+75% vs -5%).
    snipe_early_exit_enabled: bool = True
    snipe_early_exit_check_interval: int = 60   # seconds
    snipe_early_exit_capture_pct: float = 0.80  # take-profit at 80% of max move
    snipe_max_hold_hours: float = 48.0          # time-stop ceiling
    snipe_stop_loss_adverse_pp: float = 0.05    # 5pp adverse → cut
    snipe_stop_loss_window_hours: float = 2.0   # window for stop-loss eligibility

    # v12.4: prevent ≥1 position per news event. Markets that share a
    # `group_slug` in Gamma (e.g. "Q1 2026 GDP" bracket markets) are driven
    # by the same underlying outcome, so opening multiple positions on
    # them creates phantom diversification — one news event triggers
    # correlated PnL across all of them. Off → on by default in v12.4.
    snipe_correlation_filter_enabled: bool = True

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
