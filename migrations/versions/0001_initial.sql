-- 来源与批次
CREATE TABLE source (
    source_id    int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL UNIQUE,
    description  text,
    license_note text
);

CREATE TABLE source_batch (
    batch_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id      int NOT NULL REFERENCES source,
    received_at    timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz,
    parser_version text NOT NULL,
    status         text NOT NULL CHECK (status IN
        ('received', 'staged', 'validated', 'published', 'failed', 'needs_review'))
);

CREATE TABLE raw_object (
    raw_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id   bigint NOT NULL REFERENCES source_batch,
    path       text NOT NULL,
    sha256     bytea NOT NULL CHECK (octet_length(sha256) = 32),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    UNIQUE (batch_id, path)
);
CREATE INDEX raw_object_sha256 ON raw_object (sha256);

-- 处理运行：解析、清洗、查重、元数据规则共用
CREATE TABLE processing_run (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     bigint REFERENCES source_batch,
    kind         text NOT NULL,
    rule_version text NOT NULL,
    config       jsonb NOT NULL DEFAULT '{}',
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

-- 文档：稳定身份
CREATE TABLE document (
    doc_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id     int NOT NULL REFERENCES source,
    source_key    text NOT NULL,
    parent_doc_id bigint REFERENCES document,
    unit_type     text,
    seq           int,
    UNIQUE (source_id, source_key)
);
CREATE INDEX document_parent ON document (parent_doc_id);

CREATE TABLE source_record (
    record_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id     bigint NOT NULL REFERENCES source_batch,
    source_key   text NOT NULL,
    doc_id       bigint REFERENCES document,
    raw_id       bigint REFERENCES raw_object,
    locator      jsonb NOT NULL,
    raw_fields   jsonb NOT NULL,
    record_hash  bytea NOT NULL CHECK (octet_length(record_hash) = 32),
    parse_status text NOT NULL CHECK (parse_status IN ('ok', 'failed')),
    parse_error  text,
    UNIQUE (batch_id, source_key)
);
CREATE INDEX source_record_doc ON source_record (doc_id);
CREATE INDEX source_record_key ON source_record (source_key);

-- 暂存层：发布前的中间文本，按批次分区
CREATE TABLE staging_text (
    batch_id   bigint NOT NULL REFERENCES source_batch,
    source_key text NOT NULL,
    step       text NOT NULL CHECK (step IN ('decoded', 'cleaned')),
    metadata   jsonb NOT NULL DEFAULT '{}',
    content    text COMPRESSION lz4 NOT NULL,
    PRIMARY KEY (batch_id, source_key, step)
) PARTITION BY LIST (batch_id);

-- 发布层：压缩字典与正文
CREATE TABLE compression_dict (
    dict_id    int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content    bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE text_blob (
    text_hash  bytea PRIMARY KEY CHECK (octet_length(text_hash) = 32),
    codec      text NOT NULL CHECK (codec IN ('zstd')),
    dict_id    int REFERENCES compression_dict,
    char_count bigint NOT NULL CHECK (char_count >= 0),
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    content    bytea NOT NULL
);
ALTER TABLE text_blob ALTER COLUMN content SET STORAGE EXTERNAL;

-- 查重指纹：按正文和规则计算，可重建
CREATE TABLE text_fingerprint (
    text_hash    bytea NOT NULL REFERENCES text_blob,
    run_id       bigint NOT NULL REFERENCES processing_run,
    compare_hash bytea NOT NULL,
    PRIMARY KEY (text_hash, run_id)
);
CREATE INDEX fingerprint_compare ON text_fingerprint (run_id, compare_hash);

-- 版本：正文 + 完整元数据快照，除 is_current 外只增不改
CREATE TABLE document_version (
    version_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id            bigint NOT NULL REFERENCES document,
    version_no        int NOT NULL CHECK (version_no >= 1),
    parent_version_id bigint,
    text_hash         bytea NOT NULL REFERENCES text_blob,
    run_id            bigint NOT NULL REFERENCES processing_run,
    is_current        boolean NOT NULL DEFAULT false,
    quality_status    text NOT NULL CHECK (quality_status IN ('ok', 'flagged', 'quarantined')),
    quality_flags     text[] NOT NULL DEFAULT '{}',
    char_count        bigint NOT NULL CHECK (char_count >= 0),
    license           text,

    title             text,
    authors           text[],
    composed_years    int4range,
    composed_label    text,
    edition_years     int4range,
    genre             text,
    category          text,
    topics            text[],
    legacy_category   text,
    field_basis       jsonb NOT NULL DEFAULT '{}',
    extra             jsonb NOT NULL DEFAULT '{}',

    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_id, version_no),
    UNIQUE (doc_id, version_id),
    FOREIGN KEY (doc_id, parent_version_id) REFERENCES document_version (doc_id, version_id)
);

CREATE UNIQUE INDEX one_current_version ON document_version (doc_id) WHERE is_current;
CREATE INDEX current_title    ON document_version (title)    WHERE is_current;
CREATE INDEX current_genre    ON document_version (genre)    WHERE is_current;
CREATE INDEX current_category ON document_version (category) WHERE is_current;
CREATE INDEX current_chars    ON document_version (char_count) WHERE is_current;
CREATE INDEX current_years    ON document_version USING gist (composed_years) WHERE is_current;
CREATE INDEX current_authors  ON document_version USING gin (authors) WHERE is_current;
CREATE INDEX current_topics   ON document_version USING gin (topics) WHERE is_current;
CREATE INDEX version_text     ON document_version (text_hash);

-- 作品：文献身份，可以逐步补全
CREATE TABLE work (
    work_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_title text NOT NULL,
    aliases         text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX work_title ON work (canonical_title);

CREATE TABLE document_work (
    doc_id   bigint NOT NULL REFERENCES document,
    work_id  bigint NOT NULL REFERENCES work,
    relation text NOT NULL CHECK (relation IN ('whole', 'part', 'excerpt', 'contains')),
    basis    text NOT NULL CHECK (basis IN ('source', 'rule', 'human')),
    status   text NOT NULL CHECK (status IN ('candidate', 'confirmed', 'rejected')),
    PRIMARY KEY (doc_id, work_id, relation)
);
CREATE INDEX document_work_work ON document_work (work_id);

-- 重复簇：绑定具体版本和查重运行
CREATE TABLE dup_cluster (
    cluster_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     bigint NOT NULL REFERENCES processing_run,
    level      text NOT NULL CHECK (level IN ('document', 'paragraph'))
);

CREATE TABLE dup_cluster_member (
    cluster_id bigint NOT NULL REFERENCES dup_cluster,
    version_id bigint NOT NULL REFERENCES document_version,
    kind       text NOT NULL CHECK (kind IN ('exact', 'near_candidate')),
    locator    jsonb,
    PRIMARY KEY (cluster_id, version_id)
);
CREATE INDEX dup_member_version ON dup_cluster_member (version_id);

-- 数据集：固定到版本
CREATE TABLE dataset_release (
    release_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           text NOT NULL UNIQUE,
    recipe         jsonb NOT NULL,
    content_digest bytea NOT NULL CHECK (octet_length(content_digest) = 32),
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dataset_member (
    release_id bigint NOT NULL REFERENCES dataset_release,
    version_id bigint NOT NULL REFERENCES document_version,
    ordinal    bigint NOT NULL,
    split      text NOT NULL CHECK (split IN ('train', 'val', 'test')),
    PRIMARY KEY (release_id, version_id),
    UNIQUE (release_id, ordinal)
);
CREATE INDEX dataset_member_version ON dataset_member (version_id);

-- 训练使用历史：记录哪个数据集进过哪次训练
CREATE TABLE training_use (
    use_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    release_id bigint NOT NULL REFERENCES dataset_release,
    run_name   text NOT NULL,
    used_at    timestamptz,
    note       text,
    UNIQUE (release_id, run_name)
);

CREATE TABLE export_artifact (
    artifact_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    release_id  bigint NOT NULL REFERENCES dataset_release,
    path        text NOT NULL,
    format      text NOT NULL,
    sha256      bytea NOT NULL CHECK (octet_length(sha256) = 32),
    tool_info   jsonb NOT NULL DEFAULT '{}'
);
