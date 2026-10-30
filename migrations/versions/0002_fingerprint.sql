-- 指纹是可重建的派生数据：不写 WAL，不设外键，崩溃后清空，下次查重自动重算。
DROP TABLE text_fingerprint;
CREATE UNLOGGED TABLE text_fingerprint (
    text_hash    bytea NOT NULL,
    rule_version text NOT NULL,
    compare_hash bytea NOT NULL,
    signature    bytea NOT NULL,
    PRIMARY KEY (text_hash, rule_version)
);
