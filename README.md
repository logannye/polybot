# Polybot

Fully autonomous AI trading bot for [Polymarket](https://polymarket.com). Uses a multi-model LLM ensemble to estimate true probabilities of binary outcome markets, quantitative signals to confirm entry timing, and fractional Kelly criterion to size positions. The system learns from every resolved trade — adapting model weights, category preferences, and sizing parameters over time.

Built for micro-scale bankrolls ($100-500) targeting short-term markets that resolve within 72 hours.

## Who is this for?

- **Algorithmic traders** who want to trade prediction markets programmatically
- **AI/ML practitioners** interested in applied LLM calibration and ensemble methods
- **Quantitative researchers** exploring Kelly criterion sizing in prediction market contexts
- **Polymarket participants** who want systematic, data-driven trading without manual monitoring

## How it works

Polybot runs a continuous 5-minute cycle:

```
SCAN → FILTER → ANALYZE → SCORE → SIZE → EXECUTE → MANAGE → LEARN
```

1. **Scan** — Pulls all active markets from Polymarket's CLOB API
2. **Filter** — Keeps only short-term markets (<72h) with sufficient liquidity (>$500 book depth), reasonable prices ($0.05-$0.95), and respects cooldown periods
3. **Analyze** — For each candidate:
   - Runs a web search (Brave API) to gather recent context
   - Fires 3 LLM models concurrently (Claude, GPT-4o, Gemini) — each estimates the true probability *without* seeing the market price (anti-anchoring)
   - Computes 5 quantitative signals in parallel (line movement, volume spike, book imbalance, spread, time decay)
4. **Score** — Computes edge as `|ensemble_probability - market_price|`, discards if below threshold (default 5%)
5. **Size** — Fractional Kelly (quarter-Kelly) with confidence modulation based on model agreement and quant signals
6. **Execute** — Places limit orders, monitors fills, manages open positions
7. **Manage** — Checks open positions every 30 minutes for early exit (edge evaporated) or stop loss
8. **Learn** — After each resolved market: updates per-model Brier scores, adjusts ensemble trust weights, tracks category performance, runs daily self-assessment to tune Kelly multiplier and edge threshold

## Architecture

Single Python async process (`asyncio`). No threads, no message brokers, no microservices. All state in PostgreSQL. If the process crashes, systemd restarts it within 10 seconds — the process is stateless on restart.

```
polybot/
├── core/           # Main engine loop, configuration
├── markets/        # Polymarket API client, filters, WebSocket tracker
├── analysis/       # LLM ensemble, quant signals, web research
├── trading/        # Kelly sizing, risk management, order execution
├── learning/       # Brier calibration, category tracking, self-assessment
├── notifications/  # Email (Resend) + SMS (Twilio) alerts
├── dashboard/      # FastAPI status dashboard
└── db/             # PostgreSQL schema and connection pool
```

## Risk management

Designed for capital preservation at micro-scale:

| Rule | Default |
|------|---------|
| Position sizing | Quarter-Kelly (0.25x) |
| Max single position | 15% of bankroll |
| Max total deployed | 50% of bankroll |
| Max per category | 25% of bankroll |
| Max concurrent positions | 8 |
| Daily loss limit | 20% of bankroll (triggers 12h circuit breaker) |
| Min trade size | $2 |

Confidence modulation further reduces position sizes when LLM models disagree or quant signals are unfavorable.

## LLM ensemble

Three models run concurrently on every market analysis:

| Model | Role |
|-------|------|
| Claude Sonnet 4.6 | Strong reasoning, calibrated probability estimates |
| GPT-4o | Broad knowledge base |
| Gemini 2.5 Flash | Fast, diverse training data for independent perspective |

Each model estimates probability **blind** — the current market price is intentionally withheld to prevent anchoring bias. Estimates are aggregated using confidence-weighted averaging with trust weights that evolve based on each model's historical Brier score.

Inter-model agreement (standard deviation) is itself a signal: high agreement increases position sizing, high disagreement reduces it.

## Adaptive learning

The system gets smarter over time:

- **Model trust weights** — Updated after every resolved trade via EMA on per-model Brier scores. Better-calibrated models earn more influence.
- **Category performance** — Tracks win rate and ROI per market category (politics, sports, crypto, etc.). Biases scanning toward profitable categories after 20+ resolved trades.
- **Daily self-assessment** (every 24h):
  - Calibration curve analysis — detects systematic over/underconfidence
  - Edge threshold tuning — raises threshold if marginal trades lose money, lowers if they're profitable
  - Kelly multiplier adjustment — nudges sizing up if drawdowns are within tolerance, down if too volatile
- **Cold start** — First 30 trades use minimum sizes and equal model weights until enough data accumulates

## Setup

### Prerequisites

- Python 3.12+
- PostgreSQL 16
- [uv](https://docs.astral.sh/uv/) package manager
- API keys: Anthropic, OpenAI, Google AI, Brave Search, Resend, Twilio
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
TWILIO_ACCOUNT_SID=           # For SMS alerts
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=           # Your Twilio phone number
ALERT_EMAIL=you@example.com   # Where to send email alerts
ALERT_PHONE=+1234567890       # Where to send SMS alerts

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

The bot starts scanning immediately. A dashboard is available at `http://localhost:8080`.

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

## Monitoring

### Dashboard

Access via SSH tunnel (dashboard binds to localhost only):

```bash
ssh -L 8080:localhost:8080 polybot@your-vps
# Then open http://localhost:8080
```

Endpoints:
- `GET /` — Bankroll, P&L, open positions, system health
- `GET /trades` — Recent trade history with outcomes
- `GET /models` — Per-model Brier scores and trust weights
- `GET /health` — JSON health check (use with UptimeRobot)

### Alerts

- **Email** — Trade executed, trade resolved, daily summary, weekly report
- **SMS** — Circuit breaker triggered, system down, wallet balance low, exceptional P&L events

## Monthly operating cost

| Item | Cost |
|------|------|
| VPS (1 vCPU, 1GB RAM) | $5-7 |
| LLM API calls (~50 markets/day x 3 models) | $3-8 |
| Twilio SMS | ~$1 |
| Resend email | Free tier |
| Brave Search API | $3 |
| **Total** | **~$12-19/mo** |

The system needs to earn ~$0.50/day to cover its own costs.

## Testing

```bash
uv run pytest tests/ -v                                    # Run all 110 tests
uv run pytest tests/ --cov=polybot --cov-report=term       # With coverage
uv run pytest tests/test_kelly.py -v                       # Run specific module
```

## Key design decisions

- **Anti-anchoring** — LLMs never see the market price when estimating probability. This prevents them from simply parroting the market consensus and forces independent analysis.
- **Quarter-Kelly** — Full Kelly is theoretically optimal but assumes perfect probability estimates. Quarter-Kelly sacrifices ~50% of growth rate but reduces variance by ~75%.
- **Ensemble disagreement as signal** — When models disagree significantly, it means the question is genuinely uncertain. The system automatically reduces position sizes rather than forcing a bet.
- **No market orders** — Always uses limit orders for entries to avoid slippage. Aggressive limits (crossing the spread) only for exits when speed matters.
- **Single process** — At $300 bankroll, the bottleneck is edge detection quality, not execution speed. A single async process is the right complexity level.

## Disclaimer

This software is provided for educational and research purposes. Trading on prediction markets involves financial risk. Past performance does not guarantee future results. Use at your own risk and only with capital you can afford to lose.

## License

MIT
