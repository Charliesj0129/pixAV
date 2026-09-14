-- Additive, isolated single-film manifest. No existing single-file row changes.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS manifest_version integer;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS expected_part_count integer;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_provenance jsonb;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS playback_manifest_version integer;

CREATE TABLE IF NOT EXISTS video_parts (
    video_id uuid NOT NULL REFERENCES videos(id),
    part_index integer NOT NULL CHECK (part_index >= 0),
    manifest_version integer NOT NULL CHECK (manifest_version > 0),
    start_seconds double precision NOT NULL CHECK (start_seconds >= 0),
    end_seconds double precision NOT NULL CHECK (end_seconds > start_seconds),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0 AND size_bytes < 10000000000),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    filename text NOT NULL CHECK (filename ~ '^pixav-[a-f0-9-]+-part-[0-9]+-[a-f0-9]{16}\.mp4$'),
    media_info jsonb NOT NULL,
    share_url text,
    account_id uuid REFERENCES accounts(id),
    state text NOT NULL DEFAULT 'prepared' CHECK (state IN
        ('prepared', 'upload_intent', 'reconcile', 'user_action_required',
         'quota_wait', 'backed_up', 'verified', 'failed')),
    recovery jsonb NOT NULL DEFAULT '{}',
    verification jsonb NOT NULL DEFAULT '{}',
    uploaded_at timestamptz,
    usage_counted_at timestamptz,
    retry_not_before timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (video_id, part_index)
);
CREATE INDEX IF NOT EXISTS video_parts_account_pending ON video_parts(account_id)
    WHERE state IN ('upload_intent', 'reconcile', 'user_action_required');

-- Single-film execution state is also in PostgreSQL; JSON files are evidence only.
CREATE TABLE IF NOT EXISTS first_4k_runs (
    id uuid PRIMARY KEY,
    document jsonb NOT NULL,
    next_action_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
