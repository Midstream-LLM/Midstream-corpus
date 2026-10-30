"""当前版本的文档级查重，每次运行写一份完整快照。"""

from time import perf_counter

import midstream_dedup
import psycopg
from psycopg.types.json import Jsonb

from . import config

RULE_VERSION = "compare-v1"
THRESHOLD = 0.8
BANDS = 32
ROWS = 4


def _read(conn, query, params, consume):
    # PostgreSQL 的 CopyData 常常一行一块；合成大块后交给 Rust，Python 不解析行。
    with conn.cursor() as cursor, cursor.copy(query, params) as copy:
        buffer = bytearray()
        for block in copy:
            buffer.extend(block)
            if len(buffer) >= 8 << 20:
                consume(bytes(buffer))
                buffer.clear()
        if buffer:
            consume(bytes(buffer))


def _transform(source, conn, query, params, table, transform):
    with conn.cursor() as cursor, cursor.copy(f"COPY {table} FROM STDIN (FORMAT BINARY)") as copy:
        copy.write(midstream_dedup.Deduper.copy_header())
        _read(source, query, params, lambda block: copy.write(transform(block)))
        copy.write(midstream_dedup.Deduper.copy_end())


def dedup(conn: psycopg.Connection) -> dict:
    started = perf_counter()
    state = midstream_dedup.Deduper(RULE_VERSION, BANDS, ROWS, THRESHOLD)
    report = {"rule_version": RULE_VERSION, "threshold": THRESHOLD, "bands": BANDS, "rows": ROWS}
    # 同一连接不能同时 COPY 读写。source 只读取已经提交的正文。
    with conn.transaction(), psycopg.connect(conn.info.dsn) as source:
        _transform(
            source, conn,
            """COPY (SELECT b.text_hash, b.content FROM text_blob b
               WHERE NOT EXISTS (SELECT 1 FROM text_fingerprint f
                                 WHERE f.text_hash = b.text_hash AND f.rule_version = %s)
                 AND EXISTS (SELECT 1 FROM document_version v
                             WHERE v.text_hash = b.text_hash AND v.is_current)
               ORDER BY b.text_hash) TO STDOUT (FORMAT BINARY)""",
            (RULE_VERSION,),
            "text_fingerprint (text_hash, rule_version, compare_hash, signature)",
            state.fingerprints,
        )
        report["fingerprinted"] = state.fingerprinted
        report["fingerprint_seconds"] = perf_counter() - started
        stage = perf_counter()
        _read(
            conn,
            """COPY (SELECT v.version_id, f.compare_hash, f.signature
               FROM document_version v JOIN text_fingerprint f USING (text_hash)
               WHERE v.is_current AND f.rule_version = %s) TO STDOUT (FORMAT BINARY)""",
            (RULE_VERSION,), state.versions,
        )
        report["documents"], report["representatives"], report["exact_groups"] = state.group_exact()
        report["exact_seconds"] = perf_counter() - stage
        stage = perf_counter()
        state.build_lsh()
        report["lsh_seconds"] = perf_counter() - stage
        stage = perf_counter()
        conn.execute("CREATE TEMP TABLE dedup_verify (version_id bigint PRIMARY KEY) ON COMMIT DROP")
        while state.next_round():
            while batch := state.next_batch(config.CHUNK_DOCS * 5):
                conn.execute("TRUNCATE dedup_verify")
                with conn.cursor() as cursor, cursor.copy(
                    "COPY dedup_verify FROM STDIN (FORMAT BINARY)"
                ) as copy:
                    copy.write(batch)
                _read(
                    conn,
                    """COPY (SELECT v.version_id, b.content FROM dedup_verify d
                       JOIN document_version v USING (version_id)
                       JOIN text_blob b USING (text_hash)) TO STDOUT (FORMAT BINARY)""",
                    None, state.blobs,
                )
                state.verify_batch()
        report["verify_seconds"] = perf_counter() - stage
        stage = perf_counter()
        (report["clusters"], report["members"], report["candidates"],
         report["verified_pairs"]) = state.clusters()
        run_id = conn.execute(
            "INSERT INTO processing_run (kind, rule_version) VALUES ('dedup', %s) RETURNING run_id",
            (RULE_VERSION,),
        ).fetchone()[0]
        # 只保留最新快照；已发布数据集固定在版本上，不依赖旧簇。
        conn.execute("TRUNCATE dup_cluster_member, dup_cluster")
        conn.execute(
            "INSERT INTO dup_cluster (run_id, level)"
            " SELECT %s, 'document' FROM generate_series(1, %s)",
            (run_id, report["clusters"]),
        )
        # 簇尚未提交，在本连接读 ID；成员字节暂存后再 COPY 写入。
        blocks = []
        _read(
            conn,
            "COPY (SELECT cluster_id FROM dup_cluster WHERE run_id = %s ORDER BY cluster_id)"
            " TO STDOUT (FORMAT BINARY)",
            (run_id,), lambda block: blocks.append(state.members(block)),
        )
        with conn.cursor() as cursor, cursor.copy(
            "COPY dup_cluster_member (cluster_id, version_id, kind) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.write(state.copy_header())
            for block in blocks:
                copy.write(block)
            copy.write(state.copy_end())
        report["write_seconds"] = perf_counter() - stage
        report["total_seconds"] = perf_counter() - started
        conn.execute(
            "UPDATE processing_run SET config = %s, finished_at = now() WHERE run_id = %s",
            (Jsonb(report), run_id),
        )
    conn.commit()
    return {"run_id": run_id, **report}
