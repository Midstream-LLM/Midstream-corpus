ALTER TABLE source_batch ADD COLUMN import_plan jsonb;
ALTER TABLE management_event DROP CONSTRAINT management_event_action_check;
ALTER TABLE management_event ADD CONSTRAINT management_event_action_check
CHECK (action IN ('document_review', 'work_review', 'batch_resume', 'batch_abandon', 'batch_register'));
