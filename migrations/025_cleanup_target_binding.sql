-- Expand only. Old free-text authorizations stay readable but cannot authorize
-- deletion under the new janitor. They must be reviewed against current inode,
-- database, playable object and publication identities; never backfill consent.
ALTER TABLE cleanup_authorizations ADD COLUMN target_binding jsonb;
ALTER TABLE cleanup_authorizations ADD CONSTRAINT cleanup_target_binding_object
    CHECK (target_binding IS NULL OR
      COALESCE(jsonb_typeof(target_binding) = 'object' AND
        target_binding->>'version' = 'cleanup-target-v1', FALSE));

-- Existing authority SELECT permission includes the new column. The authority
-- still cannot insert, change or renew an operator's authorization.
