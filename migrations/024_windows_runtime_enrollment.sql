-- A software/environment candidate is not a verified runtime registration.
-- Keep every old identity and UI fence; never upgrade it by migration.
CREATE TABLE windows_runtime_enrollments (
    runtime_id uuid NOT NULL,
    generation bigint NOT NULL,
    identity_sha256 text NOT NULL CHECK (identity_sha256 ~ '^[a-f0-9]{64}$'),
    docker_daemon_id text NOT NULL CHECK (length(btrim(docker_daemon_id)) > 0),
    staging_device bigint NOT NULL CHECK (staging_device >= 0),
    staging_inode bigint NOT NULL CHECK (staging_inode > 0),
    native_receipt jsonb NOT NULL CHECK (jsonb_typeof(native_receipt) = 'object'),
    operator text NOT NULL CHECK (length(btrim(operator)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(runtime_id,generation),
    FOREIGN KEY(runtime_id,generation) REFERENCES upload_runtimes(runtime_id,generation),
    CHECK (native_receipt ?& ARRAY['runtime_id','runtime_generation','identity_sha256','nonce',
                                 'device_key','tools_id','script_sha256','observation_sha256','observed_at','quiescent']),
    CHECK ((native_receipt->>'runtime_id')::uuid = runtime_id),
    CHECK ((native_receipt->>'runtime_generation')::bigint = generation),
    CHECK (native_receipt->>'identity_sha256' = identity_sha256),
    CHECK (native_receipt->'quiescent' = 'true'::jsonb)
);

UPDATE upload_runtimes SET state='REVIEW_REQUIRED',
    review_reason=COALESCE(review_reason,'MIGRATION_REQUIRES_VERIFIED_ENROLLMENT')
  WHERE kind='windows_android' AND state='READY';

CREATE FUNCTION require_windows_runtime_enrollment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind='windows_android' AND NEW.state='READY' AND NOT EXISTS (
        SELECT 1 FROM windows_runtime_enrollments e
         WHERE e.runtime_id=NEW.runtime_id AND e.generation=NEW.generation
           AND e.identity_sha256=NEW.identity_sha256
           AND e.native_receipt->>'tools_id'=NEW.tools_id
           AND e.native_receipt->>'device_key'=NEW.device_key
    ) THEN
        RAISE EXCEPTION 'Windows runtime requires verified enrollment' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER windows_runtime_ready_requires_enrollment BEFORE INSERT OR UPDATE ON upload_runtimes
    FOR EACH ROW EXECUTE FUNCTION require_windows_runtime_enrollment();

CREATE FUNCTION immutable_windows_runtime_enrollment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Windows runtime enrollment is immutable' USING ERRCODE='23514';
END $$;
CREATE TRIGGER windows_runtime_enrollment_immutable BEFORE UPDATE OR DELETE ON windows_runtime_enrollments
    FOR EACH ROW EXECUTE FUNCTION immutable_windows_runtime_enrollment();

REVOKE ALL ON windows_runtime_enrollments FROM PUBLIC;
GRANT SELECT,INSERT ON windows_runtime_enrollments TO pixav_execution_authority;
GRANT SELECT ON windows_runtime_enrollments TO pixav_activity_worker;
