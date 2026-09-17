-- 007_unattended_pipeline.sql
-- Durable retries, local-file retention, and replay audit support.

ALTER TABLE videos
ADD COLUMN IF NOT EXISTS local_cleanup_after TIMESTAMPTZ;

ALTER TABLE tasks
ADD COLUMN IF NOT EXISTS retry_not_before TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_tasks_pending_due
    ON tasks (retry_not_before, created_at)
 WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS idx_videos_local_cleanup_due
    ON videos (local_cleanup_after)
 WHERE local_path IS NOT NULL
   AND local_cleanup_after IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_replay_audit (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id          UUID NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    previous_state   TEXT NOT NULL,
    previous_retries INTEGER NOT NULL,
    requested_by     TEXT NOT NULL DEFAULT 'operator',
    reason           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_replay_audit_task
    ON task_replay_audit (task_id, created_at DESC);
