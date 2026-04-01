# Polybot

Fully autonomous AI trading bot for [Polymarket](https://polymarket.com). Uses a multi-strategy architecture — arbitrage scanning, resolution sniping, and a multi-model LLM ensemble — to find and exploit edge in binary outcome markets. The system learns from every resolved trade, adapting model weights, category preferences, and sizing parameters over time.

Built for micro-scale bankrolls ($100-500) targeting aggressive compounding through mathematically provable edge (arbitrage), near-certain edge (resolution sniping), and analytical edge (ensemble forecasting).

## Who is this for?

- **Algorithmic traders** who want to trade prediction markets programmatically
- **AI/ML practitioners** interested in applied LLM calibration and ensemble methods
- **Quantitative researchers** exploring Kelly criterion sizing in prediction market contexts
- **Polymarket participants** who want systematic, data-driven trading without manual monitoring

## How it works

Three concurrent strategies run at independent frequencies within a single async process:

### Strategy 1: Arbitrage Scanner (every 45s)

Detects mathematically provable mispricings between related markets. No LLMs, pure math.

- **Complement arbitrage** — YES + NO prices on a single market sum to less than $1.00 (buy both for guaranteed profit)
- **Exhaustive outcome arbitrage** — Multi-outcome groups (e.g., "Who wins?" with candidates A, B, C) where YES prices don't sum to $1.00. Groups are validated: probability sums must be between 0.5-1.8, and net edge is capped at 20% to reject false positives from cosmetic API groupings
- **Temporal subset arbitrage** — "Will X happen by June?" priced higher than "Will X happen by July?" (logically impossible)

Sizing: Near-full Kelly (0.80x). Edge is mathematically certain — the only risk is execution.

### Strategy 2: Resolution Sniping (every 2 min)

Targets markets approaching resolution where the outcome is effectively determined but the price hasn't converged. Three confidence tiers with expanding time windows:

- **Tier 0** (no LLM): Price at $0.95+ or $0.05-, within 24h — near-certain, just collecting convergence
- **Tier 1** (single Gemini Flash call): Price $0.85-$0.95, within 12h — cheap LLM verifies outcome is determined
- **Tier 2** (single Gemini Flash call): Price $0.85-$0.95, within 120h — conservative sizing, LLM verified

Sizing: Tier-dependent Kelly — T0: 0.50x (full snipe kelly), T1: 0.35x, T2: 0.20x. High-edge trades (3-5%) get 1.5x sizing; 5%+ edge gets 2x.

**Per-market cooldown**: After exiting a market, re-entry is blocked for 4 hours unless price moves 3%+ (capturing second-leg convergence). Capped at 3 entries per market per 24h.

### Strategy 3: Ensemble Forecast (every 3 min)

The full analysis pipeline with a cost-efficient tiered funnel:

```
~200-500 active markets
    |
    v  Filter (no LLM) — resolution time, liquidity, price range
~30-60 markets
    |
    v  Pre-score (no LLM) — rank by quant signals + book depth + category history
~5 markets
    |
    v  Quick screen (single Gemini Flash) — discard if <3% edge
~2-3 markets
    |
    v  Full ensemble (Claude + GPT-4o + Gemini) — 3-model blind analysis + web research
~0-2 trades
```

Three models estimate probability **blind** — the market price is intentionally withheld to prevent anchoring bias. Estimates are aggregated using confidence-weighted averaging with trust weights that evolve based on each model's historical Brier score.

Sizing: Quarter-Kelly (0.25x) with confidence modulation.

**Market loss blacklist**: After 2 stop-losses on the same market within 12 hours, the market is blacklisted — preventing repeated losing entries on the same thesis.

**Time-stop**: Forecast trades held longer than 60 minutes are automatically exited at market price if flat or losing. Profitable positions are exempt — they fall through to the normal take-profit/stop-loss checks. This frees capital from stale positions without cutting winners.

## Architecture

Single Python async process (`asyncio`). Each strategy runs as an independent coroutine with its own scan interval, Kelly multiplier, and risk limits. Shared resources (DB, scanner, executor) are passed via a `TradingContext` dataclass. Bankroll contention between strategies is managed by an `asyncio.Lock` held only during the DB read-check-write window (~5ms).

```
polybot/
├── core/           # Engine orchestrator, configuration
├── strategies/     # Strategy framework + arbitrage, snipe, forecast implementations
├── markets/        # Polymarket API client (Gamma + CLOB), filters, WebSocket tracker
├── analysis/       # LLM ensemble, quant signals, web research, pre-scoring
├── trading/        # Kelly sizing, risk management, order execution, CLOB gateway
├── learning/       # Brier calibration, category tracking, self-assessment, per-trade learning
├── notifications/  # Email alerts (Resend) — trade events, daily reports
├── dashboard/      # FastAPI status dashboard
└── db/             # PostgreSQL schema and connection pool
```

### Crash resilience

- **Graceful shutdown** — SIGTERM/SIGINT handlers cancel all strategy tasks cleanly, log open positions on exit
- **Exponential backoff** — Strategy errors trigger 30s→60s→...→10min backoff (30 consecutive failures to disable, not 5)
- **Capital reconciliation** — Every 5 minutes, `total_deployed` is reconciled against actual open positions in the DB, auto-correcting any drift > $1
- **Startup recovery** — On restart, reconciles positions that resolved during downtime
- **LaunchAgent (macOS)** — `ai.polybot.trader` plist with KeepAlive, PostgreSQL readiness guard, kill switch support, caffeinate to prevent sleep, and 45s throttle between restarts
- **systemd (Linux)** — If the process crashes, systemd restarts it within 10 seconds

## Risk management

Strategy-aware risk management with aggressive sizing for high-certainty trades:

| Rule | Arbitrage | Snipe | Forecast |
|------|-----------|-------|----------|
| Kelly multiplier | 0.80x | 0.50x (+ tiered edge scaling) | 0.25x |
| Max single position | 40% | 30% | 15% |
| Bankroll gate | $2K minimum | — | — |

| Rule | Default |
|------|---------|
| Max total deployed | 90% of bankroll |
| Max per category | 25% of bankroll |
| Max concurrent positions | 20 |
| Daily loss limit | Configurable (default 15%, disabled during sprint mode) |
| Post-breaker cooldown | 24h at 50% Kelly |
| Min trade size | $1 |

**Fee-adjusted edge**: All edge calculations account for Polymarket's ~2% fee on winnings. Marginal edges that don't survive fee drag are automatically discarded.

**Bankroll-adaptive aggression**: Below $50, all Kelly multipliers are halved (survival mode). Above $500, multipliers are reduced by 15% (wealth preservation).

**Contrarian bet guard**: When the market consensus is >95% and the ensemble disagrees, the trade is skipped entirely. At >90% consensus with >30% disagreement, position size is halved. These extreme contrarian bets are the highest-risk — small calibration errors create large losses.

**Position concentration limits**: Each market can have at most 1 open forecast position (configurable via `MAX_POSITIONS_PER_MARKET`). Arb groups are deduplicated for 24 hours, persisted across restarts via DB-backed dedup.

**Edge skepticism**: Large edges (>10%) are progressively discounted — a 40%+ claimed edge gets only 30% of normal sizing, since extreme edges are more likely LLM miscalibration than genuine alpha.

## LLM ensemble

Three models run concurrently for full forecast analysis:

| Model | Role |
|-------|------|
| Claude Sonnet 4.6 | Strong reasoning, calibrated probability estimates |
| GPT-4o | Broad knowledge base |
| Gemini 2.5 Flash | Fast screening + diverse training data |

Gemini Flash also serves as the cheap screening model for resolution sniping and the pre-ensemble quick screen gate, keeping LLM costs under $2/day.

## Adaptive learning

Learning fires at two timescales — per-trade (instant) and hourly (periodic):

### Per-trade learning (instant)

Every trade close — whether take-profit, stop-loss, time-stop, early-exit, or resolution — triggers `TradeLearner.on_trade_closed()`:

- **Proxy trust weights** — Model Brier EMA updated using early-exit outcomes as proxy signals. Take-profit (ensemble was right, alpha=0.05), stop-loss (ensemble was wrong, alpha=0.08), ambiguous exits (alpha=0.03). Lower learning rates than resolution (0.15) because proxy signals are noisier.
- **Exit-reason analytics** — Per-strategy, per-exit-reason tracking of count, total P&L, and average hold time in `strategy_performance.learned_params` JSONB.
- **Category performance** — Tracks win rate and ROI per market category in `system_state.category_scores`. Biases the forecast pre-scoring toward profitable categories.
- **Strategy avg_edge** — Running average of edge quality per strategy.

### Hourly learning cycle

`_hourly_learning()` runs every hour to recompute adaptive parameters:

- **Adaptive TP/SL thresholds** — Analyzes 14 days of trade outcomes to find optimal take-profit and stop-loss levels per strategy. Tests thresholds in 5% increments, picks the one maximizing frequency-weighted expected value (TP) or minimizing frequency-weighted loss (SL). The position manager reads learned thresholds, falling back to config defaults when data is insufficient.
- **Snipe parameter learning** — Buckets snipe trades by edge level and price level, finds the minimum profitable edge bucket, and adjusts `snipe_min_net_edge` automatically.
- **Kelly/edge adjustment** — 3-day lookback window (vs 7-day for daily). Adjusts Kelly multiplier based on max drawdown and edge threshold based on marginal-bucket profitability.
- **Calibration correction refresh** — Recomputes per-bin probability corrections from 30-day data. Corrections fix systematic ensemble overconfidence and are applied via nearest-bin lookup on every forecast cycle.

### Daily self-assessment (midnight UTC)

- Strategy kill switch evaluation (negative P&L after 50+ trades)
- Circuit breaker check with post-breaker cooldown activation
- End-of-day report email with full P&L breakdown by strategy

### Safety invariants

Every learned parameter is **clamped** (Kelly [0.15, 0.35], TP [0.10, 0.50], SL [0.10, 0.40], edge [0.01, 0.10], calibration [-0.10, +0.10]), **defaulted** (insufficient data falls back to config), and **toggleable** (each mechanism has an `enable_*` boolean in config).

- **Kelly inputs audit trail** — Every trade records the full sizing decision: ensemble probability, market price, edge, kelly fraction, confidence multiplier, skepticism discount, and effective kelly. Enables post-hoc analysis of sizing quality.

## Monitoring

### Dashboard

Access via SSH tunnel (binds to localhost only):

```bash
ssh -L 8080:localhost:8080 polybot@your-vps
```

Endpoints:
- `GET /` — Bankroll, P&L, open positions, system health
- `GET /trades` — Recent trade history with outcomes
- `GET /models` — Per-model Brier scores and trust weights
- `GET /strategies` — Per-strategy performance (trades, P&L, enabled/disabled)
- `GET /arb` — Recent arbitrage opportunities and trades
- `GET /health` — JSON health check (use with UptimeRobot)

### Health monitor (every 60s)

- **Heartbeat**: alerts if any strategy hasn't completed a cycle in 10+ minutes
- **Stale positions**: warns if a trade has been open for >80% of the market's resolution time
- **Memory**: logs warning if RSS exceeds 512MB

### Email alerts

All notifications to your configured email via Resend:

| Priority | When |
|----------|------|
| Critical | Strategy disabled (30 consecutive errors), unresponsive 30+ min |
| Warning | Circuit breaker triggered, stale position, 5+ consecutive errors (every 5th) |
| Trade | Trade executed, trade resolved |
| Daily | End-of-day P&L report with strategy breakdown |

## Setup

### Prerequisites

- Python 3.12+
- PostgreSQL 16
- [uv](https://docs.astral.sh/uv/) package manager
- API keys: Anthropic, OpenAI, Google AI, Brave Search, Resend
- Polymarket account with API key and a funded Polygon wallet (USDC)

### Installation

```bash
git clone https://github.com/logannye/polybot.git
cd polybot
uv sync --all-extras
```

### Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Required environment variables:

```bash
# Polymarket
POLYMARKET_API_KEY=           # Your Polymarket CLOB API key
POLYMARKET_PRIVATE_KEY=       # Ethereum private key for your trading wallet

# LLM Providers
ANTHROPIC_API_KEY=            # Claude API key
OPENAI_API_KEY=               # OpenAI API key
GOOGLE_API_KEY=               # Google AI API key

# Web Research
BRAVE_API_KEY=                # Brave Search API key ($3/mo for 2K queries)

# Database
DATABASE_URL=postgresql://polybot:password@localhost:5432/polybot

# Notifications
RESEND_API_KEY=               # For email alerts
ALERT_EMAIL=you@example.com   # Where to send alerts and daily reports

# Polymarket CLOB L2 Credentials (run: uv run python scripts/derive_creds.py)
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

# Dry-run mode (set to false for live trading)
DRY_RUN=true

# Bot Config (all optional — defaults shown)
STARTING_BANKROLL=300.00
KELLY_MULT=0.25
EDGE_THRESHOLD=0.05
SCAN_INTERVAL_SECONDS=300
```

### Database setup

```bash
sudo -u postgres createuser polybot
sudo -u postgres createdb polybot -O polybot
```

The schema is applied automatically on first run.

### Running locally

```bash
uv run python -m polybot
```

The bot starts all three strategies immediately. A dashboard is available at `http://localhost:8080`.

### Going live

1. Derive your CLOB credentials:

```bash
uv run python scripts/derive_creds.py
# Copy the output into your .env file
```

2. Run in observation mode first (default):

```bash
uv run python -m polybot
# DRY_RUN=true by default — monitors and records but doesn't trade
# Watch daily report emails for 24-48h
```

3. When ready for live trading:

```bash
# In .env, set:
# DRY_RUN=false
# STARTING_BANKROLL=20  (start small)
uv run python -m polybot
```

### Running on a VPS (recommended)

For 24/7 operation on a $5/mo VPS (DigitalOcean, Hetzner):

```bash
# On the VPS
sudo useradd -m -s /bin/bash polybot
sudo mkdir -p /opt/polybot
sudo chown polybot:polybot /opt/polybot

# Clone and set up
sudo -u polybot git clone https://github.com/logannye/polybot.git /opt/polybot
cd /opt/polybot
sudo -u polybot uv sync --all-extras
sudo -u polybot cp .env.example .env
# Edit .env with your credentials

# Install and start the service
sudo cp polybot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot

# Check status
sudo systemctl status polybot
sudo journalctl -u polybot -f  # Live logs
```

### Deploying updates

```bash
./deploy.sh
```

Or manually: `ssh polybot@vps "cd /opt/polybot && git pull && uv sync && sudo systemctl restart polybot"`

## Monthly operating cost

| Item | Cost |
|------|------|
| VPS (1 vCPU, 1GB RAM) | $5-7 |
| LLM API calls (tiered funnel, ~$1-2/day) | $30-60 |
| Resend email | Free tier |
| Brave Search API | $3 |
| **Total** | **~$38-70/mo** |

## Testing

```bash
uv run pytest tests/ -v                                    # Run all 264 tests
uv run pytest tests/ --cov=polybot --cov-report=term       # With coverage
uv run pytest tests/test_arbitrage.py -v                   # Run specific module
```

## Key design decisions

- **Multi-strategy architecture** — Arb, snipe, and forecast run at different frequencies (45s/2min/5min) because different edge types have different time sensitivities. A single loop would bottleneck arb detection behind slow LLM calls.
- **Anti-anchoring** — LLMs never see the market price when estimating probability. This prevents them from simply parroting the market consensus.
- **Fee-adjusted Kelly** — Edge calculations subtract Polymarket's ~2% fee on winnings, which disproportionately affects high-probability trades. This prevents the system from chasing thin-edge, high-probability bets that are unprofitable after fees.
- **Strategy-specific Kelly** — Full Kelly for mathematically certain arb (0.80x), half for near-certain snipes (0.50x), quarter for uncertain forecasts (0.25x). This maximizes compounding on the highest-confidence trades.
- **Tiered LLM funnel** — Most markets are eliminated before any LLM is called. The few that pass get a $0.001 Gemini Flash screen before the $0.15 full ensemble. This cuts LLM costs from ~$10/day to ~$1-2/day.
- **Portfolio lock, not process lock** — Strategies only hold the asyncio.Lock during the 5ms DB read-check-write window, not during analysis. This means all three strategies analyze markets concurrently.
- **Single process** — At $100-500 bankroll, the bottleneck is edge detection quality, not execution speed. A single async process is the right complexity level.

## Disclaimer

This software is provided for educational and research purposes. Trading on prediction markets involves financial risk. Past performance does not guarantee future results. Use at your own risk and only with capital you can afford to lose.

## License

MIT
