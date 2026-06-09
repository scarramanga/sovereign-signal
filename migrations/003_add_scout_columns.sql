-- sovereign-signal migration 003
-- Add columns + dedup job type required by the Scout agent

-- source: which agent created the approval. Existing rows backfill to 'listener'
-- via the default; Scout inserts 'scout'.
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS source VARCHAR NOT NULL DEFAULT 'listener';
ALTER TABLE ss_approvals DROP CONSTRAINT IF EXISTS ss_approvals_source_check;
ALTER TABLE ss_approvals ADD CONSTRAINT ss_approvals_source_check
    CHECK (source IN ('listener', 'scout'));

-- post_url / post_text: the LinkedIn post Scout is commenting on
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS post_url VARCHAR;
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS post_text TEXT;

-- Extend ss_jobs.job_type CHECK to include 'scout_seen' (dedup marker, mirrors
-- the listener's 'listener_seen').
ALTER TABLE ss_jobs DROP CONSTRAINT IF EXISTS ss_jobs_job_type_check;
ALTER TABLE ss_jobs ADD CONSTRAINT ss_jobs_job_type_check
    CHECK (job_type IN ('publish', 'listen', 'scout', 'reply', 'listener_seen', 'scout_seen'));

-- Index for source-filtered pending-posts lookups
CREATE INDEX IF NOT EXISTS idx_ss_approvals_source ON ss_approvals(source);
