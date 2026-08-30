-- 005_unique_video_magnet_uri.sql
-- Enforce magnet de-duplication in the database instead of relying solely on
-- `VideoRepository.find_by_magnet()` doing a read-before-write.
--
-- `idx_videos_info_hash` only covers rows whose info_hash could be parsed, so
-- a malformed or placeholder magnet can currently be inserted repeatedly.
--
-- The index is partial (magnet_uri IS NOT NULL) so videos discovered without a
-- magnet are unaffected.  Duplicate magnets must be resolved before this
-- migration can be applied; failing loudly is deliberate — silently dropping
-- rows here would destroy pipeline state.

DO $$
DECLARE
    duplicate_count INTEGER;
BEGIN
    SELECT count(*) INTO duplicate_count
      FROM (
            SELECT magnet_uri
              FROM videos
             WHERE magnet_uri IS NOT NULL
             GROUP BY magnet_uri
            HAVING count(*) > 1
           ) AS duplicates;

    IF duplicate_count > 0 THEN
        RAISE EXCEPTION
            'cannot add unique index: % magnet_uri value(s) are duplicated in videos; '
            'resolve them before re-running migrations', duplicate_count;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_magnet_uri
    ON videos (magnet_uri)
 WHERE magnet_uri IS NOT NULL;
