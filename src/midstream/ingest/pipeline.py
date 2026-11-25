"""批次入库：登记、暂存、验证、发布。

中间文本放在 staging_text（lz4），发布时压缩成 zstd 写入 text_blob，
并在同一事务里生成版本、切换当前版本、删除暂存分区。
"""

import hashlib
import json
import multiprocessing
import re
import struct
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path
from time import perf_counter

import psycopg

from ..corpus import clean
from ..corpus import classify
from .. import store
from ..store import textcodec
from .adapters.base import Adapter, ParsedRecord
from ..config import CHUNK_DOCS, WORKERS


class IngestError(Exception):
    pass


class AlreadyImported(IngestError):
    pass


@dataclass(frozen=True)
class IngestReport:
    batch_id: int
    status: str
    records_ok: int
    records_failed: int
    new_documents: int
    new_versions: int
    unchanged: int
    plain_bytes: int
    stored_bytes: int
    timings: dict[str, float] = field(default_factory=dict)


def _sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.digest()


def _record_hash(record: ParsedRecord) -> bytes:
    canonical = json.dumps(
        {"fields": record.raw_fields, "text": record.text, "error": record.error},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


def _storable(text: str) -> bool:
    """PostgreSQL 的 text 不接受 NUL，也不接受代理码点。"""
    return _UNSTORABLE.search(text) is None


def _ensure_source(conn: psycopg.Connection, adapter: Adapter) -> int:
    row = conn.execute("SELECT source_id FROM source WHERE name = %s", (adapter.name,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO source (name, description, license_note) VALUES (%s, %s, %s)"
        " RETURNING source_id",
        (adapter.name, adapter.description, adapter.license_note),
    ).fetchone()[0]


def register_batch(conn: psycopg.Connection, adapter: Adapter, paths: list[Path], *, commit: bool = True) -> int:
    source_id = _ensure_source(conn, adapter)
    hashes = [(path, _sha256_file(path), path.stat().st_size) for path in paths]
    duplicate = conn.execute(
        """SELECT o.path FROM raw_object o
           JOIN source_batch b USING (batch_id)
           WHERE b.source_id = %s AND b.status = 'published' AND o.sha256 = ANY(%s)""",
        (source_id, [digest for _, digest, _ in hashes]),
    ).fetchone()
    if duplicate:
        raise AlreadyImported(f"同内容文件已发布过：{duplicate[0]}")
    batch_id = conn.execute(
        "INSERT INTO source_batch (source_id, parser_version, status)"
        " VALUES (%s, %s, 'received') RETURNING batch_id",
        (source_id, adapter.parser_version),
    ).fetchone()[0]
    for path, digest, size in hashes:
        conn.execute(
            "INSERT INTO raw_object (batch_id, path, sha256, size_bytes) VALUES (%s, %s, %s, %s)",
            (batch_id, str(path), digest, size),
        )
    if commit:
        conn.commit()
    return batch_id


def _cleaned_metadata(source: str, metadata: dict, text: str, raw_fields: dict | None = None) -> dict:
    """清洗版的元数据：来源值保留，规范标签和推断年份标明依据。"""
    metadata = RecordMetadata.model_validate(metadata).model_dump()
    derived = dict(metadata)
    derived.update(classify.merge(metadata, classify.classify(
        source, {**metadata, "raw_fields": raw_fields or {}, "text": text})))
    basis = dict(derived.get("field_basis", {}))
    status, flags = clean.assess(text)
    derived["quality_status"] = status
    derived["quality_flags"] = flags
    derived["field_basis"] = basis
    derived["processed_steps"] = {**step_flags(metadata.get("processed_steps")), "basic_clean": True}
    return RecordMetadata.model_validate(derived).model_dump()


def _scan(adapter) -> bool:
    return getattr(getattr(adapter, 'plan', None), 'format', None) == 'scan'


def _prepare(record: ParsedRecord, *, ocr: bool = False, scan: bool = False) -> tuple[ParsedRecord, str | None]:
    """返回 (记录, 清洗后正文)。不能入库的记录转成失败记录。"""
    if record.error is not None:
        return record, None
    try:
        metadata = RecordMetadata.model_validate(record.metadata).model_dump()
        record = replace(record, metadata=metadata)
    except ValueError:
        return _failed(record, "元数据未通过类型校验"), None
    text = record.text or ""
    if not _storable(text):
        return _failed(record, "正文含 NUL 或非法字符"), None
    cleaned = clean.clean_text(text, ocr=ocr, scan=scan)
    if not cleaned.strip():
        return _failed(record, "清洗后正文为空"), None
    return record, cleaned


def _failed(record: ParsedRecord, error: str) -> ParsedRecord:
    return ParsedRecord(record.source_key, record.locator, record.raw_fields, error=error)


def _ordered_map(pool, function, tasks):
    """限制在途块数；结果始终按输入顺序消费。"""
    tasks = iter(tasks)
    pending = deque(pool.submit(function, task) for task in islice(tasks, WORKERS * 2))
    while pending:
        yield pending.popleft().result()
        for task in islice(tasks, 1):
            pending.append(pool.submit(function, task))


def _copy_row(fields) -> bytes:
    def escape(value):
        if value is None:
            return r"\N"
        text = "\\x" + value.hex() if isinstance(value, bytes) else str(value)
        return text.replace("\\", "\\\\").replace("\t", r"\t").replace("\n", r"\n").replace("\r", r"\r")

    return ("\t".join(map(escape, fields)) + "\n").encode("utf-8")


def _stage_chunk(task) -> tuple[bytes, bytes]:
    adapter, batch_id, raw_id, chunk = task
    texts, records = bytearray(), bytearray()
    for parsed in adapter.parse_chunk(chunk):
        record, cleaned = _prepare(parsed, ocr=adapter.name.endswith('_ocr'), scan=_scan(adapter))
        if cleaned is not None:
            digest = textcodec.text_hash(record.text)
            same = cleaned == record.text
            texts.extend(_copy_row((
                batch_id, record.source_key, "decoded",
                json.dumps(record.metadata, ensure_ascii=False), record.text,
                digest, len(record.text),
            )))
            texts.extend(_copy_row((
                batch_id, record.source_key, "cleaned",
                json.dumps(_cleaned_metadata(adapter.name, record.metadata, cleaned, record.raw_fields), ensure_ascii=False),
                None if same else cleaned,
                digest if same else textcodec.text_hash(cleaned), len(cleaned),
            )))
        records.extend(_copy_row((
            batch_id, record.source_key, raw_id, json.dumps(record.locator),
            json.dumps(record.raw_fields, ensure_ascii=False), _record_hash(record),
            "failed" if record.error else "ok", record.error,
        )))
    return bytes(texts), bytes(records)


def stage(conn: psycopg.Connection, batch_id: int, adapter: Adapter,
          pool: ProcessPoolExecutor) -> None:
    """工作进程解析、清洗并序列化，主进程按原始块顺序 COPY。"""
    objects = conn.execute(
        "SELECT raw_id, path FROM raw_object WHERE batch_id = %s ORDER BY raw_id", (batch_id,)
    ).fetchall()
    conn.execute(
        f"CREATE TABLE staging_text_b{batch_id} PARTITION OF staging_text FOR VALUES IN ({batch_id})"
    )
    conn.execute(
        "INSERT INTO processing_run (batch_id, kind, rule_version, config, finished_at)"
        " VALUES (%s, 'parse', %s, %s, now()), (%s, 'clean', %s, %s, now())",
        (
            batch_id, adapter.parser_version, "{}",
            batch_id, clean.RULE_VERSION,
            json.dumps({"quality": clean.QUALITY_RULE_VERSION, "taxonomy": classify.MAP_VERSION, "maps": classify.versions(),
                        **({"scan": clean.SCAN_RULE_VERSION} if _scan(adapter) else {})}),
        ),
    )
    tasks = ((adapter, batch_id, raw_id, chunk)
             for raw_id, path in objects for chunk in adapter.iter_chunks(Path(path)))
    with conn.cursor() as cursor:
        for texts, records in _ordered_map(pool, _stage_chunk, tasks):
            with cursor.copy(
                "COPY staging_text (batch_id, source_key, step, metadata, content,"
                " text_hash, char_count) FROM STDIN"
            ) as copy:
                copy.write(texts)
            with cursor.copy(
                "COPY source_record (batch_id, source_key, raw_id, locator, raw_fields, record_hash,"
                " parse_status, parse_error) FROM STDIN"
            ) as copy:
                copy.write(records)
    conn.execute("UPDATE source_batch SET status = 'staged' WHERE batch_id = %s", (batch_id,))
    conn.commit()


def validate(conn: psycopg.Connection, batch_id: int) -> str:
    """检查暂存内容完整。不通过时批次进入 needs_review，不能发布。"""
    ok_records = conn.execute(
        "SELECT count(*) FROM source_record WHERE batch_id = %s AND parse_status = 'ok'",
        (batch_id,),
    ).fetchone()[0]
    steps = dict(
        conn.execute(
            "SELECT step, count(*) FROM staging_text WHERE batch_id = %s GROUP BY step",
            (batch_id,),
        ).fetchall()
    )
    complete = steps.get("decoded", 0) == ok_records and steps.get("cleaned", 0) == ok_records
    status = "validated" if complete and ok_records > 0 else "needs_review"
    conn.execute("UPDATE source_batch SET status = %s WHERE batch_id = %s", (status, batch_id))
    conn.commit()
    return status


def _run_id(conn: psycopg.Connection, batch_id: int, kind: str) -> int:
    return conn.execute(
        "SELECT run_id FROM processing_run WHERE batch_id = %s AND kind = %s", (batch_id, kind)
    ).fetchone()[0]


def _compress_blobs(rows) -> tuple[bytes, int, int]:
    output = bytearray()
    plain_bytes = stored_bytes = 0
    for digest, char_count, text in rows:
        data = textcodec.compress(text)
        size = len(text.encode("utf-8"))
        plain_bytes += size
        stored_bytes += len(data)
        output.extend(struct.pack("!h", 5))
        for value in (digest, textcodec.CODEC.encode(), struct.pack("!q", char_count),
                      struct.pack("!q", size), data):
            output.extend(struct.pack("!i", len(value)))
            output.extend(value)
    return bytes(output), plain_bytes, stored_bytes


def _write_blobs(conn: psycopg.Connection, batch_id: int, pool: ProcessPoolExecutor) -> tuple[int, int]:
    """每个新哈希只读一份正文，进程池压缩并生成二进制 COPY。"""
    conn.execute(
        "CREATE TEMP TABLE new_blob (LIKE text_blob INCLUDING STORAGE) ON COMMIT DROP"
    )
    query = """COPY (
        SELECT s.text_hash, s.char_count, s.content
        FROM (SELECT DISTINCT ON (text_hash) text_hash, source_key, step
              FROM staging_text st WHERE batch_id = %s AND content IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM text_blob b WHERE b.text_hash = st.text_hash)
              ORDER BY text_hash) fresh
        JOIN staging_text s ON s.batch_id = %s AND s.source_key = fresh.source_key
                           AND s.step = fresh.step
    ) TO STDOUT (FORMAT BINARY)"""
    plain_bytes = stored_bytes = 0
    # COPY 进行时同一连接不能读写；暂存分区已由 stage 提交。
    with store.reader(conn) as source, source.cursor() as reader:
        with reader.copy(query, (batch_id, batch_id)) as incoming, conn.cursor() as writer:
            incoming.set_types(["bytea", "int8", "text"])
            rows = incoming.rows()
            chunks = iter(lambda: list(islice(rows, CHUNK_DOCS)), [])
            with writer.copy(
                "COPY new_blob (text_hash, codec, char_count, byte_count, content)"
                " FROM STDIN (FORMAT BINARY)"
            ) as copy:
                copy.write(b"PGCOPY\n\xff\r\n\0" + bytes(8))
                for data, plain, stored in _ordered_map(pool, _compress_blobs, chunks):
                    copy.write(data)
                    plain_bytes += plain
                    stored_bytes += stored
                copy.write(b"\xff\xff")
    conn.execute("INSERT INTO text_blob (text_hash, codec, char_count, byte_count, content)"
                 " SELECT text_hash, codec, char_count, byte_count, content FROM new_blob"
                 " ON CONFLICT (text_hash) DO NOTHING")
    return plain_bytes, stored_bytes


from ..corpus.metadata import VERSION_FIELDS, RecordMetadata, version_sql, step_flags

_VERSION_COLUMNS = "doc_id, version_no, parent_version_id, text_hash, run_id, is_current, char_count, " + ', '.join(VERSION_FIELDS)

def _version_values(meta: str) -> str:
    return 'b.char_count, ' + version_sql(meta)


def publish(conn: psycopg.Connection, batch_id: int, pool: ProcessPoolExecutor, *,
            allow_uncurated: bool = False) -> IngestReport:
    """在一个事务里完成发布。"""
    timings = {}
    started = checkpoint = perf_counter()
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (batch_id,))

    def mark(name):
        nonlocal checkpoint
        now = perf_counter()
        timings[f"publish.{name}"] = now - checkpoint
        checkpoint = now

    status, source_id = conn.execute(
        "SELECT status, source_id FROM source_batch WHERE batch_id = %s FOR UPDATE", (batch_id,)
    ).fetchone()
    if status != "validated":
        raise IngestError(f"批次 {batch_id} 状态为 {status}，不能发布")

    from ..curation.ingestion import ensure_ready
    ensure_ready(conn, batch_id, allow_uncurated=allow_uncurated)
    mark("lock")
    # 大批量写入后统计信息过期，规划器会选出近似平方复杂度的计划
    conn.execute(f"ANALYZE source_record, document, staging_text_b{int(batch_id)}")
    parse_run = _run_id(conn, batch_id, "parse")
    clean_run = _run_id(conn, batch_id, "clean")
    mark("analyze_staging")
    plain_bytes, stored_bytes = _write_blobs(conn, batch_id, pool)
    mark("blobs")

    # 记录哈希和上次发布相同的记录，不产生新版本
    conn.execute(
        f"""CREATE TEMP TABLE changed ON COMMIT DROP AS
            SELECT r.source_key, r.record_id
            FROM source_record r
            LEFT JOIN LATERAL (
                SELECT p.record_hash FROM source_record p
                JOIN source_batch b ON b.batch_id = p.batch_id
                WHERE b.source_id = {int(source_id)} AND b.status = 'published'
                  AND p.source_key = r.source_key
                ORDER BY p.batch_id DESC LIMIT 1
            ) previous ON true
            WHERE r.batch_id = {int(batch_id)} AND r.parse_status = 'ok'
              AND (previous.record_hash IS DISTINCT FROM r.record_hash
                   OR EXISTS (SELECT 1 FROM curation_item ci WHERE ci.record_id=r.record_id AND ci.status='applied'))"""
    )
    mark("changed")
    new_documents = conn.execute(
        """INSERT INTO document (source_id, source_key, unit_type)
           SELECT %s, c.source_key, s.metadata->>'unit_type'
           FROM changed c
           JOIN staging_text s ON s.batch_id = %s AND s.source_key = c.source_key
                              AND s.step = 'decoded'
           ON CONFLICT (source_id, source_key) DO NOTHING""",
        (source_id, batch_id),
    ).rowcount
    mark("documents")
    conn.execute("ANALYZE document, changed")
    mark("analyze_documents")
    conn.execute(
        """UPDATE source_record r SET doc_id = d.doc_id
           FROM document d
           WHERE r.batch_id = %s AND r.parse_status = 'ok'
             AND d.source_id = %s AND d.source_key = r.source_key""",
        (batch_id, source_id),
    )
    mark("link_records")
    conn.execute(
        f"""CREATE TEMP TABLE publishing ON COMMIT DROP AS
            SELECT c.source_key, d.doc_id, st.metadata AS dec_meta, sc.metadata AS cln_meta,
                   st.text_hash AS dec_hash, sc.text_hash AS cln_hash,
                   coalesce(h.last_no, 0) AS last_no, h.current_id
            FROM changed c
            JOIN document d ON d.source_id = {int(source_id)} AND d.source_key = c.source_key
            JOIN staging_text st ON st.batch_id = {int(batch_id)} AND st.source_key = c.source_key
                                AND st.step = 'decoded'
            JOIN staging_text sc ON sc.batch_id = {int(batch_id)} AND sc.source_key = c.source_key
                                AND sc.step = 'cleaned'
            LEFT JOIN LATERAL (
                SELECT max(version_no) AS last_no,
                       max(version_id) FILTER (WHERE is_current) AS current_id
                FROM document_version v WHERE v.doc_id = d.doc_id
            ) h ON true"""
    )
    mark("publishing")
    conn.execute("ANALYZE publishing")
    mark("analyze_publishing")
    conn.execute("UPDATE document_version v SET is_current = false FROM publishing p"
                 " WHERE v.version_id = p.current_id")
    mark("current_versions")
    decoded = conn.execute(
        f"""INSERT INTO document_version ({_VERSION_COLUMNS})
            SELECT p.doc_id, p.last_no + 1, p.current_id, p.dec_hash, %s, false,
                   {_version_values('p.dec_meta')}
            FROM publishing p JOIN text_blob b ON b.text_hash = p.dec_hash""",
        (parse_run,),
    ).rowcount
    mark("decoded_versions")
    cleaned = conn.execute(
        f"""INSERT INTO document_version ({_VERSION_COLUMNS})
            SELECT p.doc_id, p.last_no + 2, parent.version_id, p.cln_hash, %s, true,
                   {_version_values('p.cln_meta')}
            FROM publishing p
            JOIN text_blob b ON b.text_hash = p.cln_hash
            JOIN document_version parent
              ON parent.doc_id = p.doc_id AND parent.version_no = p.last_no + 1""",
        (clean_run,),
    ).rowcount
    mark("cleaned_versions")
    conn.execute(
        """INSERT INTO work (canonical_title)
           SELECT DISTINCT p.cln_meta->>'work_title' FROM publishing p
           WHERE p.cln_meta->>'work_title' IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM work w
                             WHERE w.canonical_title = p.cln_meta->>'work_title')"""
    )
    conn.execute(
        """INSERT INTO document_work (doc_id, work_id, relation, basis, status)
           SELECT p.doc_id, w.work_id, p.cln_meta->>'work_relation', 'source', 'confirmed'
           FROM publishing p
           JOIN LATERAL (SELECT min(work_id) AS work_id FROM work
                         WHERE canonical_title = p.cln_meta->>'work_title') w ON true
           WHERE w.work_id IS NOT NULL
           ON CONFLICT DO NOTHING"""
    )
    mark("works")
    counts = conn.execute(
        """SELECT count(*) FILTER (WHERE parse_status = 'ok'),
                  count(*) FILTER (WHERE parse_status = 'failed')
           FROM source_record WHERE batch_id = %s""",
        (batch_id,),
    ).fetchone()
    changed_count = conn.execute("SELECT count(*) FROM changed").fetchone()[0]
    from ..curation.apply import bind_published
    bind_published(conn, batch_id)
    from ..curation.tracking import record_batch_clean
    record_batch_clean(conn, batch_id, clean_run)
    conn.execute(f"DROP TABLE staging_text_b{batch_id}")
    conn.execute(
        "UPDATE source_batch SET status = 'published', published_at = now() WHERE batch_id = %s",
        (batch_id,),
    )
    conn.commit()
    mark("finish")
    timings["publish"] = perf_counter() - started
    return IngestReport(
        batch_id=batch_id,
        status="published",
        records_ok=counts[0],
        records_failed=counts[1],
        new_documents=new_documents,
        new_versions=decoded + cleaned,
        unchanged=counts[0] - changed_count,
        plain_bytes=plain_bytes,
        stored_bytes=stored_bytes,
        timings=timings,
    )


def ingest(conn: psycopg.Connection, adapter: Adapter, paths: list[Path], *,
           curation=None, actor='system', reason='ingest') -> IngestReport:
    timings = {}
    # psycopg 带后台线程，fork 有死锁风险；暂存与发布共用一个 spawn 进程池。
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=WORKERS, mp_context=context) as pool:
        started = perf_counter()
        batch_id = register_batch(conn, adapter, paths)
        if curation is not None:
            from ..curation.ingestion import configure_batch
            configure_batch(conn, batch_id, curation)
            conn.commit()
        timings["register"] = perf_counter() - started
        started = perf_counter()
        try:
            stage(conn, batch_id, adapter, pool)
        except Exception:
            conn.rollback()
            conn.execute("UPDATE source_batch SET status = 'failed' WHERE batch_id = %s", (batch_id,))
            conn.commit()
            raise
        timings["stage"] = perf_counter() - started
        started = perf_counter()
        status = validate(conn, batch_id)
        timings["validate"] = perf_counter() - started
        if status != "validated":
            raise IngestError(f"批次 {batch_id} 验证未通过，状态 {status}")
        from ..curation.ingestion import curate_batch
        curate_batch(conn, batch_id, actor=actor, reason=reason)
        report = publish(conn, batch_id, pool)
    # 查重由 ingest 操作登记为后续任务。
    return replace(report, timings={**timings, **report.timings})
