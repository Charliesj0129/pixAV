-- 002_create_videos.sql
CREATE TABLE IF NOT EXISTS videos (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title         TEXT NOT NULL,
    magnet_uri    TEXT,
    local_path    TEXT,
    share_url     TEXT,
    cdn_url       TEXT,
    embedding     vector(1536),
    status        TEXT NOT NULL DEFAULT 'discovered'
                      CHECK (status IN ('discovered', 'downloading', 'downloaded',
                                        'uploading', 'available', 'expired', 'failed')),
    metadata_json JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

CREATE INDEX idx_videos_status ON videos (status);
CREATE INDEX idx_videos_title ON videos USING gin (to_tsvector('english', title));
CREATE INDEX idx_videos_embedding
    ON videos USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
