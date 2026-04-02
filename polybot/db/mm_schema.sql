-- Market-making schema additions

CREATE TABLE IF NOT EXISTS mm_orders (
    id SERIAL PRIMARY KEY,
    market_id INT NOT NULL REFERENCES markets(id),
    polymarket_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    clob_order_id TEXT UNIQUE NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price NUMERIC(6,4) NOT NULL,
    size NUMERIC NOT NULL,
    size_filled NUMERIC NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'live',
    posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mm_inventory (
    id SERIAL PRIMARY KEY,
    polymarket_id TEXT UNIQUE NOT NULL,
    yes_shares NUMERIC NOT NULL DEFAULT 0,
    no_shares NUMERIC NOT NULL DEFAULT 0,
    net_delta NUMERIC NOT NULL DEFAULT 0,
    cost_basis NUMERIC NOT NULL DEFAULT 0,
    realized_pnl NUMERIC NOT NULL DEFAULT 0,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mm_fills (
    id SERIAL PRIMARY KEY,
    mm_order_id INT NOT NULL REFERENCES mm_orders(id),
    polymarket_id TEXT NOT NULL,
    side TEXT NOT NULL,
    fill_price NUMERIC(6,4) NOT NULL,
    fill_size NUMERIC NOT NULL,
    filled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mm_daily_stats (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    polymarket_id TEXT NOT NULL,
    spread_pnl NUMERIC NOT NULL DEFAULT 0,
    maker_rebates NUMERIC NOT NULL DEFAULT 0,
    liquidity_rewards NUMERIC NOT NULL DEFAULT 0,
    fills_count INT NOT NULL DEFAULT 0,
    quotes_placed INT NOT NULL DEFAULT 0,
    UNIQUE(date, polymarket_id)
);

CREATE INDEX IF NOT EXISTS idx_mm_orders_status ON mm_orders(status);
CREATE INDEX IF NOT EXISTS idx_mm_orders_polymarket_id ON mm_orders(polymarket_id);
CREATE INDEX IF NOT EXISTS idx_mm_orders_clob_order_id ON mm_orders(clob_order_id);
CREATE INDEX IF NOT EXISTS idx_mm_inventory_polymarket_id ON mm_inventory(polymarket_id);
CREATE INDEX IF NOT EXISTS idx_mm_fills_polymarket_id ON mm_fills(polymarket_id);
CREATE INDEX IF NOT EXISTS idx_mm_daily_stats_date ON mm_daily_stats(date);

INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled, learned_params)
VALUES ('market_maker', 0, 0, 0, 0, true, '{}')
ON CONFLICT (strategy) DO NOTHING;
