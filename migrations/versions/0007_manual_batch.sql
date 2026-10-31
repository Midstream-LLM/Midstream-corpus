ALTER TABLE source_batch DROP CONSTRAINT source_batch_status_check;
ALTER TABLE source_batch ADD CONSTRAINT source_batch_status_check CHECK (status IN
    ('received', 'staged', 'validated', 'published', 'failed', 'needs_review', 'abandoned'));

CREATE TABLE management_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action text NOT NULL CHECK (action IN
        ('document_review', 'work_review', 'batch_resume', 'batch_abandon')),
    actor text NOT NULL CHECK (length(btrim(actor)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    doc_id bigint REFERENCES document,
    work_id bigint REFERENCES work,
    batch_id bigint REFERENCES source_batch,
    from_version_id bigint REFERENCES document_version,
    to_version_id bigint REFERENCES document_version,
    before_state jsonb NOT NULL DEFAULT '{}',
    after_state jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX management_event_doc ON management_event (doc_id, event_id);
CREATE INDEX management_event_batch ON management_event (batch_id, event_id);
