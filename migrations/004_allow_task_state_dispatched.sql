-- 004_allow_task_state_dispatched.sql
-- Add 'dispatched' state so Maxwell-Core can claim+enqueue without marking
-- tasks as worker-executing transient states.

ALTER TABLE tasks
DROP CONSTRAINT IF EXISTS tasks_state_check;

ALTER TABLE tasks
ADD CONSTRAINT tasks_state_check
CHECK (
    state IN (
        'pending',
        'dispatched',
        'downloading',
        'remuxing',
        'uploading',
        'verifying',
        'complete',
        'failed'
    )
);
