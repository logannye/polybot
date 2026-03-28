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
    ('claude-sonnet-4.6', 0.333),
    ('gpt-4o', 0.333),
    ('gemini-2.5-flash', 0.333)
ON CONFLICT (model_name) DO NOTHING;

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_markets_resolution ON markets(resolution_time);
CREATE INDEX IF NOT EXISTS idx_markets_polymarket_id ON markets(polymarket_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_market_id ON analyses(market_id);
CREATE INDEX IF NOT EXISTS idx_analyses_timestamp ON analyses(timestamp);
