-- 段落级重复：原文字符偏移，左闭右开；每次查重替换快照。
CREATE TABLE dup_span (
    run_id bigint NOT NULL REFERENCES processing_run,
    version_id bigint NOT NULL REFERENCES document_version,
    start_char int NOT NULL,
    end_char int NOT NULL,
    source_version_id bigint NOT NULL REFERENCES document_version,
    source_start_char int NOT NULL,
    PRIMARY KEY (version_id, start_char)
);
