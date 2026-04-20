-- v10 Phase C — trade_outcome table per spec §5 Loop 1
-- Append-only record of every position close. Read by Kelly scaler,
-- edge-decay monitor, calibrator refit, and weekly reflection.

CREATE TABLE IF NOT EXISTS trade_outcome (
    id SERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    market_id INT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    market_category TEXT NOT NULL DEFAULT 'unknown',
    entry_price NUMERIC(5,4) NOT NULL,
    exit_price NUMERIC(5,4),
    pnl NUMERIC(12,4),

    -- Predicted state at entry (used by Kelly scaler)
    predicted_prob NUMERIC(5,4),
    realized_outcome SMALLINT CHECK (realized_outcome IS NULL OR realized_outcome IN (0, 1)),

    -- Live Sports only (maps to sport_calibration bucket)
    game_state_bucket TEXT,

    -- Snipe only (T0 / T1)
    tier SMALLINT,

    kelly_inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    exit_reason TEXT NOT NULL,
    duration_minutes NUMERIC(10,2) NOT NULL DEFAULT 0,

    closed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_outcome_strategy_closed
    ON trade_outcome (strategy, closed_at DESC);

CREATE INDEX IF NOT EXISTS idx_trade_outcome_strategy_category
    ON trade_outcome (strategy, market_category, closed_at DESC);

-- Kelly scaler stored on strategy_performance
ALTER TABLE strategy_performance
    ADD COLUMN IF NOT EXISTS kelly_scaler NUMERIC(5,3) NOT NULL DEFAULT 1.0;

-- Edge-decay disable flag (transient, 48h)
ALTER TABLE strategy_performance
    ADD COLUMN IF NOT EXISTS decay_disabled_until TIMESTAMPTZ;
