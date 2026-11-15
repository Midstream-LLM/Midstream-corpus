CREATE FUNCTION valid_step_flags(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
 SELECT CASE WHEN jsonb_typeof(value) <> 'object' THEN false ELSE NOT EXISTS (
   SELECT 1 FROM jsonb_each(value) p WHERE jsonb_typeof(p.value)<>'boolean'
   OR p.key !~ '^[a-z][a-z0-9_]*$') END
$$;
ALTER TABLE document_version ADD COLUMN processed_steps jsonb NOT NULL DEFAULT '{}'
 CHECK (valid_step_flags(processed_steps));
ALTER TABLE document_version ADD COLUMN contributors jsonb NOT NULL DEFAULT '[]'
 CHECK (jsonb_typeof(contributors)='array');
ALTER TABLE document_version ADD COLUMN tags jsonb NOT NULL DEFAULT '[]'
 CHECK (jsonb_typeof(tags)='array');
ALTER TABLE document_version ADD COLUMN tune_pattern text;
ALTER TABLE document_version ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'
 CHECK(jsonb_typeof(external_ids)='object');
ALTER TABLE document_version ADD COLUMN field_claims jsonb NOT NULL DEFAULT '{}'
 CHECK(jsonb_typeof(field_claims)='object');
CREATE INDEX uncurated_current ON document_version(doc_id)
 WHERE is_current AND NOT coalesce((processed_steps->>'ai_curation')::boolean,false);
CREATE INDEX current_processing_steps ON document_version USING gin(processed_steps) WHERE is_current;
CREATE INDEX current_tags ON document_version USING gin(tags) WHERE is_current;
ALTER TABLE source_batch ADD COLUMN curation_config jsonb;
ALTER TABLE source_record ADD COLUMN curation_base_version_id bigint REFERENCES document_version;
ALTER TABLE management_event DROP CONSTRAINT management_event_action_check;
ALTER TABLE management_event ADD CONSTRAINT management_event_action_check CHECK
 (action IN ('document_review','work_review','batch_resume','batch_abandon','batch_register','dedup_override',
             'curation_apply','curation_rollback'));

CREATE TABLE curation_item (
 item_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id bigint NOT NULL REFERENCES processing_run,
 doc_id bigint REFERENCES document,
 record_id bigint REFERENCES source_record,
 base_version_id bigint REFERENCES document_version,
 result_version_id bigint REFERENCES document_version,
 input_hash text NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','proposed','applied','conflict','failed','rolled_back')),
 completed_steps jsonb NOT NULL DEFAULT '{}' CHECK(valid_step_flags(completed_steps)),
 summary text NOT NULL DEFAULT '',
 error_code text,
 usage jsonb NOT NULL DEFAULT '{}',
 input_snapshot jsonb NOT NULL DEFAULT '{}',
 output_snapshot jsonb NOT NULL DEFAULT '{}',
 output_segments jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK (doc_id IS NOT NULL OR record_id IS NOT NULL)
);
CREATE INDEX curation_item_doc ON curation_item(doc_id,item_id DESC);
CREATE INDEX curation_item_record ON curation_item(record_id,item_id DESC);
CREATE TABLE curation_call (
 item_id bigint NOT NULL REFERENCES curation_item,
 call_key text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('model','retrieval')),
 result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(item_id,call_key)
);
CREATE TABLE curation_evidence (
 evidence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 digest text NOT NULL UNIQUE,
 provider text NOT NULL,
 reference text NOT NULL,
 content jsonb NOT NULL,
 retrieved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE curation_claim (
 claim_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 item_id bigint NOT NULL REFERENCES curation_item,
 kind text NOT NULL CHECK(kind IN ('metadata','text_patch','segment')),
 field text NOT NULL,
 value jsonb NOT NULL,
 source_kind text NOT NULL CHECK(source_kind IN ('rule','authority','llm','human')),
 confidence double precision CHECK(confidence BETWEEN 0 AND 1),
 confidence_type text NOT NULL DEFAULT 'model_self_reported'
 CHECK(confidence_type IN ('model_self_reported','deterministic','calibrated')),
 rationale text NOT NULL,
 application_status text NOT NULL CHECK(application_status IN ('proposed','applied','withheld','conflict','reverted')),
 human_reviewed boolean NOT NULL DEFAULT false,
 review_outcome text CHECK(review_outcome IN ('confirmed','rejected')),
 reviewed_by text,
 reviewed_at timestamptz,
 CHECK (human_reviewed = (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND review_outcome IS NOT NULL))
);
CREATE INDEX curation_claim_item ON curation_claim(item_id,claim_id);
CREATE TABLE curation_claim_evidence (
 claim_id bigint REFERENCES curation_claim,
 evidence_id bigint REFERENCES curation_evidence,
 PRIMARY KEY(claim_id,evidence_id)
);
CREATE TABLE curation_review_event (
 review_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 claim_id bigint NOT NULL REFERENCES curation_claim,
 actor text NOT NULL, reason text NOT NULL,
 outcome text NOT NULL CHECK(outcome IN ('confirmed','rejected')),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE processing_event (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 run_id bigint NOT NULL REFERENCES processing_run,
 version_id bigint REFERENCES document_version,
 record_id bigint REFERENCES source_record,
 step text NOT NULL CHECK(step ~ '^[a-z][a-z0-9_]*$'),
 completed boolean NOT NULL,
 details jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(version_id IS NOT NULL OR record_id IS NOT NULL)
);
CREATE INDEX processing_event_version ON processing_event(version_id,event_id);
CREATE TABLE retrieval_cache (
 cache_key text PRIMARY KEY,
 payload jsonb NOT NULL,
 expires_at timestamptz NOT NULL
);
CREATE INDEX retrieval_cache_expiry ON retrieval_cache(expires_at);
CREATE TABLE document_segment (
 segment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 version_id bigint NOT NULL REFERENCES document_version,
 claim_id bigint NOT NULL REFERENCES curation_claim,
 start_char bigint NOT NULL CHECK(start_char>=0),
 end_char bigint NOT NULL CHECK(end_char>start_char),
 title text,
 unit_type text NOT NULL,
 UNIQUE(version_id,claim_id)
);
CREATE INDEX document_segment_version ON document_segment(version_id,start_char);
