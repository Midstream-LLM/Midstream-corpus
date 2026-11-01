CREATE TABLE job (
    job_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    params jsonb NOT NULL,
    progress jsonb NOT NULL DEFAULT '{"current":0,"total":0,"message":"等待 worker"}',
    result jsonb,
    error text,
    actor text NOT NULL,
    reason text NOT NULL,
    cancel_requested boolean NOT NULL DEFAULT false,
    heartbeat_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE INDEX job_queued ON job (job_id) WHERE status = 'queued';
CREATE TABLE job_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES job,
    created_at timestamptz NOT NULL DEFAULT now(),
    level text NOT NULL,
    message text NOT NULL
);
CREATE INDEX job_event_recent ON job_event (job_id, event_id DESC);
