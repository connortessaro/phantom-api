CREATE TABLE IF NOT EXISTS api_keys (
    key_hash            TEXT PRIMARY KEY,
    credit_balance      INTEGER NOT NULL,       -- micro-USD remaining
    credit_spent        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id          TEXT PRIMARY KEY,
    xmr_address         TEXT NOT NULL,
    xmr_subaddr_index   INTEGER NOT NULL DEFAULT 0,
    xmr_amount          TEXT NOT NULL,           -- decimal string; never float
    credit_micro_usd    INTEGER NOT NULL,        -- credits user receives on success
    bundle_name         TEXT NOT NULL,           -- bundle name or 'custom'
    validity_days       INTEGER NOT NULL DEFAULT 90,
    status              TEXT NOT NULL DEFAULT 'pending',
    -- status: pending, confirming, ready, completed, expired
    key_hash            TEXT,
    created_at          TEXT NOT NULL,
    confirmed_at        TEXT,
    expires_at          TEXT NOT NULL,
    -- NowPayments rail (nullable for legacy XMR-direct rows):
    np_invoice_id       TEXT,                    -- NowPayments invoice id
    np_payment_id       TEXT,                    -- NowPayments internal payment id (arrives on first IPN)
    pay_currency        TEXT,                    -- coin customer chose (btc/eth/xmr/usdt/...)
    pay_amount          TEXT,                    -- decimal string in pay_currency
    outcome_amount      TEXT,                    -- XMR we received post-conversion
    parent_payment_id   TEXT                     -- set on re-deposits
);

-- Add columns idempotently for existing DBs (SQLite ignores duplicate columns on schema reload
-- if the table was just created above, but tolerates failures here for upgrades).
-- These ALTERs are wrapped to be safe to re-run.

CREATE TABLE IF NOT EXISTS usage_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash            TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_address ON payments(xmr_address);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_log(key_hash);
