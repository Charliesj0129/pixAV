-- Additive recovery bookkeeping. Existing facts and operation identities remain.
ALTER TABLE executions ADD COLUMN recovery_count integer NOT NULL DEFAULT 0 CHECK (recovery_count >= 0);
ALTER TABLE executions ADD COLUMN lease_seconds integer NOT NULL DEFAULT 120 CHECK (lease_seconds > 0);
ALTER TABLE executions ADD COLUMN failure_class text;
ALTER TABLE executions ADD COLUMN error_code text;
ALTER TABLE source_observations ADD COLUMN policy_version text NOT NULL DEFAULT 'source-policy-v1';

-- Fence stale legacy task IDs for an admitted video as well as the managed task
-- ID. Lock the same video as admission so check/start cannot race ownership.
CREATE OR REPLACE FUNCTION guard_managed_task() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_video uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.video_id IS DISTINCT FROM OLD.video_id THEN
        RAISE EXCEPTION 'task video identity is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN target_video := OLD.video_id; ELSE target_video := NEW.video_id; END IF;
    PERFORM id FROM videos WHERE id=target_video FOR UPDATE;
    IF EXISTS (SELECT FROM workflow_tasks WHERE video_id=target_video)
       AND NOT pg_has_role(current_user, 'pixav_execution_authority', 'member') THEN
        RAISE EXCEPTION 'managed task requires execution authority' USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER managed_task_insert_guard BEFORE INSERT ON tasks
FOR EACH ROW EXECUTE FUNCTION guard_managed_task();

-- Honor the authority's lease duration, including the storage stage. Heartbeats
-- only renew ownership; they never advance stages or schedule retries.
CREATE OR REPLACE FUNCTION heartbeat_activity(attempt uuid, token uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE executions e SET lease_until=now()+make_interval(secs=>e.lease_seconds)
      FROM activity_attempts a WHERE a.id=attempt AND a.worker_token=token
       AND a.execution_id=e.id AND a.generation=e.generation AND a.owner=e.owner
       AND e.state='RUNNING' AND e.lease_until > now();
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed = 1 THEN
        UPDATE account_leases l SET lease_until=e.lease_until, heartbeat_at=now()
          FROM activity_attempts a JOIN executions e ON e.id=a.execution_id
         WHERE a.id=attempt AND l.execution_id=e.id
           AND l.generation=a.generation AND l.owner=a.owner;
    END IF;
    RETURN changed=1;
END $$;
