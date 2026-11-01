CREATE TABLE dedup_override (
    override_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    left_doc_id bigint NOT NULL REFERENCES document,
    right_doc_id bigint NOT NULL REFERENCES document,
    actor text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (left_doc_id < right_doc_id),
    UNIQUE(left_doc_id,right_doc_id)
);
ALTER TABLE management_event DROP CONSTRAINT management_event_action_check;
ALTER TABLE management_event ADD CONSTRAINT management_event_action_check CHECK
 (action IN ('document_review','work_review','batch_resume','batch_abandon','batch_register','dedup_override'));
