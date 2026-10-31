ALTER TABLE dataset_release
    ADD COLUMN char_count bigint CHECK (char_count >= 0),
    ADD COLUMN train_char_count bigint CHECK (train_char_count >= 0),
    ADD COLUMN val_char_count bigint CHECK (val_char_count >= 0),
    ADD COLUMN test_char_count bigint CHECK (test_char_count >= 0);

-- 按已固定的成员版本回填，不读取当前版本，也不解压正文。
UPDATE dataset_release r
SET char_count = counts.total,
    train_char_count = counts.train,
    val_char_count = counts.val,
    test_char_count = counts.test
FROM (
    SELECT r.release_id,
           coalesce(sum(v.char_count), 0) AS total,
           coalesce(sum(v.char_count) FILTER (WHERE m.split = 'train'), 0) AS train,
           coalesce(sum(v.char_count) FILTER (WHERE m.split = 'val'), 0) AS val,
           coalesce(sum(v.char_count) FILTER (WHERE m.split = 'test'), 0) AS test
    FROM dataset_release r
    LEFT JOIN dataset_member m USING (release_id)
    LEFT JOIN document_version v USING (version_id)
    GROUP BY r.release_id
) counts
WHERE r.release_id = counts.release_id;

ALTER TABLE dataset_release
    ALTER COLUMN char_count SET NOT NULL,
    ALTER COLUMN train_char_count SET NOT NULL,
    ALTER COLUMN val_char_count SET NOT NULL,
    ALTER COLUMN test_char_count SET NOT NULL,
    ADD CONSTRAINT release_char_count_sum
        CHECK (char_count::numeric = train_char_count::numeric + val_char_count + test_char_count);
