-- Expand only. LibraryProjection: what pixAV has published to the media
-- library, the metadata a person set by hand, and the artwork originals a
-- projection may use. The files under the library root are derived: every one
-- of them can be rebuilt from these rows and the retained artwork.
-- Apply using an administrative connection after identity and backup verification.

-- One row per video that has ever been considered for publication.
--   PENDING   no active projection (never published, or withdrawn)
--   PUBLISHED the active projection is exactly the desired one
--   STALE     an active projection exists but no longer matches what is desired
--   FAILED    the last attempt failed; any previously active projection remains
CREATE TABLE library_publications (
    video_id uuid PRIMARY KEY REFERENCES videos(id),
    -- The READY object the active projection streams. Cleanup relies on it
    -- naming the same object whose playback was verified.
    playable_asset_id uuid REFERENCES playable_assets(id),
    state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','PUBLISHED','STALE','FAILED')),
    layout_version text NOT NULL,
    projection_version integer NOT NULL DEFAULT 0 CHECK (projection_version >= 0),
    desired_digest text CHECK (desired_digest ~ '^[a-f0-9]{64}$'),
    active_digest text CHECK (active_digest ~ '^[a-f0-9]{64}$'),
    -- The version directory the library link points at; never a path.
    active_version text CHECK (active_version ~ '^[0-9]{1,9}-[a-f0-9]{12}$'),
    effective_metadata jsonb NOT NULL DEFAULT '{}',
    published_at timestamptz,
    withdrawn_at timestamptz,
    withdrawn_reason text CHECK (withdrawn_reason ~ '^[A-Z_]{1,80}$'),
    failure_code text CHECK (failure_code ~ '^[A-Z_]{1,80}$'),
    attempted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- An active projection is one specific set of bytes, and both halves of
    -- that claim are recorded together or not at all.
    CHECK ((active_digest IS NULL) = (active_version IS NULL)),
    CHECK (state <> 'PUBLISHED' OR (active_digest IS NOT NULL AND active_digest = desired_digest
        AND playable_asset_id IS NOT NULL AND published_at IS NOT NULL)),
    CHECK (state <> 'STALE' OR active_digest IS NOT NULL),
    CHECK (state <> 'PENDING' OR active_digest IS NULL),
    CHECK (state <> 'FAILED' OR failure_code IS NOT NULL)
);
CREATE INDEX library_publications_state ON library_publications(state);

-- A person's value for one field. Scrapers write elsewhere, so no refresh can
-- reach these rows; clearing an override is the only way to lose one.
CREATE TABLE video_metadata_overrides (
    video_id uuid NOT NULL REFERENCES videos(id),
    field text NOT NULL CHECK (field IN ('title','description','release_date','studio','performers','tags')),
    value jsonb NOT NULL,
    operator text NOT NULL CHECK (length(btrim(operator)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (video_id, field),
    CHECK (jsonb_typeof(value) = CASE WHEN field IN ('performers','tags') THEN 'array' ELSE 'string' END)
);

-- Artwork originals are content-addressed files under the artwork root; this
-- row is their provenance and the digest a projection checks before use. A
-- provider outage cannot remove a retained original.
CREATE TABLE video_artwork (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos(id),
    kind text NOT NULL CHECK (kind IN ('poster','backdrop')),
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    media_type text NOT NULL CHECK (media_type IN ('image/jpeg','image/png','image/webp')),
    provenance jsonb NOT NULL DEFAULT '{}',
    state text NOT NULL DEFAULT 'retained' CHECK (state IN ('retained','withdrawn')),
    created_at timestamptz NOT NULL DEFAULT now(),
    withdrawn_at timestamptz,
    CHECK ((state = 'withdrawn') = (withdrawn_at IS NOT NULL))
);
CREATE UNIQUE INDEX video_artwork_one_retained ON video_artwork(video_id, kind) WHERE state = 'retained';

REVOKE ALL ON library_publications, video_metadata_overrides, video_artwork FROM PUBLIC;
-- The reconciler runs inside the authority's tick and records what it
-- published; it only reads what people decided about metadata and artwork.
GRANT SELECT, INSERT, UPDATE ON library_publications TO pixav_execution_authority;
GRANT SELECT ON video_metadata_overrides, video_artwork TO pixav_execution_authority;
