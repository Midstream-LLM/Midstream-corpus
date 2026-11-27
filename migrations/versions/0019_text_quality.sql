-- 正文缺字统计：去重时决定保留哪一份要用，导出试算只读元数据，所以预先算好。
-- 与正文一一对应（按 text_hash），规则变了整表重算。
CREATE TABLE text_quality (
    text_hash     bytea PRIMARY KEY REFERENCES text_blob (text_hash) ON DELETE CASCADE,
    rule_version  text NOT NULL,
    missing_chars integer NOT NULL CHECK (missing_chars >= 0)
);
