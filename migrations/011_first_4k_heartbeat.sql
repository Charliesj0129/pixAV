-- Independent liveness columns: document checkpoints must not overwrite them.
ALTER TABLE first_4k_runs ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE first_4k_runs ADD COLUMN IF NOT EXISTS heartbeat_stage text;
ALTER TABLE first_4k_runs ADD COLUMN IF NOT EXISTS stage_started_at timestamptz;
ALTER TABLE first_4k_runs ADD COLUMN IF NOT EXISTS progress_at timestamptz;
ALTER TABLE first_4k_runs ADD COLUMN IF NOT EXISTS current_part_index integer;
