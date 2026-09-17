-- Expand only. RemoteAsset durability, execution-owned account leases and the
-- cleanup authorization trail. No legacy upload state is promoted: a share URL
-- written by the legacy pixel_injector path never becomes a durable fact here.
-- Apply using an administrative connection after identity and backup verification.

-- The managed execution authority now also owns remote storage and its
-- verification, so its stage vocabulary grows. Widening a CHECK is expand-only:
-- every existing row already satisfies the new constraint.
ALTER TABLE executions DROP CONSTRAINT executions_stage_check;
ALTER TABLE executions ADD CONSTRAINT executions_stage_check
    CHECK (stage IN ('download','prepare','handoff','upload','verify'));

-- One prepared artifact has at most one remote asset. Durability is a committed
-- transition, never an inference from share_url being present.
CREATE TABLE remote_assets (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos(id),
    artifact_id uuid NOT NULL UNIQUE REFERENCES workflow_artifacts(id),
    provider text NOT NULL DEFAULT 'google_photos' CHECK (provider = 'google_photos'),
    state text NOT NULL DEFAULT 'REQUESTED'
        CHECK (state IN ('REQUESTED','CREATED','VERIFIED','DURABLE','INVALID')),
    policy_version text NOT NULL CHECK (length(btrim(policy_version)) > 0),
    expected jsonb NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    segment_count integer NOT NULL CHECK (segment_count > 0),
    durable_at timestamptz,
    invalidated_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'DURABLE') = (durable_at IS NOT NULL))
);
CREATE INDEX remote_assets_video_state ON remote_assets(video_id, state);

-- A whole-film asset is one segment. Splitting is a transport concern of the
-- provider's per-file limit, so a single segment is the ordinary case and the
-- manifest rules stay identical for one segment or many.
CREATE TABLE remote_asset_segments (
    asset_id uuid NOT NULL REFERENCES remote_assets(id),
    segment_index integer NOT NULL CHECK (segment_index >= 0),
    start_seconds double precision NOT NULL CHECK (start_seconds >= 0),
    end_seconds double precision NOT NULL CHECK (end_seconds > start_seconds),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0 AND size_bytes < 10000000000),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    local_path text NOT NULL,
    media_info jsonb NOT NULL,
    share_url text,
    account_id uuid REFERENCES accounts(id),
    state text NOT NULL DEFAULT 'prepared' CHECK (state IN
        ('prepared','upload_intent','reconcile','user_action_required',
         'quota_wait','backed_up','verified','failed')),
    recovery jsonb NOT NULL DEFAULT '{}',
    verification jsonb NOT NULL DEFAULT '{}',
    uploaded_at timestamptz,
    usage_counted_at timestamptz,
    retry_not_before timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, segment_index),
    -- Quota is charged in the same transaction that records the share location.
    CHECK ((usage_counted_at IS NULL) = (share_url IS NULL))
);
CREATE INDEX remote_asset_segments_account_pending ON remote_asset_segments(account_id)
    WHERE state IN ('upload_intent','reconcile','user_action_required');

-- The lease covers the actual execution, not a wall-clock window a worker
-- chose. A row may only be taken over once its DB-clock deadline has passed.
CREATE TABLE account_leases (
    account_id uuid PRIMARY KEY REFERENCES accounts(id),
    execution_id uuid NOT NULL UNIQUE REFERENCES executions(id),
    generation bigint NOT NULL,
    owner uuid NOT NULL,
    lease_until timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A reader holds one of these for as long as it is serving bytes from a local
-- artifact. Cleanup refuses while any lease is live, and eviction waits for the
-- holder to release rather than pulling the file out from under it.
CREATE TABLE reader_leases (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos(id),
    path text NOT NULL,
    holder text NOT NULL,
    lease_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reader_leases_video ON reader_leases(video_id, lease_until);

-- Every cleanup decision is recorded, including the refusals. A missing row is
-- itself evidence that cleanup never evaluated the artifact.
CREATE TABLE cleanup_audit (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos(id),
    path text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('deleted','rejected','missing','unsafe','failed')),
    applied boolean NOT NULL,
    reasons jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cleanup_audit_video ON cleanup_audit(video_id, created_at DESC);

-- The account lease must cover the actual execution, so it is renewed by the
-- same heartbeat that proves the execution is alive. A worker that stops
-- reporting lets both expire together instead of stranding the account.
CREATE OR REPLACE FUNCTION heartbeat_activity(attempt uuid, token uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE executions e SET lease_until=now()+interval '120 seconds'
      FROM activity_attempts a WHERE a.id=attempt AND a.worker_token=token
       AND a.execution_id=e.id AND a.generation=e.generation AND a.owner=e.owner
       AND e.state='RUNNING' AND e.lease_until > now();
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed = 1 THEN
        UPDATE account_leases l SET lease_until=now()+interval '120 seconds', heartbeat_at=now()
          FROM activity_attempts a WHERE a.id=attempt AND l.execution_id=a.execution_id
           AND l.generation=a.generation AND l.owner=a.owner;
    END IF;
    RETURN changed=1;
END $$;

REVOKE ALL ON remote_assets, remote_asset_segments, account_leases, cleanup_audit, reader_leases FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON account_leases TO pixav_execution_authority;
GRANT SELECT, INSERT, UPDATE ON remote_assets, remote_asset_segments, cleanup_audit
    TO pixav_execution_authority;
GRANT SELECT, DELETE ON reader_leases TO pixav_execution_authority;
GRANT SELECT, UPDATE ON accounts TO pixav_execution_authority;
GRANT SELECT ON remote_assets, remote_asset_segments TO pixav_activity_worker;

-- The activity worker journals its intent before every external effect, but it
-- cannot decide that an upload happened: recovery and state only move forward
-- through this function, and never for a segment whose usage is already counted.
CREATE FUNCTION journal_segment(report jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE remote_asset_segments s
       SET state = report->>'state',
           recovery = report->'recovery',
           account_id = COALESCE((report->>'account_id')::uuid, s.account_id),
           updated_at = now()
      FROM activity_attempts a JOIN executions e ON e.id = a.execution_id
     WHERE s.asset_id = (report->>'asset_id')::uuid
       AND s.segment_index = (report->>'segment_index')::integer
       AND s.usage_counted_at IS NULL
       AND report->>'state' IN ('upload_intent','reconcile','user_action_required')
       AND a.id = (report->>'attempt_id')::uuid
       AND a.worker_token = (report->>'token')::uuid
       AND a.generation = e.generation AND a.owner = e.owner
       AND e.state = 'RUNNING' AND e.lease_until > now();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;
REVOKE ALL ON FUNCTION journal_segment(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION journal_segment(jsonb) TO pixav_activity_worker, pixav_execution_authority;
