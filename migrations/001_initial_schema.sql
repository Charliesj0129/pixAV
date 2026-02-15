-- 001_initial_schema.sql
-- Consolidated schema for pixAV project.
-- Contains all tables, extensions, and indices as of 2026-02-15.

-- 1. Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. Storage Instances
-- Tracks physical storage buckets (e.g. Google Photos accounts)
CREATE TABLE IF NOT EXISTS storage_instances (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL, -- references accounts(id), constraint added later
    capacity_bytes  BIGINT NOT NULL DEFAULT 0,
    used_bytes      BIGINT NOT NULL DEFAULT 0,
    health          TEXT NOT NULL DEFAULT 'healthy'
                        CHECK (health IN ('healthy', 'degraded', 'full', 'offline')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_storage_health ON storage_instances (health);


-- 3. Accounts
-- Google accounts used for uploading.
CREATE TABLE IF NOT EXISTS accounts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email       TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'cooldown', 'banned', 'unverified')),
    storage_instance_id UUID REFERENCES storage_instances (id) ON DELETE SET NULL,
    
    -- Runtime Controls / Quotas
    daily_uploaded_bytes BIGINT NOT NULL DEFAULT 0,
    daily_quota_bytes    BIGINT NOT NULL DEFAULT 21474836480, -- 20GB default
    quota_reset_at       TIMESTAMPTZ NOT NULL DEFAULT (date_trunc('day', now()) + interval '1 day'),
    
    last_used_at     TIMESTAMPTZ,
    cooldown_until   TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_accounts_status ON accounts (status);
CREATE INDEX idx_accounts_last_used ON accounts (last_used_at);
CREATE INDEX idx_accounts_cooldown_until ON accounts (cooldown_until);
CREATE INDEX idx_accounts_lease_expires ON accounts (lease_expires_at);

-- Circular FK for storage_instances -> accounts
ALTER TABLE storage_instances
    ADD CONSTRAINT fk_storage_accounts
    FOREIGN KEY (account_id)
    REFERENCES accounts (id)
    ON DELETE CASCADE;


-- 4. Videos
-- Core media entity.
CREATE TABLE IF NOT EXISTS videos (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title         TEXT NOT NULL,
    magnet_uri    TEXT,
    info_hash     VARCHAR(40),
    local_path    TEXT,
    share_url     TEXT,
    cdn_url       TEXT,
    
    -- Metadata
    metadata_json JSONB,
    tags          TEXT[] DEFAULT '{}',
    quality_score INTEGER DEFAULT 0,
    
    -- Search
    -- 384 dimensions for paraphrase-multilingual-MiniLM-L12-v2
    embedding     vector(384), 
    search_text   tsvector,
    
    status        TEXT NOT NULL DEFAULT 'discovered'
                      CHECK (status IN ('discovered', 'downloading', 'downloaded',
                                        'uploading', 'available', 'expired', 'failed')),
    
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_videos_info_hash ON videos(info_hash);
CREATE INDEX idx_videos_status ON videos (status);
CREATE INDEX idx_videos_tags ON videos USING gin(tags);

-- Full Text Search Index (GIN)
CREATE INDEX idx_videos_search_text ON videos USING GIN (search_text);
-- Trigram Index for fuzzy title matching (e.g. partial codes)
CREATE INDEX idx_videos_title_trgm ON videos USING GIN (title gin_trgm_ops);
-- HNSW Vector Index (Cosine Similarity)
CREATE INDEX idx_videos_embedding ON videos USING hnsw (embedding vector_cosine_ops);


-- 5. Tasks
-- Async processing units.
CREATE TABLE IF NOT EXISTS tasks (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    video_id      UUID NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    account_id    UUID REFERENCES accounts (id) ON DELETE SET NULL,
    
    state         TEXT NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending', 'downloading', 'remuxing',
                                       'uploading', 'verifying', 'complete', 'failed')),
    queue_name    TEXT NOT NULL DEFAULT '',
    
    retries       INTEGER NOT NULL DEFAULT 0,
    max_retries   INTEGER NOT NULL DEFAULT 3,
    error_message TEXT,
    
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

CREATE INDEX idx_tasks_state ON tasks (state);
CREATE INDEX idx_tasks_video ON tasks (video_id);
CREATE INDEX idx_tasks_account ON tasks (account_id);


-- 6. Triggers
-- Automatically maintain search_text from title + tags
CREATE OR REPLACE FUNCTION videos_search_text_update() RETURNS trigger AS $$
BEGIN
  -- Combine title (Weight A) and tags (Weight B) into search_text.
  -- We use 'simple' config to avoid over-aggressive stemming which ruins AV codes.
  NEW.search_text :=
      setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'A') ||
      setweight(to_tsvector('simple', array_to_string(coalesce(NEW.tags, '{}'), ' ')), 'B');
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tsvectorupdate ON videos;
CREATE TRIGGER tsvectorupdate BEFORE INSERT OR UPDATE
ON videos FOR EACH ROW EXECUTE FUNCTION videos_search_text_update();
