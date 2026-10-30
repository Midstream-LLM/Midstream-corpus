"""当前版本的文档级和段落级查重，各自保存最新快照。"""

import shutil
import tempfile
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


def _fingerprints(source, conn, state):
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


def dedup(conn: psycopg.Connection) -> dict:
    started = perf_counter()
    state = midstream_dedup.Deduper(RULE_VERSION, BANDS, ROWS, THRESHOLD)
    report = {"rule_version": RULE_VERSION, "threshold": THRESHOLD, "bands": BANDS, "rows": ROWS}
    # 同一连接不能同时 COPY 读写。source 只读取已经提交的正文。
    with conn.transaction(), psycopg.connect(conn.info.dsn) as source:
        _fingerprints(source, conn, state)
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


def dedup_spans(conn: psycopg.Connection) -> dict:
    started = perf_counter()
    report = {"rule_version": "span-v1", "k": 32, "w": 32, "min_span": 64}
    directory = tempfile.mkdtemp(prefix="midstream-spans-")
    try:
        with conn.transaction(), psycopg.connect(conn.info.dsn) as source:
            fingerprints = midstream_dedup.Deduper(RULE_VERSION, BANDS, ROWS, THRESHOLD)
            _fingerprints(source, conn, fingerprints)
            report["compare_fingerprinted"] = fingerprints.fingerprinted
            report["compare_fingerprint_seconds"] = perf_counter() - started
            del fingerprints
            stage = perf_counter()
            state = midstream_dedup.SpanDeduper(directory)
            # 与 Deduper.group_exact 同样按 compare_hash 取最小 version_id；
            # 由 PostgreSQL 分组，避免为段落查重加载无用的 512 字节 OPH 签名。
            _read(
                conn,
                """COPY (SELECT v.version_id, b.content
                   FROM (SELECT min(v.version_id) AS version_id
                         FROM document_version v JOIN text_fingerprint f USING (text_hash)
                         WHERE v.is_current AND v.char_count >= 64 AND f.rule_version = %s
                         GROUP BY f.compare_hash) rep
                   JOIN document_version v USING (version_id)
                   JOIN text_blob b USING (text_hash)
                   ORDER BY v.version_id) TO STDOUT (FORMAT BINARY)""",
                (RULE_VERSION,), state.fingerprints,
            )
            (report["representatives"], report["eligible_documents"],
             report["fingerprints"]) = state.counts()
            report["fingerprint_seconds"] = perf_counter() - stage
            stage = perf_counter()
            report["seeds"], report["template_hashes"] = state.seeds()
            report["seed_seconds"] = perf_counter() - stage
            stage = perf_counter()
            report["matches"], report["spans"] = state.verify()
            report["verify_seconds"] = perf_counter() - stage
            stage = perf_counter()
            run_id = conn.execute(
                "INSERT INTO processing_run (kind, rule_version) VALUES ('dedup', 'span-v1')"
                " RETURNING run_id"
            ).fetchone()[0]
            conn.execute("TRUNCATE dup_span")
            with conn.cursor() as cursor, cursor.copy(
                "COPY dup_span (run_id, version_id, start_char, end_char,"
                " source_version_id, source_start_char) FROM STDIN (FORMAT BINARY)"
            ) as copy:
                copy.write(midstream_dedup.Deduper.copy_header())
                while block := state.next_output(run_id):
                    copy.write(block)
                copy.write(midstream_dedup.Deduper.copy_end())
            report["write_seconds"] = perf_counter() - stage
            report["total_seconds"] = perf_counter() - started
            conn.execute(
                "UPDATE processing_run SET config = %s, finished_at = now() WHERE run_id = %s",
                (Jsonb(report), run_id),
            )
        conn.commit()
        return {"run_id": run_id, **report}
    finally:
        shutil.rmtree(directory)
