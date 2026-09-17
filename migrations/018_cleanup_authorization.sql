-- Expand only. Staging deletion is the one irreversible step in the pipeline,
-- so the authorization for it is a row naming one exact file, not a policy that
-- covers a directory. A row alone still does not authorize anything: the
-- janitor recomputes the artifact's facts from disk and refuses unless they
-- match what was approved.
-- Apply using an administrative connection after identity and backup verification.

CREATE TABLE cleanup_authorizations (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos(id),
    -- One exact target. No pattern, no prefix, no directory.
    path text NOT NULL,
    -- The bytes that were approved. If the file changed after approval, the
    -- approval is about a file that no longer exists.
    artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    -- The remote copy the approval relies on, so an authorization cannot
    -- outlive the asset it was granted against.
    remote_asset_id uuid NOT NULL REFERENCES remote_assets(id),
    -- When playback was actually verified from that remote copy. Component D
    -- produces this; until it does, no honest row can be written here.
    playback_verified_at timestamptz NOT NULL,
    -- Where the off-host backup of these bytes can be found. Free text on
    -- purpose: it is an operator's reference, never a path this code follows.
    backup_reference text NOT NULL CHECK (length(btrim(backup_reference)) > 0),
    approved_by text NOT NULL CHECK (length(btrim(approved_by)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Approval is a window, not a standing permission.
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE INDEX cleanup_authorizations_target ON cleanup_authorizations(video_id, path, expires_at DESC);

REVOKE ALL ON cleanup_authorizations FROM PUBLIC;
-- The janitor reads these to decide; it must never be able to write one.
GRANT SELECT ON cleanup_authorizations TO pixav_execution_authority;
