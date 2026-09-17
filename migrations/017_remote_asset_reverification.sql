-- Expand only. A durable remote copy that nobody ever reads again is an
-- assumption, not a fact, so the authority records when each one was last
-- independently re-read and can open a verify-only execution for the overdue.
-- Apply using an administrative connection after identity and backup verification.

ALTER TABLE remote_assets ADD COLUMN reverified_at timestamptz;

-- The overdue set is "durable, and last looked at longer ago than the interval".
-- durable_at stands in until the first re-read, so an asset stored today is not
-- immediately overdue.
CREATE INDEX remote_assets_reverification_due ON remote_assets(COALESCE(reverified_at, durable_at))
    WHERE state = 'DURABLE';

-- 013 declared REQUESTED -> CREATED -> VERIFIED -> DURABLE but nothing ever
-- wrote VERIFIED, so assets whose every segment already came back cold and
-- whole are sitting in CREATED. Promotion now requires the transition to have
-- been committed, and these rows already satisfy what it means: this advances
-- them from facts the database already holds, and invents nothing.
UPDATE remote_assets a SET state = 'VERIFIED', updated_at = now()
 WHERE a.state = 'CREATED'
   AND a.segment_count = (SELECT count(*) FROM remote_asset_segments s
                           WHERE s.asset_id = a.id AND s.state = 'verified');
