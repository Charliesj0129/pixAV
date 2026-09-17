-- 008_source_candidates.sql
-- One media item, many sources. Purely additive: safe to apply while the
-- currently deployed code is still running.
--
-- Rationale (RFC v3 §0.B.6):
--   A dead swarm invalidates one *source*, not the media item. Modelling the
--   magnet as a column on `videos` made "this torrent has no seeds" and "this
--   film is unobtainable" the same fact, so a single dead source failed the
--   film permanently.
--
-- Expand/contract: this file only creates and backfills. Dropping the old
-- `videos.cdn_url` column is split into 009 because it is the half that breaks
-- any still-running process compiled against the old schema — see that file.

CREATE TABLE IF NOT EXISTS source_candidates (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    video_id          UUID NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    magnet_uri        TEXT NOT NULL,
    info_hash         VARCHAR(40),
    origin            TEXT NOT NULL DEFAULT 'sehuatang',
    quality_score     INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL DEFAULT 'pending'
                          CHECK (state IN ('pending', 'unavailable', 'succeeded')),
    -- Set when a candidate is cooled down. NULL means "eligible now".
    unavailable_until TIMESTAMPTZ,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ
);

-- Two rows may share a magnet across different videos, but never within one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_candidates_video_magnet
    ON source_candidates (video_id, magnet_uri);

-- The selection query: best eligible candidate for one video.
CREATE INDEX IF NOT EXISTS idx_source_candidates_eligible
    ON source_candidates (video_id, quality_score DESC, created_at ASC)
 WHERE state = 'pending';

-- Backfill: every existing magnet becomes that video's first candidate. Runs
-- re-entrantly, and never resurrects a candidate an operator already cooled down.
INSERT INTO source_candidates (video_id, magnet_uri, info_hash, quality_score)
SELECT v.id, v.magnet_uri, v.info_hash, COALESCE(v.quality_score, 0)
  FROM videos AS v
 WHERE v.magnet_uri IS NOT NULL
   AND btrim(v.magnet_uri) <> ''
ON CONFLICT (video_id, magnet_uri) DO NOTHING;
