-- 001_create_accounts.sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS accounts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email       TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'cooldown', 'banned', 'unverified')),
    storage_instance_id UUID,
    last_used_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_accounts_status ON accounts (status);
CREATE INDEX idx_accounts_last_used ON accounts (last_used_at);
