-- Expand only. A private receipt is validated by the operator entry point;
-- the authority consumes its immutable, exact-target registration, never a
-- free-text backup reference. Legacy approvals remain ineligible.
ALTER TABLE cleanup_authorizations ADD COLUMN recovery_authorization jsonb;
ALTER TABLE cleanup_authorizations ADD COLUMN approval_sha256 text;
ALTER TABLE cleanup_authorizations ADD CONSTRAINT cleanup_recovery_authorization_object
    CHECK (recovery_authorization IS NULL OR COALESCE(
      jsonb_typeof(recovery_authorization)='object' AND
      recovery_authorization->>'version'='cleanup-recovery-authorization-v1', FALSE));
ALTER TABLE cleanup_authorizations ADD CONSTRAINT cleanup_approval_digest_format
    CHECK (approval_sha256 IS NULL OR approval_sha256 ~ '^[a-f0-9]{64}$');

-- Do not backfill or grant INSERT/UPDATE to the execution authority or workers.
