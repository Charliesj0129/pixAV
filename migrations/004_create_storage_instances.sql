-- 004_create_storage_instances.sql
CREATE TABLE IF NOT EXISTS storage_instances (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    capacity_bytes  BIGINT NOT NULL DEFAULT 0,
    used_bytes      BIGINT NOT NULL DEFAULT 0,
    health          TEXT NOT NULL DEFAULT 'healthy'
                        CHECK (health IN ('healthy', 'degraded', 'full', 'offline')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_storage_account ON storage_instances (account_id);
CREATE INDEX idx_storage_health ON storage_instances (health);

-- Back-reference from accounts
ALTER TABLE accounts
    ADD CONSTRAINT fk_accounts_storage
    FOREIGN KEY (storage_instance_id)
    REFERENCES storage_instances (id)
    ON DELETE SET NULL;
