-- 003_add_task_trace_and_runtime_fields.sql
-- Align tasks table with shared Task model contract.
-- - trace_id: persisted cross-module correlation ID
-- - local_path/share_url: optional task-level snapshots for handoff/debugging

ALTER TABLE tasks
ADD COLUMN IF NOT EXISTS local_path TEXT;

ALTER TABLE tasks
ADD COLUMN IF NOT EXISTS share_url TEXT;

ALTER TABLE tasks
ADD COLUMN IF NOT EXISTS trace_id TEXT;

UPDATE tasks
   SET trace_id = uuid_generate_v4()::text
 WHERE trace_id IS NULL
    OR btrim(trace_id) = '';

ALTER TABLE tasks
ALTER COLUMN trace_id SET DEFAULT uuid_generate_v4()::text;

ALTER TABLE tasks
ALTER COLUMN trace_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_trace_id ON tasks (trace_id);
