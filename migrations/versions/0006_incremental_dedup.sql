-- 缓存精确 Jaccard 分数，包含未通过阈值的候选。
-- 比较规则包含归一化及 8-gram 定义；阈值变化可复用分数。
CREATE UNLOGGED TABLE dedup_pair (
    rule_version text NOT NULL,
    left_hash bytea NOT NULL CHECK (octet_length(left_hash) = 16),
    right_hash bytea NOT NULL CHECK (octet_length(right_hash) = 16),
    similarity double precision NOT NULL CHECK (similarity >= 0 AND similarity <= 1),
    PRIMARY KEY (rule_version, left_hash, right_hash),
    CHECK (left_hash < right_hash)
);

ALTER TABLE dup_cluster ADD COLUMN anchor_version_id bigint;
UPDATE dup_cluster c SET anchor_version_id = m.anchor
FROM (SELECT cluster_id, min(version_id) AS anchor FROM dup_cluster_member GROUP BY cluster_id) m
WHERE c.cluster_id = m.cluster_id AND c.level = 'document';
CREATE UNIQUE INDEX dup_cluster_anchor ON dup_cluster (level, anchor_version_id);

-- 输入摘要覆盖完整的当前版本集合；两类查重独立判断是否可以跳过。
CREATE TABLE dedup_snapshot (
    level text PRIMARY KEY CHECK (level IN ('document', 'paragraph')),
    input_digest bytea NOT NULL CHECK (octet_length(input_digest) = 32),
    parameters jsonb NOT NULL,
    run_id bigint NOT NULL REFERENCES processing_run
);
