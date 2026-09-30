-- Expand-only, opt-in playback/projection facts. No legacy row is promoted.
CREATE TABLE playable_assets (
    video_id uuid PRIMARY KEY REFERENCES videos(id),
    remote_asset_id uuid NOT NULL REFERENCES remote_assets(id),
    state text NOT NULL CHECK (state IN ('PREPARING','READY','STALE','INVALID')),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
    cache_path text,
    size_bytes bigint CHECK (size_bytes > 0),
    sha256 text CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    evidence jsonb NOT NULL DEFAULT '{}',
    playback_verified_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (state <> 'READY' OR (cache_path IS NOT NULL AND size_bytes IS NOT NULL AND sha256 IS NOT NULL))
);
CREATE TABLE library_publications (
    video_id uuid PRIMARY KEY REFERENCES videos(id),
    state text NOT NULL CHECK (state IN ('PENDING','PUBLISHED','STALE','FAILED')),
    manifest_sha256 text,
    revision text,
    metadata jsonb NOT NULL DEFAULT '{}',
    poster_path text,
    poster_sha256 text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (state <> 'PUBLISHED' OR (manifest_sha256 IS NOT NULL AND revision IS NOT NULL
        AND poster_path IS NOT NULL AND poster_sha256 IS NOT NULL))
);
REVOKE ALL ON playable_assets, library_publications FROM PUBLIC;
GRANT SELECT ON playable_assets, library_publications TO pixav_execution_authority;
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='pixav_playback') THEN
        CREATE ROLE pixav_playback NOLOGIN;
    END IF;
END $$;
GRANT SELECT ON videos, remote_assets, remote_asset_segments TO pixav_playback;
-- Row locks serialize preparation/readers with janitor's videos FOR UPDATE.
GRANT UPDATE (id) ON videos, remote_assets TO pixav_playback;
GRANT SELECT, INSERT, UPDATE ON playable_assets, library_publications TO pixav_playback;

CREATE FUNCTION invalidate_playback_projection() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
    IF NEW.state='INVALID' THEN
        UPDATE playable_assets SET state='INVALID',playback_verified_at=NULL,updated_at=now()
        WHERE remote_asset_id=NEW.id;
        UPDATE library_publications SET state='STALE',updated_at=now()
        WHERE video_id IN (SELECT video_id FROM playable_assets WHERE remote_asset_id=NEW.id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER invalidate_playback_projection AFTER UPDATE OF state ON remote_assets
FOR EACH ROW EXECUTE FUNCTION invalidate_playback_projection();
