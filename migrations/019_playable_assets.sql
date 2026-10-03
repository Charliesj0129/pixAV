-- Expand only. A durable remote copy is not yet something a player can read:
-- PlayableAsset is the committed fact that the remote bytes were brought back,
-- checked against what they stand for, and can be served. The playback cache it
-- is served from is disposable and never recorded here as truth.
-- Apply using an administrative connection after identity and backup verification.

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pixav_playback_reader') THEN
        CREATE ROLE pixav_playback_reader NOLOGIN;
    END IF;
END $$;

-- Preparing a playable object is work the authority schedules, retries and
-- fences like any other activity, so its stage joins the same vocabulary.
ALTER TABLE executions DROP CONSTRAINT executions_stage_check;
ALTER TABLE executions ADD CONSTRAINT executions_stage_check
    CHECK (stage IN ('download','prepare','handoff','upload','verify','playback'));

CREATE TABLE playable_assets (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos(id),
    remote_asset_id uuid NOT NULL REFERENCES remote_assets(id),
    -- How the playable object is assembled from the remote segments. A change
    -- of recipe is a new version, never a reinterpretation of an old one.
    preparation_version text NOT NULL CHECK (length(btrim(preparation_version)) > 0),
    state text NOT NULL DEFAULT 'PREPARING' CHECK (state IN ('PREPARING','READY','STALE','INVALID')),
    -- The exact bytes every stream response must come from. Known only once a
    -- materialization has been verified, and never changed afterwards: a
    -- rebuild that produces other bytes makes this row STALE instead.
    size_bytes bigint CHECK (size_bytes > 0),
    sha256 text CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    evidence jsonb NOT NULL DEFAULT '{}',
    ready_at timestamptz,
    -- The last time a verified object was published into the playback cache.
    -- Demand older than this has already been answered.
    materialized_at timestamptz,
    -- An end-to-end stream check through the stable URL. Cleanup requires it.
    playback_verified_at timestamptz,
    playback_evidence jsonb NOT NULL DEFAULT '{}',
    invalidated_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (state <> 'READY' OR (ready_at IS NOT NULL AND size_bytes IS NOT NULL AND sha256 IS NOT NULL)),
    CHECK (playback_verified_at IS NULL OR ready_at IS NOT NULL),
    CHECK (state <> 'INVALID' OR COALESCE(length(btrim(invalidated_reason)), 0) > 0)
);
-- One stable URL answers with one object: at most one READY per video.
CREATE UNIQUE INDEX playable_assets_one_ready ON playable_assets(video_id) WHERE state = 'READY';
-- At most one live preparation of a remote asset under one recipe.
CREATE UNIQUE INDEX playable_assets_one_current ON playable_assets(remote_asset_id, preparation_version)
    WHERE state IN ('PREPARING','READY');
CREATE INDEX playable_assets_remote ON playable_assets(remote_asset_id, state);

-- A derived signal, not a fact: a reader found no cached object and asked for
-- one. The authority compares it with materialized_at; nothing else reads it.
CREATE TABLE playback_cache_requests (
    playable_asset_id uuid PRIMARY KEY REFERENCES playable_assets(id),
    requested_at timestamptz NOT NULL DEFAULT now(),
    request_count bigint NOT NULL DEFAULT 1 CHECK (request_count > 0)
);

REVOKE ALL ON playable_assets, playback_cache_requests FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON playable_assets TO pixav_execution_authority;
GRANT SELECT ON playback_cache_requests TO pixav_execution_authority;
-- The playback worker is told which asset to materialize and reads its facts;
-- only the authority decides that a materialization made anything READY.
GRANT SELECT ON playable_assets TO pixav_activity_worker;
-- The stream endpoint reads eligibility and records demand. It cannot make an
-- asset READY, change a remote asset, or touch execution state.
GRANT SELECT ON videos, remote_assets, playable_assets TO pixav_playback_reader;
GRANT SELECT, INSERT, UPDATE ON playback_cache_requests TO pixav_playback_reader;
-- The legacy share-URL path still marks a video available when it resolves.
GRANT UPDATE (status, updated_at) ON videos TO pixav_playback_reader;
