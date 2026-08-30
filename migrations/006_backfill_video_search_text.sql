-- 006_backfill_video_search_text.sql
-- `videos.search_text` is maintained by the `tsvectorupdate` trigger, which
-- only fires on INSERT/UPDATE.  Rows that predate the column (or that were
-- inserted before the trigger existed) keep a NULL vector and are therefore
-- invisible to `VideoRepository.search()`.
--
-- Recompute the vector for those rows using the same expression as the trigger.

UPDATE videos
   SET search_text =
           setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
           setweight(to_tsvector('simple', array_to_string(coalesce(tags, '{}'), ' ')), 'B')
 WHERE search_text IS NULL;
