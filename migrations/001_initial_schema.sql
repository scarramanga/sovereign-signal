-- sovereign-signal initial schema
-- All tables use ss_ prefix

-- Content repository
CREATE TABLE IF NOT EXISTS ss_content (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    platform        TEXT NOT NULL CHECK (platform IN ('linkedin', 'substack', 'both')),
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'pipeline', 'scheduled', 'live', 'archived')),
    scheduled_at    TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    linkedin_post_id TEXT,
    themes          TEXT[],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- LinkedIn session cookies (persistent auth)
CREATE TABLE IF NOT EXISTS ss_sessions (
    id              SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'linkedin',
    cookies         TEXT NOT NULL,
    user_agent      TEXT,
    valid           BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Agent job queue
CREATE TABLE IF NOT EXISTS ss_jobs (
    id              SERIAL PRIMARY KEY,
    job_type        TEXT NOT NULL
                    CHECK (job_type IN ('publish', 'listen', 'scout', 'reply')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'awaiting_approval', 'approved', 'rejected', 'complete', 'failed')),
    payload         JSONB NOT NULL DEFAULT '{}',
    result          JSONB,
    error           TEXT,
    content_id      INTEGER REFERENCES ss_content(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Approval email loop
CREATE TABLE IF NOT EXISTS ss_approvals (
    id              SERIAL PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES ss_jobs(id),
    draft_text      TEXT NOT NULL,
    reaction        TEXT,
    reasoning       TEXT,
    resend_email_id TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'edited', 'rejected')),
    approved_text   TEXT,
    responded_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Scout opportunities (posts found for Andy to comment on)
CREATE TABLE IF NOT EXISTS ss_opportunities (
    id              SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'linkedin',
    post_url        TEXT NOT NULL,
    post_author     TEXT,
    post_snippet    TEXT,
    themes          TEXT[],
    draft_comment   TEXT,
    reaction        TEXT,
    reasoning       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sent_for_approval', 'approved', 'posted', 'rejected', 'expired')),
    job_id          INTEGER REFERENCES ss_jobs(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ss_content_status ON ss_content(status);
CREATE INDEX IF NOT EXISTS idx_ss_jobs_status ON ss_jobs(status);
CREATE INDEX IF NOT EXISTS idx_ss_jobs_type ON ss_jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_ss_approvals_status ON ss_approvals(status);
CREATE INDEX IF NOT EXISTS idx_ss_opportunities_status ON ss_opportunities(status);
