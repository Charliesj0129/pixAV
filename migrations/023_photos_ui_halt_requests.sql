-- Worker reports are reconciliation requests, never proof that writers stopped.
CREATE TABLE photos_ui_halt_requests (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES photos_ui_sessions(id),
    reason text NOT NULL CHECK (reason IN ('UI_SCOPE_ENDED','UI_GUARD_LOST','NATIVE_HALT_UNCONFIRMED')),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON photos_ui_halt_requests FROM PUBLIC;
GRANT SELECT ON photos_ui_halt_requests TO pixav_execution_authority;

CREATE FUNCTION request_photos_ui_halt(subject uuid,secret uuid,report jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE existing photos_ui_halt_requests%ROWTYPE;
BEGIN
    IF report->>'reason' NOT IN ('UI_SCOPE_ENDED','UI_GUARD_LOST','NATIVE_HALT_UNCONFIRMED')
       OR report->>'id' IS NULL OR jsonb_typeof(report->'evidence') IS DISTINCT FROM 'object'
    THEN RETURN false; END IF;
    PERFORM 1 FROM photos_ui_sessions WHERE id=subject AND token=secret
      AND state IN ('ACTIVE','HALT_REQUIRED') FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO existing FROM photos_ui_halt_requests WHERE id=(report->>'id')::uuid;
    IF FOUND THEN
        RETURN existing.session_id=subject AND existing.reason=report->>'reason'
           AND existing.evidence=report->'evidence';
    END IF;
    INSERT INTO photos_ui_halt_requests(id,session_id,reason,evidence)
      VALUES((report->>'id')::uuid,subject,report->>'reason',report->'evidence');
    UPDATE photos_ui_sessions SET state='HALT_REQUIRED'
      WHERE id=subject AND token=secret AND state='ACTIVE';
    RETURN true;
END $$;
REVOKE ALL ON FUNCTION request_photos_ui_halt(uuid,uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION request_photos_ui_halt(uuid,uuid,jsonb) TO pixav_activity_worker;
