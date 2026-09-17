-- Expand only. The retained upload guest becomes a journalled fact so that a
-- crash between "decide to create a container" and "a container exists" is
-- recoverable, exactly like an upload. Apply using an administrative connection
-- after identity and backup verification.

-- One row per upload worker. The guest holds a signed-in Google account and
-- possibly an upload in flight, so it is retained across executions and is
-- never recreated on a hunch: an unresolved intent is an operator decision.
CREATE TABLE guest_runtimes (
    owner uuid PRIMARY KEY,
    state text NOT NULL CHECK (state IN
        ('INTENT','GUEST_CREATED','READY','RETIRING','REVIEW_REQUIRED','RETIRED')),
    account_id uuid REFERENCES accounts(id),
    profile text NOT NULL CHECK (length(btrim(profile)) > 0),
    guest_image text NOT NULL CHECK (length(btrim(guest_image)) > 0),
    tools_image text NOT NULL CHECK (length(btrim(tools_image)) > 0),
    guest_id text,
    tools_id text,
    staging_root text NOT NULL CHECK (length(btrim(staging_root)) > 0),
    last_execution_id uuid REFERENCES executions(id),
    review_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- A runtime that claims to be usable must name both of its containers.
    CHECK (state <> 'READY' OR (guest_id IS NOT NULL AND tools_id IS NOT NULL)),
    CHECK (state <> 'GUEST_CREATED' OR guest_id IS NOT NULL),
    CHECK (state <> 'REVIEW_REQUIRED' OR length(btrim(review_reason)) > 0)
);

REVOKE ALL ON guest_runtimes FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON guest_runtimes TO pixav_execution_authority;
GRANT SELECT ON guest_runtimes TO pixav_activity_worker;

-- The worker records what it is about to do to its own runtime and nothing
-- else. It cannot adopt another worker's guest, and it cannot move a runtime
-- that an operator has stopped for review back into service.
CREATE FUNCTION journal_runtime(report jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    IF report->>'state' NOT IN ('INTENT','GUEST_CREATED','READY','RETIRING','REVIEW_REQUIRED','RETIRED') THEN
        RETURN false;
    END IF;
    INSERT INTO guest_runtimes(owner, state, account_id, profile, guest_image, tools_image,
                               guest_id, tools_id, staging_root, last_execution_id, review_reason)
    VALUES ((report->>'owner')::uuid,
            report->>'state',
            (report->>'account_id')::uuid,
            report->>'profile',
            report->>'guest_image',
            report->>'tools_image',
            report->>'guest_id',
            report->>'tools_id',
            report->>'staging_root',
            (report->>'execution_id')::uuid,
            report->>'review_reason')
    ON CONFLICT (owner) DO UPDATE
       SET state = EXCLUDED.state,
           account_id = COALESCE(EXCLUDED.account_id, guest_runtimes.account_id),
           guest_id = COALESCE(EXCLUDED.guest_id, guest_runtimes.guest_id),
           tools_id = COALESCE(EXCLUDED.tools_id, guest_runtimes.tools_id),
           last_execution_id = COALESCE(EXCLUDED.last_execution_id, guest_runtimes.last_execution_id),
           review_reason = EXCLUDED.review_reason,
           updated_at = now()
     WHERE guest_runtimes.state <> 'REVIEW_REQUIRED' OR EXCLUDED.state = 'REVIEW_REQUIRED';
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;
REVOKE ALL ON FUNCTION journal_runtime(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION journal_runtime(jsonb) TO pixav_activity_worker, pixav_execution_authority;

-- Retiring a runtime forgets the containers but keeps the row, so the next
-- provisioning still sees that this owner has a history worth reconciling.
CREATE FUNCTION forget_runtime_containers(subject uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE changed integer;
BEGIN
    UPDATE guest_runtimes SET state='RETIRED', guest_id=NULL, tools_id=NULL,
           account_id=NULL, review_reason=NULL, updated_at=now()
     WHERE owner = subject AND state = 'RETIRING';
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;
REVOKE ALL ON FUNCTION forget_runtime_containers(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION forget_runtime_containers(uuid)
    TO pixav_activity_worker, pixav_execution_authority;
