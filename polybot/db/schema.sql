CREATE TABLE IF NOT EXISTS markets (
    id SERIAL PRIMARY KEY,
    polymarket_id TEXT UNIQUE NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'unknown',
    resolution_time TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_price NUMERIC(5,4),
    volume_24h NUMERIC,
    book_depth NUMERIC,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS analyses (
    id SERIAL PRIMARY KEY,
    market_id INT NOT NULL REFERENCES markets(id),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_estimates JSONB NOT NULL,
    ensemble_probability NUMERIC(5,4) NOT NULL,
    ensemble_stdev NUMERIC(5,4) NOT NULL,
    quant_signals JSONB NOT NULL,
    edge NUMERIC(5,4) NOT NULL,
    web_research_summary TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    market_id INT NOT NULL REFERENCES markets(id),
    analysis_id INT NOT NULL REFERENCES analyses(id),
    side TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
    entry_price NUMERIC(5,4) NOT NULL,
    position_size_usd NUMERIC NOT NULL,
    shares NUMERIC NOT NULL,
    kelly_inputs JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'closed')),
    exit_price NUMERIC(5,4),
    exit_reason TEXT CHECK (exit_reason IN ('resolution', 'early_exit', 'stop_loss')),
    pnl NUMERIC,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS model_performance (
    id SERIAL PRIMARY KEY,
    model_name TEXT UNIQUE NOT NULL,
    resolved_count INT NOT NULL DEFAULT 0,
    brier_score_ema NUMERIC(6,4) NOT NULL DEFAULT 0.25,
    trust_weight NUMERIC(4,3) NOT NULL DEFAULT 0.333,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS system_state (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    bankroll NUMERIC NOT NULL,
    total_deployed NUMERIC NOT NULL DEFAULT 0,
    daily_pnl NUMERIC NOT NULL DEFAULT 0,
    kelly_mult NUMERIC(4,3) NOT NULL DEFAULT 0.250,
    edge_threshold NUMERIC(4,3) NOT NULL DEFAULT 0.050,
    category_scores JSONB NOT NULL DEFAULT '{}',
    calibration_corrections JSONB NOT NULL DEFAULT '{}',
    last_scan_at TIMESTAMPTZ,
    circuit_breaker_until TIMESTAMPTZ
);

-- Initialize model performance rows
INSERT INTO model_performance (model_name, trust_weight) VALUES
    ('claude-haiku-4.5', 0.333),
    ('gpt-5.4-mini', 0.333),
    ('gemini-3-flash', 0.333)
ON CONFLICT (model_name) DO NOTHING;

-- v2: Strategy column on trades
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT
    CHECK (strategy IN ('arbitrage', 'snipe', 'forecast')) NOT NULL DEFAULT 'forecast';

-- v2: Strategy performance tracking
CREATE TABLE IF NOT EXISTS strategy_performance (
    id SERIAL PRIMARY KEY,
    strategy TEXT UNIQUE NOT NULL,
    total_trades INT NOT NULL DEFAULT 0,
    winning_trades INT NOT NULL DEFAULT 0,
    total_pnl NUMERIC NOT NULL DEFAULT 0,
    avg_edge NUMERIC(5,4) NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO strategy_performance (strategy) VALUES
    ('arbitrage'), ('snipe'), ('forecast')
ON CONFLICT (strategy) DO NOTHING;

-- v2: Market relationships for arbitrage detection
CREATE TABLE IF NOT EXISTS market_relationships (
    id SERIAL PRIMARY KEY,
    group_id TEXT NOT NULL,
    market_id INT NOT NULL REFERENCES markets(id),
    relationship_type TEXT NOT NULL
        CHECK (relationship_type IN ('exhaustive_group', 'temporal_subset', 'complement')),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(group_id, market_id)
);

-- v2: Post-breaker cooldown tracking
ALTER TABLE system_state ADD COLUMN IF NOT EXISTS post_breaker_until TIMESTAMPTZ;

-- v2.1: CLOB order tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS clob_order_id TEXT;

-- v2.1: Expand trade status for dry-run and fill tracking
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_status_check;
ALTER TABLE trades ADD CONSTRAINT trades_status_check
    CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'closed',
                      'dry_run', 'dry_run_resolved'));

-- v2.3: Expand exit_reason for time-stop and arb TTL exits
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;
ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IN ('resolution', 'early_exit', 'stop_loss', 'take_profit', 'time_stop', 'arb_ttl_expired'));

-- v2.4: Learning system — per-strategy learned parameters
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS learned_params JSONB NOT NULL DEFAULT '{}';

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_markets_resolution ON markets(resolution_time);
CREATE INDEX IF NOT EXISTS idx_markets_polymarket_id ON markets(polymarket_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_market_id ON analyses(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_timestamp ON analyses(timestamp);
CREATE INDEX IF NOT EXISTS idx_market_relationships_group ON market_relationships(group_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_clob_order_id ON trades(clob_order_id);
