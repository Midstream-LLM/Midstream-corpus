-- 正文字形统计：导出去重时繁体本和简体本分开选，导出试算只读元数据，所以预先算好。
-- 只存计数，是否算繁体由代码里的阈值判断；规则变了整表重算。
CREATE TABLE text_script (
    text_hash         bytea PRIMARY KEY REFERENCES text_blob (text_hash) ON DELETE CASCADE,
    rule_version      text NOT NULL,
    han_chars         integer NOT NULL CHECK (han_chars >= 0),
    traditional_chars integer NOT NULL CHECK (traditional_chars >= 0 AND traditional_chars <= han_chars)
);
