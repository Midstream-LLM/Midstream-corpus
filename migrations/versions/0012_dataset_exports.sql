CREATE TABLE dataset_export (
    export_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    release_id bigint NOT NULL REFERENCES dataset_release,
    format text NOT NULL,
    options jsonb NOT NULL,
    manifest_path text,
    status text NOT NULL DEFAULT 'writing' CHECK (status IN ('writing', 'completed', 'failed')),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
ALTER TABLE export_artifact
    ADD COLUMN export_id bigint REFERENCES dataset_export,
    ADD COLUMN split text,
    ADD COLUMN row_count bigint,
    ADD COLUMN size_bytes bigint;
CREATE INDEX export_artifact_export ON export_artifact (export_id);
CREATE INDEX dataset_export_release ON dataset_export (release_id, export_id DESC);
