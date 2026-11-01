CREATE TABLE text_token_count (
    text_hash bytea NOT NULL REFERENCES text_blob,
    tokenizer_fingerprint bytea NOT NULL CHECK (octet_length(tokenizer_fingerprint) = 32),
    token_count bigint NOT NULL CHECK (token_count >= 0),
    PRIMARY KEY (text_hash, tokenizer_fingerprint)
);
CREATE TABLE release_token_stats (
    release_id bigint NOT NULL REFERENCES dataset_release,
    unit text NOT NULL DEFAULT 'document',
    tokenizer_fingerprint bytea NOT NULL CHECK (octet_length(tokenizer_fingerprint) = 32),
    tokenizer_name text NOT NULL,
    total bigint NOT NULL,
    by_split jsonb NOT NULL,
    by_source jsonb NOT NULL,
    by_category jsonb NOT NULL,
    percentiles jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (release_id, unit, tokenizer_fingerprint)
);
