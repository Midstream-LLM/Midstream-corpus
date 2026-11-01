"""当前版本的文档级和段落级查重，各自保存最新快照。"""

import hashlib
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


def _input_digest(conn) -> bytes:
    digest = hashlib.sha256()
    # 版本不可变，按 ID 排序的完整集合可识别新增、替换及移除；不读取正文。
    _read(conn, "COPY (SELECT version_id FROM document_version WHERE is_current"
          " ORDER BY version_id) TO STDOUT (FORMAT BINARY)", None, digest.update)
    return digest.digest()


def document_snapshot_current(conn) -> bool:
    """文档级快照是否覆盖全部当前版本。"""
    row = conn.execute(
        "SELECT input_digest FROM dedup_snapshot WHERE level = 'document'"
    ).fetchone()
    return row is not None and bytes(row[0]) == _input_digest(conn)


def _reuse_snapshot(conn, level, digest, parameters, started):
    row = conn.execute(
        "SELECT s.input_digest, s.parameters, s.run_id, r.config"
        " FROM dedup_snapshot s JOIN processing_run r USING (run_id) WHERE s.level = %s",
        (level,),
    ).fetchone()
    if row is None or bytes(row[0]) != digest or row[1] != parameters:
        return None
    report = dict(row[3])
    for key in report:
        if key.endswith("_seconds") or key in {
            "fingerprinted", "compare_fingerprinted", "candidates", "verified_pairs",
            "cached_pairs", "computed_pairs", "clusters_changed", "members_written", "members_deleted",
        }:
            report[key] = 0
    report.update(reused_snapshot=True, input_seconds=perf_counter() - started,
                  total_seconds=perf_counter() - started)
    return {"run_id": row[2], **report}


def _save_snapshot(conn, level, digest, parameters, run_id):
    conn.execute(
        "INSERT INTO dedup_snapshot (level, input_digest, parameters, run_id) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (level) DO UPDATE SET input_digest = EXCLUDED.input_digest,"
        " parameters = EXCLUDED.parameters, run_id = EXCLUDED.run_id",
        (level, digest, Jsonb(parameters), run_id),
    )


def _copy_bytes(conn, table, data):
    with conn.cursor() as cursor, cursor.copy(f"COPY {table} FROM STDIN (FORMAT BINARY)") as copy:
        copy.write(data)


def _write_clusters(conn, state, run_id, count):
    """临时表接收完整结果，只对持久表写实际发生的成员变化。"""
    conn.execute("CREATE TEMP TABLE dedup_result (cluster_key bigint, version_id bigint, kind text)"
                 " ON COMMIT DROP")
    blocks = []
    _read(conn, "COPY (SELECT generate_series(1, %s)::bigint) TO STDOUT (FORMAT BINARY)",
          (count,), lambda block: blocks.append(state.members(block)))
    with conn.cursor() as cursor, cursor.copy(
        "COPY dedup_result FROM STDIN (FORMAT BINARY)"
    ) as copy:
        copy.write(state.copy_header())
        for block in blocks:
            copy.write(block)
        copy.write(state.copy_end())
    conn.execute("ANALYZE dedup_result")
    conn.execute("CREATE TEMP TABLE dedup_anchors ON COMMIT DROP AS"
                 " SELECT cluster_key, min(version_id) AS anchor FROM dedup_result GROUP BY cluster_key")
    conn.execute("ANALYZE dedup_anchors")
    conn.execute(
        "INSERT INTO dup_cluster (run_id, level, anchor_version_id)"
        " SELECT %s, 'document', a.anchor FROM dedup_anchors a"
        " WHERE NOT EXISTS (SELECT 1 FROM dup_cluster c"
        "                   WHERE c.level = 'document' AND c.anchor_version_id = a.anchor)"
        " ON CONFLICT (level, anchor_version_id) DO NOTHING", (run_id,),
    )
    conn.execute(
        "CREATE TEMP TABLE dedup_desired ON COMMIT DROP AS"
        " SELECT c.cluster_id, r.version_id, r.kind FROM dedup_result r"
        " JOIN dedup_anchors a USING (cluster_key)"
        " JOIN dup_cluster c ON c.anchor_version_id = a.anchor AND c.level = 'document'"
    )
    conn.execute("ALTER TABLE dedup_desired ADD PRIMARY KEY (cluster_id, version_id)")
    conn.execute("ANALYZE dedup_desired")
    conn.execute(
        "CREATE TEMP TABLE dedup_changed ON COMMIT DROP AS"
        " SELECT DISTINCT coalesce(m.cluster_id, n.cluster_id) AS cluster_id"
        " FROM (SELECT m.cluster_id, m.version_id, m.kind FROM dup_cluster_member m"
        "       JOIN dup_cluster c USING (cluster_id) WHERE c.level = 'document') m"
        " FULL JOIN dedup_desired n USING (cluster_id, version_id) WHERE m.kind IS DISTINCT FROM n.kind"
    )
    changed = conn.execute("SELECT count(*) FROM dedup_changed").fetchone()[0]
    deleted = conn.execute(
        "DELETE FROM dup_cluster_member m USING dup_cluster c"
        " WHERE m.cluster_id = c.cluster_id AND c.level = 'document'"
        " AND NOT EXISTS (SELECT 1 FROM dedup_desired n"
        "                 WHERE n.cluster_id = m.cluster_id AND n.version_id = m.version_id)"
    ).rowcount
    written = conn.execute(
        "INSERT INTO dup_cluster_member (cluster_id, version_id, kind)"
        " SELECT n.cluster_id, n.version_id, n.kind FROM dedup_desired n"
        " WHERE NOT EXISTS (SELECT 1 FROM dup_cluster_member m WHERE m.cluster_id = n.cluster_id"
        "                   AND m.version_id = n.version_id AND m.kind = n.kind)"
        " ON CONFLICT (cluster_id, version_id) DO UPDATE SET kind = EXCLUDED.kind"
        " WHERE dup_cluster_member.kind IS DISTINCT FROM EXCLUDED.kind"
    ).rowcount
    conn.execute("UPDATE dup_cluster c SET run_id = %s FROM dedup_changed d"
                 " WHERE c.cluster_id = d.cluster_id", (run_id,))
    conn.execute("DELETE FROM dup_cluster c WHERE c.level = 'document'"
                 " AND NOT EXISTS (SELECT 1 FROM dup_cluster_member m WHERE m.cluster_id = c.cluster_id)")
    return {"clusters_changed": changed, "members_written": written, "members_deleted": deleted}


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


def dedup(conn: psycopg.Connection, *, full: bool = False) -> dict:
    report = _dedup(conn, full=full)
    conn.commit()
    return report


def _dedup(conn: psycopg.Connection, *, full: bool) -> dict:
    started = perf_counter()
    state = midstream_dedup.Deduper(RULE_VERSION, BANDS, ROWS, THRESHOLD)
    report = {"rule_version": RULE_VERSION, "threshold": THRESHOLD, "bands": BANDS, "rows": ROWS}
    parameters = {**report, "algorithm": "lsh-rotation-v1"}
    # 同一连接不能同时 COPY 读写。source 只读取已经提交的正文。
    with conn.transaction(), psycopg.connect(conn.info.dsn) as source:
        # 文档和段落流程共用指纹缓存，避免两个 CLI 同时写同一份缓存。
        conn.execute("SELECT pg_advisory_xact_lock(762031)")
        digest = _input_digest(conn)
        if not full and (reused := _reuse_snapshot(conn, "document", digest, parameters, started)):
            return reused
        report.update(reused_snapshot=False, input_seconds=perf_counter() - started)
        stage = perf_counter()
        _fingerprints(source, conn, state)
        report["fingerprinted"] = state.fingerprinted
        report["fingerprint_seconds"] = perf_counter() - stage
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
        conn.execute("CREATE TEMP TABLE dedup_pairs (left_hash bytea, right_hash bytea) ON COMMIT DROP")
        conn.execute("CREATE TEMP TABLE dedup_pair_result"
                     " (left_hash bytea, right_hash bytea, similarity double precision) ON COMMIT DROP")
        while state.next_round():
            while batch := state.next_batch(config.CHUNK_DOCS * 5):
                if not full:
                    conn.execute("TRUNCATE dedup_pairs")
                    _copy_bytes(conn, "dedup_pairs", state.candidate_pairs())
                    _read(conn,
                          "COPY (SELECT p.left_hash, p.right_hash, c.similarity FROM dedup_pairs p"
                          " JOIN dedup_pair c USING (left_hash, right_hash) WHERE c.rule_version = %s)"
                          " TO STDOUT (FORMAT BINARY)", (RULE_VERSION,), state.cached_pairs)
                    batch = state.uncached_batch()
                    if batch is None:
                        continue
                conn.execute("TRUNCATE dedup_verify")
                _copy_bytes(conn, "dedup_verify", batch)
                _read(
                    conn,
                    """COPY (SELECT v.version_id, b.content FROM dedup_verify d
                       JOIN document_version v USING (version_id)
                       JOIN text_blob b USING (text_hash)) TO STDOUT (FORMAT BINARY)""",
                    None, state.blobs,
                )
                conn.execute("TRUNCATE dedup_pair_result")
                _copy_bytes(conn, "dedup_pair_result", state.verify_batch())
                conn.execute(
                    "INSERT INTO dedup_pair (rule_version, left_hash, right_hash, similarity)"
                    " SELECT %s, left_hash, right_hash, similarity FROM dedup_pair_result"
                    " ON CONFLICT (rule_version, left_hash, right_hash) DO UPDATE"
                    " SET similarity = EXCLUDED.similarity"
                    " WHERE dedup_pair.similarity IS DISTINCT FROM EXCLUDED.similarity", (RULE_VERSION,),
                )
        report["verify_seconds"] = perf_counter() - stage
        report["cached_pairs"] = state.cache_hits
        report["computed_pairs"] = state.computed_pairs
        stage = perf_counter()
        (report["clusters"], report["members"], report["candidates"],
         report["verified_pairs"]) = state.clusters()
        run_id = conn.execute(
            "INSERT INTO processing_run (kind, rule_version) VALUES ('dedup', %s) RETURNING run_id",
            (RULE_VERSION,),
        ).fetchone()[0]
        report.update(_write_clusters(conn, state, run_id, report["clusters"]))
        _save_snapshot(conn, "document", digest, parameters, run_id)
        report["write_seconds"] = perf_counter() - stage
        report["total_seconds"] = perf_counter() - started
        conn.execute(
            "UPDATE processing_run SET config = %s, finished_at = now() WHERE run_id = %s",
            (Jsonb(report), run_id),
        )
    return {"run_id": run_id, **report}


def dedup_spans(conn: psycopg.Connection, *, full: bool = False) -> dict:
    report = _dedup_spans(conn, full=full)
    conn.commit()
    return report


def _dedup_spans(conn: psycopg.Connection, *, full: bool) -> dict:
    started = perf_counter()
    report = {"rule_version": "span-v1", "k": 32, "w": 32, "min_span": 64}
    parameters = {**report, "comparison_rule": RULE_VERSION}
    directory = tempfile.mkdtemp(prefix="midstream-spans-")
    try:
        with conn.transaction(), psycopg.connect(conn.info.dsn) as source:
            conn.execute("SELECT pg_advisory_xact_lock(762031)")
            digest = _input_digest(conn)
            if not full and (reused := _reuse_snapshot(conn, "paragraph", digest, parameters, started)):
                return reused
            report.update(reused_snapshot=False, input_seconds=perf_counter() - started)
            stage = perf_counter()
            fingerprints = midstream_dedup.Deduper(RULE_VERSION, BANDS, ROWS, THRESHOLD)
            _fingerprints(source, conn, fingerprints)
            report["compare_fingerprinted"] = fingerprints.fingerprinted
            report["compare_fingerprint_seconds"] = perf_counter() - stage
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
            _save_snapshot(conn, "paragraph", digest, parameters, run_id)
            report["total_seconds"] = perf_counter() - started
            conn.execute(
                "UPDATE processing_run SET config = %s, finished_at = now() WHERE run_id = %s",
                (Jsonb(report), run_id),
            )
        return {"run_id": run_id, **report}
    finally:
        shutil.rmtree(directory)
