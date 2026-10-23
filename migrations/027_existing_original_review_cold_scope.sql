-- Expand only. An authority-accepted existing-original review performs a
-- fresh cold read in its original upload operation. Preserve every activity,
-- runtime, account, lease and persistent halt binding. No rows are promoted.

CREATE OR REPLACE FUNCTION acquire_photos_ui_session(report jsonb) RETURNS boolean
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
            OR (report->>'role' IN ('cold_read','observe') AND e.stage='verify')
            OR (report->>'role'='cold_read' AND e.stage='playback')
            OR (report->>'role'='cold_read' AND e.stage='upload' AND EXISTS (
                SELECT 1 FROM remote_asset_segments segment
                 WHERE segment.asset_id::text=e.checkpoint->>'asset_id'
                   AND segment.account_id=runtime.account_id
                   AND segment.state IN ('prepared','upload_intent')
                   AND segment.uploaded_at IS NULL AND segment.usage_counted_at IS NULL
                   AND o.identity='upload:'||segment.asset_id::text||':'||segment.segment_index::text
                   AND segment.recovery->'upload_outcome_review'=e.checkpoint->'upload_outcome_review'
                   AND e.checkpoint->'upload_outcome_review'->>'status'='ACCEPTED'
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'version'='upload-outcome-review-v1'
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'outcome'='EXISTING_ORIGINAL'
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'execution_id'=e.id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'operation_id'=o.id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'asset_id'=segment.asset_id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'segment_index'=segment.segment_index::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'account_id'=runtime.account_id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'guest_owner'=runtime.owner::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'segment_sha256'=segment.sha256)))
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

CREATE OR REPLACE FUNCTION heartbeat_photos_ui_session(subject uuid,secret uuid) RETURNS boolean
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
            OR (s.role IN ('cold_read','observe') AND e.stage='verify')
            OR (s.role='cold_read' AND e.stage='playback')
            OR (s.role='cold_read' AND e.stage='upload' AND EXISTS (
                SELECT 1 FROM remote_asset_segments segment
                 WHERE segment.asset_id::text=e.checkpoint->>'asset_id'
                   AND segment.account_id=r.account_id
                   AND segment.state IN ('prepared','upload_intent')
                   AND segment.uploaded_at IS NULL AND segment.usage_counted_at IS NULL
                   AND o.identity='upload:'||segment.asset_id::text||':'||segment.segment_index::text
                   AND segment.recovery->'upload_outcome_review'=e.checkpoint->'upload_outcome_review'
                   AND e.checkpoint->'upload_outcome_review'->>'status'='ACCEPTED'
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'version'='upload-outcome-review-v1'
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'outcome'='EXISTING_ORIGINAL'
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'execution_id'=e.id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'operation_id'=o.id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'asset_id'=segment.asset_id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'segment_index'=segment.segment_index::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'account_id'=r.account_id::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'guest_owner'=r.owner::text
                   AND e.checkpoint->'upload_outcome_review'->'evidence'->>'segment_sha256'=segment.sha256)))
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

