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
- **Exhaustive outcome arbitrage** — Multi-outcome groups (e.g., "Who wins?" with candidates A, B, C) where YES prices don't sum to $1.00
- **Temporal subset arbitrage** — "Will X happen by June?" priced higher than "Will X happen by July?" (logically impossible)

Sizing: Near-full Kelly (0.80x). Edge is mathematically certain — the only risk is execution.

### Strategy 2: Resolution Sniping (every 2 min)

Targets markets within 1-6 hours of resolution where the outcome is effectively determined but the price hasn't converged.

- **Tier 0** (no LLM): Price already at $0.92+ or $0.08- — near-certain, just collecting convergence
- **Tier 1** (single Gemini Flash call): Price $0.80-$0.92, within 3h — cheap LLM verifies outcome is determined

Sizing: Half-Kelly (0.50x). Near-certain but with residual risk.

### Strategy 3: Ensemble Forecast (every 5 min)

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

## Architecture

Single Python async process (`asyncio`). Each strategy runs as an independent coroutine with its own scan interval, Kelly multiplier, and risk limits. Shared resources (DB, scanner, executor) are passed via a `TradingContext` dataclass. Bankroll contention between strategies is managed by an `asyncio.Lock` held only during the DB read-check-write window (~5ms).

```
polybot/
├── core/           # Engine orchestrator, configuration
├── strategies/     # Strategy framework + arbitrage, snipe, forecast implementations
├── markets/        # Polymarket API client, filters, WebSocket tracker
├── analysis/       # LLM ensemble, quant signals, web research, pre-scoring
├── trading/        # Kelly sizing, risk management, order execution
├── learning/       # Brier calibration, category tracking, self-assessment
├── notifications/  # Email alerts (Resend) — trade events, daily reports
├── dashboard/      # FastAPI status dashboard
└── db/             # PostgreSQL schema and connection pool
```

If the process crashes, systemd restarts it within 10 seconds. On startup, the engine reconciles any positions that resolved during downtime.

## Risk management

Strategy-aware risk management with aggressive sizing for high-certainty trades:

| Rule | Arbitrage | Snipe | Forecast |
|------|-----------|-------|----------|
| Kelly multiplier | 0.80x | 0.50x | 0.25x |
| Max single position | 40% | 25% | 15% |

| Rule | Default |
|------|---------|
| Max total deployed | 70% of bankroll |
| Max per category | 25% of bankroll |
| Max concurrent positions | 12 |
| Daily loss limit | 15% of bankroll (triggers 6h circuit breaker) |
| Post-breaker cooldown | 24h at 50% Kelly |
| Min trade size | $1 |

**Fee-adjusted edge**: All edge calculations account for Polymarket's ~2% fee on winnings. Marginal edges that don't survive fee drag are automatically discarded.

**Bankroll-adaptive aggression**: Below $50, all Kelly multipliers are halved (survival mode). Above $500, multipliers are reduced by 15% (wealth preservation).

## LLM ensemble

Three models run concurrently for full forecast analysis:

| Model | Role |
|-------|------|
| Claude Sonnet 4.6 | Strong reasoning, calibrated probability estimates |
| GPT-4o | Broad knowledge base |
| Gemini 2.5 Flash | Fast screening + diverse training data |

Gemini Flash also serves as the cheap screening model for resolution sniping and the pre-ensemble quick screen gate, keeping LLM costs under $2/day.

## Adaptive learning

- **Model trust weights** — Updated after every resolved trade via EMA on per-model Brier scores. Cold start protection: equal weights for the first 30 trades.
- **Strategy-level P&L** — Win rate, total P&L, and average edge tracked per strategy. Kill switch disables any strategy with negative P&L after 50+ trades.
- **Category performance** — Tracks win rate and ROI per market category. Biases the forecast pre-scoring toward profitable categories.
- **Calibration correction** — After 50+ trades, applies per-bucket correction to ensemble probabilities to fix systematic over/underconfidence.
- **Daily self-assessment** (midnight UTC):
  - Kelly multiplier tuning based on 7-day drawdown
  - Edge threshold tuning based on marginal trade profitability
  - Strategy kill switch evaluation
  - Circuit breaker check with post-breaker cooldown activation
  - End-of-day report email with full P&L breakdown by strategy

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
| Critical | Strategy disabled (5 consecutive errors), unresponsive 30+ min |
| Warning | Circuit breaker triggered, stale position |
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
uv run pytest tests/ -v                                    # Run all 188 tests
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
