-- v12 migration: snipe-only architecture.
-- Additive only. No DROPs. Old strategy rows stay as historical record.

-- 1. Shadow signal log: every entry candidate, regardless of fill outcome.
CREATE TABLE IF NOT EXISTS shadow_signal (
    id                  serial PRIMARY KEY,
    polymarket_id       text NOT NULL,
    yes_price           numeric(7,6) NOT NULL,
    hours_remaining     numeric(8,2) NOT NULL,
    side                text NOT NULL CHECK (side IN ('YES','NO')),
    buy_price           numeric(7,6) NOT NULL,
    verifier_verdict    text,
    verifier_confidence numeric(4,3),
    verifier_reason     text,
    passed_filter       boolean NOT NULL DEFAULT false,
    fill_attempted      boolean NOT NULL DEFAULT false,
    filled              boolean NOT NULL DEFAULT false,
    reject_reason       text,
    resolved_outcome    smallint,
    hypothetical_pnl    numeric(12,4),
    realized_pnl        numeric(12,4),
    signaled_at         timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz
);

CREATE INDEX IF NOT EXISTS idx_shadow_signal_resolved
    ON shadow_signal(resolved_at)
    WHERE resolved_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_shadow_signal_polymarket
    ON shadow_signal(polymarket_id, signaled_at DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_signal_signaled_at
    ON shadow_signal(signaled_at DESC);

-- 2. Enrich trade_outcome with verifier provenance so a closed trade's
--    "why we took it" is queryable forever.
ALTER TABLE trade_outcome
    ADD COLUMN IF NOT EXISTS verifier_confidence numeric(4,3),
    ADD COLUMN IF NOT EXISTS verifier_reason     text;

-- 3. Rolling hit-rate gauge persisted in system_state. Survives restart.
ALTER TABLE system_state
    ADD COLUMN IF NOT EXISTS rolling_hit_rate       numeric(5,4),
    ADD COLUMN IF NOT EXISTS rolling_hit_rate_n     integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS killswitch_tripped_at  timestamptz,
    ADD COLUMN IF NOT EXISTS killswitch_reason      text;

-- 4. Ensure 'snipe' strategy_performance row exists (idempotent).
INSERT INTO strategy_performance (strategy)
    VALUES ('snipe')
    ON CONFLICT (strategy) DO NOTHING;
