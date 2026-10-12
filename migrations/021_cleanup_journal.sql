-- Expand only. The intent commits before unlink, independently of the domain
-- transaction. Snapshot identifiers deliberately have no FK to the locked video:
-- history must survive withdrawal, and intent insertion must not wait for that lock.
CREATE TABLE cleanup_journal (
    id uuid PRIMARY KEY,
    video_id uuid NOT NULL,
    path text NOT NULL,
    fingerprint jsonb NOT NULL CHECK (jsonb_typeof(fingerprint) = 'object'),
    gates jsonb NOT NULL CHECK (jsonb_typeof(gates) = 'object'),
    state text NOT NULL DEFAULT 'INTENT' CHECK (state IN ('INTENT','COMPLETED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK ((state = 'COMPLETED') = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX cleanup_journal_pending_path ON cleanup_journal(path) WHERE state='INTENT';
REVOKE ALL ON cleanup_journal FROM PUBLIC;
GRANT SELECT, INSERT ON cleanup_journal TO pixav_execution_authority;
GRANT UPDATE (state, completed_at) ON cleanup_journal TO pixav_execution_authority;
