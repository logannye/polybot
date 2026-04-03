# Polybot

Fully autonomous AI trading bot for [Polymarket](https://polymarket.com). Uses a multi-strategy architecture — resolution sniping, ensemble forecasting, market making, and mean reversion — to find and exploit edge in binary outcome markets. The system learns from every resolved trade, adapting model weights, category preferences, and sizing parameters over time.

Built for micro-scale bankrolls ($100-500) targeting aggressive compounding through near-certain edge (resolution sniping), analytical edge (ensemble forecasting), passive income (market making spread capture + maker rebates), and contrarian edge (mean reversion after overreactions).

## Who is this for?

- **Algorithmic traders** who want to trade prediction markets programmatically
- **AI/ML practitioners** interested in applied LLM calibration and ensemble methods
- **Quantitative researchers** exploring Kelly criterion sizing in prediction market contexts
- **Polymarket participants** who want systematic, data-driven trading without manual monitoring

## How it works

Five concurrent strategies run at independent frequencies within a single async process:

### Strategy 1: Resolution Sniping (every 60s)

Targets markets approaching resolution where the outcome is effectively determined but the price hasn't converged. Three confidence tiers with expanding time windows:

- **Tier 0** (no LLM): Price at $0.95+ or $0.05-, within 24h — near-certain, just collecting convergence
- **Tier 1** (single Gemini Flash call): Price $0.85-$0.95, within 12h — cheap LLM verifies outcome is determined
- **Tier 2** (single Gemini Flash call): Price $0.80-$0.85, within 72h — conservative sizing, LLM verified
- **Tier 3** (single Gemini Flash call): Price $0.75-$0.80, within 120h — most conservative sizing

Sizing: Tier-dependent Kelly — T0: 0.50x (full snipe kelly), T1: 0.43x, T2: 0.28x, T3: 0.15x. High-edge trades (3-5%) get 1.5x sizing; 5%+ edge gets 2x.

**Per-market cooldown**: After exiting a market, re-entry is blocked for 1 hour unless price moves 3%+ (capturing second-leg convergence). Capped at 2 entries per market per 24h, with a cumulative exposure cap of 30% of bankroll per market across all open snipe positions.

### Strategy 2: Ensemble Forecast (every 5 min)

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
    v  Full ensemble (Claude Haiku + GPT-5.4-mini + Gemini 3 Flash) — 3-model blind analysis + web research
~0-2 trades
```

Three models estimate probability **blind** — the market price is intentionally withheld to prevent anchoring bias. Estimates are aggregated using confidence-weighted averaging with trust weights that evolve based on each model's historical Brier score.

Sizing: Quarter-Kelly (0.20x) with confidence modulation. Market-efficiency shrinkage of 35% blends ensemble probability toward the market price to counteract LLM overconfidence.

**Market loss blacklist**: After 2 stop-losses on the same market within 12 hours, the market is blacklisted — preventing repeated losing entries on the same thesis.

**Ensemble consensus**: Requires 2+ of 3 models to agree on direction (above or below market price) before placing a trade. Prevents one outlier model from dragging the ensemble into a bad position.

**Category filtering**: Markets in categories with negative average P&L (after 10+ trades) are automatically excluded from the forecast funnel.

**Time-stop**: Forecast trades are automatically exited if held past a dynamic time limit that scales with time-to-resolution — `max(90min floor, 15% of hours-to-resolution)`, capped at 8 hours. Only fires on flat or losing positions — profitable trades fall through to the normal take-profit/stop-loss checks.

### Strategy 3: Market Making (every 5s, opt-in)

Posts two-sided quotes on selected markets to earn spread capture, maker rebates, and liquidity rewards. Supports dry-run simulation mode — simulates fills when market price crosses quote prices, tracking P&L without placing real orders.

- **Market selection** — Scores markets by volume (≥$5K/day), book depth (≥$1K), resolution time (≥7 days out), and price range (0.10-0.90 for 3x two-sided reward bonus). Re-evaluates every 5 minutes, maintains 3-8 active markets.
- **Zero-fee quoting** — All orders use `post_only=True`, guaranteeing maker status (0% fees). Earns 20-25% of taker fees as maker rebates.
- **Inventory skew** — Linear skew proportional to net delta: when long YES, widens the ask and tightens the bid to encourage rebalancing. Hard limit of $50 net exposure per market.
- **Heartbeat** — Sends heartbeat every 5s (Polymarket cancels all orders if no heartbeat within 10s).
- **Volatility circuit breaker** — If any market moves >15% in 15 minutes, all quotes in that market are cancelled and it's blacklisted for 1 hour.

Revenue sources: spread capture + maker rebates (20-25% of taker fees, daily) + liquidity rewards (quadratic scoring, daily) + holding rewards (4% APY).

### Strategy 4: Mean Reversion (every 5 min, opt-in)

Detects markets where price has moved >10% in the last scan cycle and takes a contrarian position betting on partial reversion. Markets tend to overreact to news — this strategy captures the correction.

- **Price snapshot tracking** — Maintains in-memory price snapshots for all ~4,800 active markets. Compares current price against last snapshot to detect large moves without fetching price history for every market.
- **Contrarian entry** — Price spiked UP → buy NO (bet on reversion down). Price dropped → buy YES (bet on reversion up).
- **Edge estimation** — Expected edge = move magnitude × reversion fraction (default 40%). Only triggers on moves >10% with net edge >2%.
- **Custom exit targets** — Take-profit when 40% of the move reverts; stop-loss if price extends 25% further in the overreaction direction; time-stop at 24 hours.
- **Cooldown** — 24h per-market cooldown prevents chasing the same overreaction.

Sizing: Conservative Kelly (0.15x), max 10% bankroll per position, max 5 concurrent mean-reversion positions.

### Strategy 5: Arbitrage Scanner (every 45s, disabled)

Detects mathematically provable mispricings between related markets. Currently disabled pending fixes to the exhaustive group detection logic (non-mutually-exclusive markets were being incorrectly grouped as exhaustive outcomes).

## Architecture

Single Python async process (`asyncio`). Each strategy runs as an independent coroutine with its own scan interval, Kelly multiplier, and risk limits. Shared resources (DB, scanner, executor) are passed via a `TradingContext` dataclass. Bankroll contention between strategies is managed by an `asyncio.Lock` held only during the DB read-check-write window (~5ms).

```
polybot/
├── core/           # Engine orchestrator, configuration
├── strategies/     # Strategy framework + snipe, forecast, market_maker, mean_reversion, arbitrage
├── markets/        # Polymarket API client (Gamma + CLOB), filters, WebSocket hub, rewards client
├── analysis/       # LLM ensemble, quant signals, web research, pre-scoring
├── trading/        # Kelly sizing, risk management, order execution, CLOB gateway, fees, inventory, quotes
├── learning/       # Brier calibration, category tracking, self-assessment, per-trade learning
├── notifications/  # Email alerts (Resend) — trade events, daily reports
├── dashboard/      # FastAPI status dashboard
└── db/             # PostgreSQL schema (main + market-making) and connection pool
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

| Rule | Snipe | Forecast | Market Maker | Mean Reversion |
|------|-------|----------|-------------|----------------|
| Kelly multiplier | 0.50x (+ tiered edge scaling) | 0.20x | 0.15x | 0.15x |
| Max single position | 25% | 15% | 10% | 10% |
| Max per market (cumulative) | 30% | 1 position | `mm_max_inventory` ($50) | 1 position |
| Max concurrent (strategy) | — | — | 8 markets | 5 |

| Rule | Default |
|------|---------|
| Max total deployed | 70% of bankroll |
| Max per category | 25% of bankroll |
| Max concurrent positions | 12 |
| Daily loss limit | 15% of bankroll (triggers 6h circuit breaker) |
| Post-breaker cooldown | 24h at 50% Kelly |
| Min trade size | $1 |

**Category-specific fees**: All edge calculations use Polymarket's actual fee formula (`feeRate * price * (1 - price)`) with category-specific rates (crypto: 7.2%, sports: 3%, finance/politics: 4%, geopolitics: 0%). When `use_maker_orders=True` (default), all orders use `post_only` for guaranteed maker status — **0% fees** plus maker rebate income (20-25% of taker fees). At extreme prices (p=0.95), this corrects fee estimates from the old flat 2% down to the actual 0.19%.

**Bankroll-adaptive aggression**: Below $50, all Kelly multipliers are halved (survival mode). Above $500, multipliers are reduced by 15% (wealth preservation).

**Contrarian bet guard**: When the market consensus is >95% and the ensemble disagrees, the trade is skipped entirely. At >90% consensus with >30% disagreement, position size is halved. These extreme contrarian bets are the highest-risk — small calibration errors create large losses.

**Position concentration limits**: Each market can have at most 1 open forecast position (configurable via `MAX_POSITIONS_PER_MARKET`). Arb groups are deduplicated for 24 hours, persisted across restarts via DB-backed dedup.

**Edge skepticism**: Large edges (>7%) are progressively discounted — a 30%+ claimed edge gets only 30% of normal sizing, since extreme edges are more likely LLM miscalibration than genuine alpha.

## LLM ensemble

Three models run concurrently for full forecast analysis:

| Model | Role | Cost (in/out per MTok) |
|-------|------|-----------------------|
| Claude Haiku 4.5 | Calibrated probability estimates | $0.80 / $4.00 |
| GPT-5.4-mini | Broad knowledge, strong reasoning | $0.75 / $4.50 |
| Gemini 3 Flash | Fast screening + diverse training data | $0.50 / $3.00 |

Gemini 3 Flash also serves as the screening model for resolution sniping and the pre-ensemble quick screen gate. Snipe candidates use tier-appropriate LLM guards: Tier 1 candidates >12h and Tier 2 candidates >48h are rejected without an LLM call. Total LLM costs ~$2-3/day.

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

Every learned parameter is **clamped** (Kelly [0.15, 0.35], TP [0.10, 0.50], SL [0.10, 0.25], edge [0.01, 0.10], calibration [-0.10, +0.10]), **defaulted** (insufficient data falls back to config), and **toggleable** (each mechanism has an `enable_*` boolean in config).

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

# Market Making (opt-in, supports dry-run simulation)
MM_ENABLED=false
MM_QUOTE_SIZE_USD=10.0
MM_MAX_MARKETS=8

# Mean Reversion (opt-in)
MR_ENABLED=false
MR_TRIGGER_THRESHOLD=0.10
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

The bot starts all enabled strategies immediately. A dashboard is available at `http://localhost:8080`.

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
uv run pytest tests/ -v                                    # Run all tests
uv run pytest tests/ --cov=polybot --cov-report=term       # With coverage
uv run pytest tests/test_arbitrage.py -v                   # Run specific module
```

## Key design decisions

- **Multi-strategy architecture** — Five strategies run at different frequencies (5s/60s/5min/5min/45s) because different edge types have different time sensitivities. A single loop would bottleneck market making behind slow LLM calls.
- **Anti-anchoring** — LLMs never see the market price when estimating probability. This prevents them from simply parroting the market consensus.
- **Category-aware fee model** — Edge calculations use Polymarket's actual fee formula (`feeRate * p * (1-p)`) with per-category rates, not a flat 2%. All orders default to `post_only=True` (maker status, 0% fees). This dramatically improves edge accuracy at extreme prices where the old flat rate overestimated fees by 10x.
- **Strategy-specific Kelly** — Full Kelly for mathematically certain arb (0.80x), half for near-certain snipes (0.50x), quarter for uncertain forecasts (0.25x). This maximizes compounding on the highest-confidence trades.
- **Tiered LLM funnel** — Most markets are eliminated before any LLM is called. The few that pass get a $0.001 Gemini Flash screen before the $0.15 full ensemble. This cuts LLM costs from ~$10/day to ~$1-2/day.
- **Portfolio lock, not process lock** — Strategies only hold the asyncio.Lock during the 5ms DB read-check-write window, not during analysis. This means all three strategies analyze markets concurrently.
- **Single process** — At $100-500 bankroll, the bottleneck is edge detection quality, not execution speed. A single async process is the right complexity level.
- **Market making as a fourth revenue stream** — While the directional strategies (arb, snipe, forecast) capture edge from mispricings, market making earns from spread capture, maker rebates, and liquidity rewards — independent of directional accuracy. This provides stable daily income that compounds bankroll for the other strategies.
- **WebSocket price streaming** — A central `PriceStreamHub` subscribes to real-time price updates for all monitored tokens, enabling sub-second reaction times for position management and market-making quote adjustments.

## Disclaimer

This software is provided for educational and research purposes. Trading on prediction markets involves financial risk. Past performance does not guarantee future results. Use at your own risk and only with capital you can afford to lose.

## License

MIT
