-- v10 Phase B — Live Sports calibration table
-- Per spec §5 Loop 2. Stores (sport, bucket, predicted_prob, realized_outcome)
-- observations. Refit hourly by the calibration daemon (PR C).

CREATE TABLE IF NOT EXISTS sport_calibration (
    id SERIAL PRIMARY KEY,
    sport TEXT NOT NULL,
    bucket_key TEXT NOT NULL,
    predicted_prob NUMERIC(5,4) NOT NULL CHECK (predicted_prob >= 0 AND predicted_prob <= 1),
    realized_outcome SMALLINT NOT NULL CHECK (realized_outcome IN (0, 1)),
    game_id TEXT,
    trade_id INT REFERENCES trades(id) ON DELETE SET NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sport_calibration_sport_bucket
    ON sport_calibration (sport, bucket_key, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_sport_calibration_observed_at
    ON sport_calibration (observed_at DESC);
