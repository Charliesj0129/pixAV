-- Expand only. Retain the legacy journal and every original operation/generation.
-- READY requires separate live verification; migration never promotes a runtime.
CREATE TABLE upload_runtimes (
    runtime_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    owner uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('redroid','windows_android')),
    account_id uuid REFERENCES accounts(id),
    device_key text NOT NULL CHECK (length(btrim(device_key)) > 0),
    profile text NOT NULL CHECK (length(btrim(profile)) > 0),
    identity jsonb NOT NULL CHECK (jsonb_typeof(identity) = 'object'),
    identity_sha256 text NOT NULL CHECK (identity_sha256 ~ '^[a-f0-9]{64}$'),
    tools_id text,
    tools_image text NOT NULL CHECK (length(btrim(tools_image)) > 0),
    staging_root text NOT NULL CHECK (length(btrim(staging_root)) > 0),
    state text NOT NULL CHECK (state IN ('REGISTERED','READY','REVIEW_REQUIRED','RETIRED')),
    legacy_owner uuid REFERENCES guest_runtimes(owner),
    review_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (runtime_id,generation),
    CHECK (state <> 'READY' OR (account_id IS NOT NULL AND tools_id IS NOT NULL)),
    CHECK (state <> 'REVIEW_REQUIRED' OR length(btrim(review_reason)) > 0),
    CHECK (kind <> 'windows_android' OR legacy_owner IS NULL)
);

INSERT INTO upload_runtimes(runtime_id,generation,owner,kind,account_id,device_key,profile,
                            identity,identity_sha256,tools_id,tools_image,staging_root,
                            state,legacy_owner,review_reason)
SELECT owner,1,owner,'redroid',account_id,'legacy-owner:' || owner::text,profile,
       jsonb_build_object('schema','retained-redroid-runtime-v1','guest_id',guest_id,
                          'guest_image',guest_image,'tools_id',tools_id),
       encode(sha256(convert_to(jsonb_build_object('schema','retained-redroid-runtime-v1',
              'guest_id',guest_id,'guest_image',guest_image,'tools_id',tools_id)::text,'UTF8')),'hex'),
       tools_id,tools_image,staging_root,'REVIEW_REQUIRED',owner,'MIGRATION_REQUIRES_RECONCILIATION'
  FROM guest_runtimes;

CREATE TABLE photos_ui_sessions (
    id uuid PRIMARY KEY,
    token uuid NOT NULL,
    runtime_id uuid NOT NULL,
    runtime_generation bigint NOT NULL,
    runtime_owner uuid NOT NULL,
    account_id uuid NOT NULL REFERENCES accounts(id),
    device_key text NOT NULL,
    role text NOT NULL CHECK (role IN ('upload','cold_read','observe')),
    execution_id uuid NOT NULL REFERENCES executions(id),
    attempt_id uuid NOT NULL REFERENCES activity_attempts(id),
    operation_id uuid NOT NULL REFERENCES operation_intents(id),
    generation bigint NOT NULL CHECK (generation > 0),
    owner uuid NOT NULL,
    worker_token uuid NOT NULL,
    state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','HALT_REQUIRED','STOPPED')),
    lease_until timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    stopped_at timestamptz,
    halt_evidence jsonb,
    FOREIGN KEY(runtime_id,runtime_generation) REFERENCES upload_runtimes(runtime_id,generation),
    CHECK ((state = 'STOPPED') = (stopped_at IS NOT NULL)),
    CHECK (state <> 'STOPPED' OR jsonb_typeof(halt_evidence) = 'object')
);
-- A missed heartbeat never authorizes stealing an external writer's device.
CREATE UNIQUE INDEX photos_ui_one_account ON photos_ui_sessions(account_id)
    WHERE state IN ('ACTIVE','HALT_REQUIRED');
CREATE UNIQUE INDEX photos_ui_one_device ON photos_ui_sessions(device_key)
    WHERE state IN ('ACTIVE','HALT_REQUIRED');
CREATE UNIQUE INDEX photos_ui_one_runtime_owner ON photos_ui_sessions(runtime_owner)
    WHERE state IN ('ACTIVE','HALT_REQUIRED');
CREATE UNIQUE INDEX photos_ui_one_upload ON photos_ui_sessions(role)
    WHERE role = 'upload' AND state IN ('ACTIVE','HALT_REQUIRED');

CREATE FUNCTION guard_upload_runtime_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.runtime_id,NEW.generation,NEW.owner,NEW.kind,NEW.account_id,NEW.device_key,
           NEW.profile,NEW.identity,NEW.identity_sha256,NEW.tools_id,NEW.tools_image,
           NEW.staging_root,NEW.legacy_owner)
       IS DISTINCT FROM
       ROW(OLD.runtime_id,OLD.generation,OLD.owner,OLD.kind,OLD.account_id,OLD.device_key,
           OLD.profile,OLD.identity,OLD.identity_sha256,OLD.tools_id,OLD.tools_image,
           OLD.staging_root,OLD.legacy_owner) THEN
        RAISE EXCEPTION 'runtime identity requires a new generation' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER upload_runtime_identity_immutable BEFORE UPDATE ON upload_runtimes
    FOR EACH ROW EXECUTE FUNCTION guard_upload_runtime_identity();

REVOKE ALL ON upload_runtimes,photos_ui_sessions FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON upload_runtimes TO pixav_execution_authority;
GRANT SELECT,INSERT,UPDATE ON photos_ui_sessions TO pixav_execution_authority;
GRANT SELECT ON upload_runtimes TO pixav_activity_worker;
GRANT SELECT(id,runtime_id,runtime_generation,runtime_owner,account_id,device_key,role,
             execution_id,attempt_id,operation_id,generation,owner,state,lease_until,
             heartbeat_at,created_at,stopped_at,halt_evidence)
    ON photos_ui_sessions TO pixav_activity_worker;

CREATE FUNCTION acquire_photos_ui_session(report jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE runtime upload_runtimes%ROWTYPE; deadline timestamptz; existing photos_ui_sessions%ROWTYPE;
BEGIN
    IF report->>'role' NOT IN ('upload','cold_read','observe')
       OR report->>'id' IS NULL OR report->>'token' IS NULL THEN RETURN false; END IF;
    SELECT * INTO runtime FROM upload_runtimes
     WHERE runtime_id=(report->>'runtime_id')::uuid
       AND generation=(report->>'runtime_generation')::bigint
       AND owner=(report->>'runtime_owner')::uuid
       AND account_id=(report->>'account_id')::uuid AND state='READY' AND kind='windows_android'
     FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT e.lease_until INTO deadline FROM executions e
      JOIN activity_attempts a ON a.execution_id=e.id
      JOIN operation_intents o ON o.id=a.operation_id AND o.execution_id=e.id
     WHERE e.id=(report->>'execution_id')::uuid AND a.id=(report->>'attempt_id')::uuid
       AND a.operation_id=(report->>'operation_id')::uuid
       AND a.generation=(report->>'generation')::bigint AND e.generation=a.generation
       AND a.owner=(report->>'owner')::uuid AND e.owner=a.owner
       AND a.worker_token=(report->>'worker_token')::uuid
       AND e.state='RUNNING' AND e.lease_until > clock_timestamp()
       AND a.stage=e.stage AND o.stage=e.stage
       AND ((report->>'role'='upload' AND e.stage='upload')
            OR (report->>'role' IN ('cold_read','observe') AND e.stage='verify'))
     FOR UPDATE OF e;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO existing FROM photos_ui_sessions WHERE id=(report->>'id')::uuid;
    IF FOUND THEN
        RETURN existing.token=(report->>'token')::uuid AND existing.state='ACTIVE'
           AND existing.lease_until > clock_timestamp()
           AND existing.runtime_id=runtime.runtime_id AND existing.runtime_generation=runtime.generation
           AND existing.account_id=runtime.account_id AND existing.device_key=runtime.device_key
           AND existing.role=report->>'role' AND existing.attempt_id=(report->>'attempt_id')::uuid
           AND existing.operation_id=(report->>'operation_id')::uuid
           AND existing.generation=(report->>'generation')::bigint
           AND existing.worker_token=(report->>'worker_token')::uuid;
    END IF;
    INSERT INTO photos_ui_sessions(id,token,runtime_id,runtime_generation,runtime_owner,
        account_id,device_key,role,execution_id,attempt_id,operation_id,generation,owner,
        worker_token,lease_until)
    VALUES((report->>'id')::uuid,(report->>'token')::uuid,runtime.runtime_id,runtime.generation,
        runtime.owner,runtime.account_id,runtime.device_key,report->>'role',
        (report->>'execution_id')::uuid,(report->>'attempt_id')::uuid,(report->>'operation_id')::uuid,
        (report->>'generation')::bigint,(report->>'owner')::uuid,(report->>'worker_token')::uuid,
        LEAST(deadline,clock_timestamp()+interval '60 seconds'));
    RETURN true;
EXCEPTION WHEN unique_violation THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION acquire_photos_ui_session(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION acquire_photos_ui_session(jsonb)
    TO pixav_activity_worker,pixav_execution_authority;

CREATE FUNCTION heartbeat_photos_ui_session(subject uuid,secret uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE photos_ui_sessions s SET heartbeat_at=clock_timestamp(),
           lease_until=LEAST(e.lease_until,clock_timestamp()+interval '60 seconds')
      FROM executions e,activity_attempts a,operation_intents o,upload_runtimes r
     WHERE s.id=subject AND s.token=secret AND s.state='ACTIVE'
       AND s.lease_until > clock_timestamp()
       AND e.id=s.execution_id AND a.id=s.attempt_id AND a.execution_id=e.id
       AND a.operation_id=s.operation_id AND a.worker_token=s.worker_token
       AND o.id=s.operation_id AND o.execution_id=e.id AND o.stage=e.stage AND a.stage=e.stage
       AND a.generation=s.generation AND e.generation=s.generation
       AND a.owner=s.owner AND e.owner=s.owner
       AND r.runtime_id=s.runtime_id AND r.generation=s.runtime_generation
       AND r.state='READY' AND r.kind='windows_android'
       AND r.owner=s.runtime_owner AND r.account_id=s.account_id AND r.device_key=s.device_key
       AND ((s.role='upload' AND e.stage='upload')
            OR (s.role IN ('cold_read','observe') AND e.stage='verify'))
       AND e.state='RUNNING' AND e.lease_until > clock_timestamp();
    GET DIAGNOSTICS changed=ROW_COUNT;
    IF changed=0 THEN
        UPDATE photos_ui_sessions SET state='HALT_REQUIRED'
         WHERE id=subject AND token=secret AND state='ACTIVE';
    END IF;
    RETURN changed=1;
END $$;
REVOKE ALL ON FUNCTION heartbeat_photos_ui_session(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION heartbeat_photos_ui_session(uuid,uuid)
    TO pixav_activity_worker,pixav_execution_authority;

-- Only the authority may submit an independently verified halt receipt. This
-- function binds that receipt; it cannot observe Windows or stop a device itself.
CREATE FUNCTION record_verified_photos_ui_halt(subject uuid,secret uuid,evidence jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE photos_ui_sessions s SET state='STOPPED',stopped_at=clock_timestamp(),halt_evidence=evidence
      FROM upload_runtimes r
     WHERE s.id=subject AND s.token=secret AND s.state IN ('ACTIVE','HALT_REQUIRED')
       AND r.runtime_id=s.runtime_id AND r.generation=s.runtime_generation
       AND evidence->>'schema'='windows-ui-halt-v1'
       AND evidence->>'runtime_id'=s.runtime_id::text
       AND (evidence->>'runtime_generation')::bigint=s.runtime_generation
       AND evidence->>'operation_id'=s.operation_id::text
       AND (evidence->>'generation')::bigint=s.generation
       AND evidence->>'device_key'=s.device_key AND evidence->>'tools_id'=r.tools_id
       AND evidence->>'identity_sha256'=r.identity_sha256
       AND evidence->'runner_stopped'='true'::jsonb
       AND evidence->'native_writers_stopped'='true'::jsonb
       AND evidence->>'receipt_sha256' ~ '^[a-f0-9]{64}$';
    GET DIAGNOSTICS changed=ROW_COUNT;
    RETURN changed=1;
END $$;
REVOKE ALL ON FUNCTION record_verified_photos_ui_halt(uuid,uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_verified_photos_ui_halt(uuid,uuid,jsonb) TO pixav_execution_authority;
