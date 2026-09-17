-- Expand only. No legacy status is promoted and no task is automatically adopted.
-- Apply using an administrative connection after identity and backup verification.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pixav_execution_authority') THEN
        CREATE ROLE pixav_execution_authority NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pixav_activity_worker') THEN
        CREATE ROLE pixav_activity_worker NOLOGIN;
    END IF;
END $$;

CREATE TABLE workflow_tasks (
    task_id uuid PRIMARY KEY REFERENCES tasks(id),
    video_id uuid NOT NULL REFERENCES videos(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(video_id)
);
CREATE TABLE executions (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES workflow_tasks(task_id),
    state text NOT NULL DEFAULT 'READY' CHECK (state IN
        ('READY','RUNNING','WAITING_RETRY','WAITING_QUOTA','USER_ACTION_REQUIRED','SUCCEEDED','FAILED','CANCELLED')),
    stage text NOT NULL DEFAULT 'download' CHECK (stage IN ('download','prepare','handoff')),
    generation bigint NOT NULL DEFAULT 0,
    owner uuid,
    lease_until timestamptz,
    due_at timestamptz NOT NULL DEFAULT now(),
    blocked_reason text,
    candidate_id uuid REFERENCES source_candidates(id),
    infrastructure_retries integer NOT NULL DEFAULT 0 CHECK (infrastructure_retries >= 0),
    max_retries integer NOT NULL CHECK (max_retries >= 0),
    checkpoint jsonb NOT NULL DEFAULT '{}',
    replay_of uuid REFERENCES executions(id),
    replay_operator text,
    replay_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (replay_of IS NULL OR (length(btrim(replay_operator)) > 0 AND length(btrim(replay_reason)) > 0))
);
CREATE UNIQUE INDEX execution_one_active ON executions(task_id)
WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED');
CREATE TABLE activity_attempts (
    id uuid PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES executions(id),
    generation bigint NOT NULL,
    owner uuid NOT NULL,
    stage text NOT NULL,
    operation_id uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE(execution_id, generation)
);
CREATE TABLE operation_intents (
    id uuid PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES executions(id),
    stage text NOT NULL,
    identity text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(execution_id, stage, identity)
);
ALTER TABLE activity_attempts ADD FOREIGN KEY(operation_id) REFERENCES operation_intents(id);
CREATE TABLE activity_results (
    attempt_id uuid PRIMARY KEY REFERENCES activity_attempts(id),
    payload jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz
);
CREATE TABLE workflow_artifacts (
    id uuid PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES executions(id),
    operation_id uuid NOT NULL REFERENCES operation_intents(id),
    path text NOT NULL,
    facts jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(operation_id)
);
CREATE TABLE storage_handoffs (
    operation_id uuid PRIMARY KEY REFERENCES operation_intents(id),
    execution_id uuid NOT NULL UNIQUE REFERENCES executions(id),
    artifact_id uuid NOT NULL REFERENCES workflow_artifacts(id),
    storage_task_id uuid NOT NULL UNIQUE REFERENCES tasks(id),
    accepted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE source_observations (
    video_id uuid NOT NULL REFERENCES videos(id),
    provider text NOT NULL,
    provider_id text NOT NULL,
    info_hash text NOT NULL CHECK (info_hash ~ '^[a-f0-9]{40}$'),
    observation jsonb NOT NULL,
    eligible boolean NOT NULL,
    score integer NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(video_id, provider, provider_id)
);

-- A legacy process cannot mutate an adopted task, even via direct SQL (orphan
-- cleaner/replay). Authority logins must be provisioned separately; never grant
-- this membership to a legacy service or a worker login.
CREATE FUNCTION guard_managed_task() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT FROM workflow_tasks WHERE task_id = OLD.id)
       AND NOT pg_has_role(current_user, 'pixav_execution_authority', 'member') THEN
        RAISE EXCEPTION 'managed task requires execution authority' USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER managed_task_guard BEFORE UPDATE OR DELETE ON tasks
FOR EACH ROW EXECUTE FUNCTION guard_managed_task();

REVOKE ALL ON workflow_tasks, executions, activity_attempts, operation_intents,
    activity_results, workflow_artifacts, storage_handoffs, source_observations FROM PUBLIC;
GRANT SELECT ON workflow_tasks TO PUBLIC;
GRANT SELECT, INSERT, UPDATE ON workflow_tasks, executions, activity_attempts, operation_intents,
    activity_results, workflow_artifacts, storage_handoffs, source_observations TO pixav_execution_authority;
GRANT SELECT, INSERT, UPDATE ON tasks, videos, source_candidates TO pixav_execution_authority;
GRANT SELECT ON executions, activity_attempts, operation_intents, workflow_artifacts TO pixav_activity_worker;

-- Workers can only insert a result for the exact active lease; they cannot
-- update execution, attempt history, retry timing or an existing result.
CREATE FUNCTION report_activity(report jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE accepted integer;
BEGIN
    PERFORM 1 FROM executions e JOIN activity_attempts a ON a.execution_id = e.id
     WHERE a.id = (report->>'attempt_id')::uuid
       AND e.id = (report->>'execution_id')::uuid
       AND e.task_id = (report->>'task_id')::uuid
       AND a.operation_id = (report->>'operation_id')::uuid
       AND a.owner = (report->>'owner')::uuid AND e.owner = a.owner
       AND a.generation = (report->>'generation')::bigint AND e.generation = a.generation
       AND e.state = 'RUNNING' AND e.lease_until > now()
     FOR UPDATE OF e;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO activity_results(attempt_id,payload) VALUES ((report->>'attempt_id')::uuid, report)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS accepted = ROW_COUNT;
    RETURN accepted = 1;
END $$;
REVOKE ALL ON FUNCTION report_activity(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION report_activity(jsonb) TO pixav_activity_worker, pixav_execution_authority;

ALTER TABLE activity_attempts ADD COLUMN worker_token uuid;
CREATE FUNCTION claim_activity(attempt uuid, token uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE activity_attempts a SET worker_token=token FROM executions e
     WHERE a.id=attempt AND a.execution_id=e.id AND a.generation=e.generation
       AND a.owner=e.owner AND e.state='RUNNING' AND e.lease_until > now()
       AND a.worker_token IS NULL;
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed=1;
END $$;
CREATE FUNCTION heartbeat_activity(attempt uuid, token uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE executions e SET lease_until=now()+interval '120 seconds'
      FROM activity_attempts a WHERE a.id=attempt AND a.worker_token=token
       AND a.execution_id=e.id AND a.generation=e.generation AND a.owner=e.owner
       AND e.state='RUNNING' AND e.lease_until > now();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed=1;
END $$;
REVOKE ALL ON FUNCTION claim_activity(uuid,uuid), heartbeat_activity(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_activity(uuid,uuid), heartbeat_activity(uuid,uuid) TO pixav_activity_worker;
