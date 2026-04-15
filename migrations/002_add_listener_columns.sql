-- sovereign-signal migration 002
-- Add columns required by the Listener agent

-- approval_token: unique token used in approval email links
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS approval_token TEXT;

-- context_json: stores post_url, commenter_name, comment_text, comment_id
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS context_json JSONB;

-- approved_at / posted_at timestamps for tracking approval flow
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE ss_approvals ADD COLUMN IF NOT EXISTS posted_at TIMESTAMPTZ;

-- Make job_id nullable — listener creates approvals before a job exists
ALTER TABLE ss_approvals ALTER COLUMN job_id DROP NOT NULL;

-- Extend ss_jobs.job_type CHECK to include 'listener_seen'
ALTER TABLE ss_jobs DROP CONSTRAINT IF EXISTS ss_jobs_job_type_check;
ALTER TABLE ss_jobs ADD CONSTRAINT ss_jobs_job_type_check
    CHECK (job_type IN ('publish', 'listen', 'scout', 'reply', 'listener_seen'));

-- Extend ss_jobs.status CHECK to include 'done'
ALTER TABLE ss_jobs DROP CONSTRAINT IF EXISTS ss_jobs_status_check;
ALTER TABLE ss_jobs ADD CONSTRAINT ss_jobs_status_check
    CHECK (status IN ('pending', 'running', 'awaiting_approval', 'approved', 'rejected', 'complete', 'failed', 'done'));

-- Index on approval_token for fast lookups
CREATE INDEX IF NOT EXISTS idx_ss_approvals_token ON ss_approvals(approval_token);
