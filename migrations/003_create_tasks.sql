-- 003_create_tasks.sql
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
