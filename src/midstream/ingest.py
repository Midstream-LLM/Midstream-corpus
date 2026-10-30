"""批次入库：登记、暂存、验证、发布。

中间文本放在 staging_text（lz4），发布时压缩成 zstd 写入 text_blob，
并在同一事务里生成版本、切换当前版本、删除暂存分区。
"""

import hashlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator

import psycopg

from . import clean, dedup, eras, textcodec
from .adapters.base import Adapter, ParsedRecord
from .config import CHUNK_DOCS, WORKERS


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
    dedup: dict | None = None


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


def _storable(text: str) -> bool:
    """PostgreSQL 的 text 不接受 NUL，也不接受代理码点。"""
    return "\x00" not in text and not any("\ud800" <= ch <= "\udfff" for ch in text)


def _ensure_source(conn: psycopg.Connection, adapter: Adapter) -> int:
    row = conn.execute("SELECT source_id FROM source WHERE name = %s", (adapter.name,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO source (name, description, license_note) VALUES (%s, %s, %s)"
        " RETURNING source_id",
        (adapter.name, adapter.description, adapter.license_note),
    ).fetchone()[0]


def register_batch(conn: psycopg.Connection, adapter: Adapter, paths: list[Path]) -> int:
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
    conn.commit()
    return batch_id


def _cleaned_metadata(metadata: dict, text: str) -> dict:
    """清洗版的元数据：来源值保留，规则推断值标明依据。"""
    derived = dict(metadata)
    basis = dict(metadata.get("field_basis", {}))
    years = eras.era_years(metadata.get("composed_label"))
    if years:
        derived["composed_years"] = list(years)
        basis["composed_years"] = "rule"
    status, flags = clean.assess(text)
    derived["quality_status"] = status
    derived["quality_flags"] = flags
    derived["field_basis"] = basis
    return derived


def _prepare(record: ParsedRecord) -> tuple[ParsedRecord, str | None]:
    """返回 (记录, 清洗后正文)。不能入库的记录转成失败记录。"""
    if record.error is not None:
        return record, None
    text = record.text or ""
    if not _storable(text):
        return _failed(record, "正文含 NUL 或非法字符"), None
    cleaned = clean.clean_text(text)
    if not cleaned.strip():
        return _failed(record, "清洗后正文为空"), None
    return record, cleaned


def _failed(record: ParsedRecord, error: str) -> ParsedRecord:
    return ParsedRecord(record.source_key, record.locator, record.raw_fields, error=error)


def stage(conn: psycopg.Connection, batch_id: int, adapter: Adapter) -> None:
    """解析并写入暂存分区。"""
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
            json.dumps({"quality": clean.QUALITY_RULE_VERSION, "era": eras.RULE_VERSION}),
        ),
    )
    for raw_id, path in objects:
        chunk: list[ParsedRecord] = []
        for parsed in adapter.parse(Path(path)):
            chunk.append(parsed)
            if len(chunk) >= CHUNK_DOCS:
                _stage_chunk(conn, batch_id, raw_id, chunk)
                chunk = []
        if chunk:
            _stage_chunk(conn, batch_id, raw_id, chunk)
    conn.execute("UPDATE source_batch SET status = 'staged' WHERE batch_id = %s", (batch_id,))
    conn.commit()


def _stage_chunk(
    conn: psycopg.Connection, batch_id: int, raw_id: int, chunk: list[ParsedRecord]
) -> None:
    """分块写入，单次只在内存里保留一块。"""
    prepared = [_prepare(parsed) for parsed in chunk]
    with conn.cursor() as cursor:
        with cursor.copy(
            "COPY staging_text (batch_id, source_key, step, metadata, content) FROM STDIN"
        ) as copy:
            for record, cleaned in prepared:
                if cleaned is None:
                    continue
                copy.write_row(
                    (batch_id, record.source_key, "decoded",
                     json.dumps(record.metadata, ensure_ascii=False), record.text)
                )
                copy.write_row(
                    (batch_id, record.source_key, "cleaned",
                     json.dumps(_cleaned_metadata(record.metadata, cleaned), ensure_ascii=False),
                     cleaned)
                )
        with cursor.copy(
            "COPY source_record (batch_id, source_key, raw_id, locator, raw_fields, record_hash,"
            " parse_status, parse_error) FROM STDIN"
        ) as copy:
            for record, _ in prepared:
                copy.write_row(
                    (batch_id, record.source_key, raw_id,
                     json.dumps(record.locator),
                     json.dumps(record.raw_fields, ensure_ascii=False),
                     _record_hash(record),
                     "failed" if record.error else "ok", record.error)
                )


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


def _chunks(cursor) -> Iterator[list[tuple]]:
    while rows := cursor.fetchmany(CHUNK_DOCS):
        yield rows


def _write_blobs(conn: psycopg.Connection, batch_id: int, pool: ProcessPoolExecutor) -> tuple[int, int]:
    """把本批次还没有的正文压缩后写入 text_blob，返回 (明文字节, 压缩后字节)。"""
    conn.execute(
        "CREATE TEMP TABLE new_blob (LIKE text_blob INCLUDING STORAGE) ON COMMIT DROP"
    )
    seen: set[bytes] = set()
    plain_bytes = stored_bytes = 0

    def fresh_chunks(reader, lookup) -> Iterator[list[tuple[bytes, str]]]:
        for rows in _chunks(reader):
            fresh = []
            for (text,) in rows:
                digest = textcodec.text_hash(text)
                if digest not in seen:
                    seen.add(digest)
                    fresh.append((digest, text))
            if not fresh:
                continue
            known = {
                bytes(row[0])
                for row in lookup.execute(
                    "SELECT text_hash FROM text_blob WHERE text_hash = ANY(%s)",
                    ([digest for digest, _ in fresh],),
                )
            }
            todo = [item for item in fresh if item[0] not in known]
            if todo:
                yield todo

    def write(copy, window: list[list[tuple[bytes, str]]]) -> None:
        nonlocal plain_bytes, stored_bytes
        results = pool.map(textcodec.compress_many, [[t for _, t in c] for c in window])
        for chunk, compressed in zip(window, results):
            for (digest, text), data in zip(chunk, compressed):
                size = len(text.encode("utf-8"))
                plain_bytes += size
                stored_bytes += len(data)
                copy.write_row((digest, textcodec.CODEC, len(text), size, data))

    # COPY 进行时同一连接不能读游标，所以暂存内容用单独的连接读取
    with psycopg.connect(conn.info.dsn) as source, conn.cursor() as writer:
        with source.cursor(name="staged_text") as reader, writer.copy(
            "COPY new_blob (text_hash, codec, char_count, byte_count, content) FROM STDIN"
        ) as copy:
            reader.execute("SELECT content FROM staging_text WHERE batch_id = %s", (batch_id,))
            lookup = psycopg.connect(conn.info.dsn, autocommit=True)
            try:
                window: list[list[tuple[bytes, str]]] = []
                for todo in fresh_chunks(reader, lookup):
                    window.append(todo)
                    if len(window) >= WORKERS:
                        write(copy, window)
                        window = []
                if window:
                    write(copy, window)
            finally:
                lookup.close()
    conn.execute("INSERT INTO text_blob (text_hash, codec, char_count, byte_count, content)"
                 " SELECT text_hash, codec, char_count, byte_count, content FROM new_blob")
    return plain_bytes, stored_bytes


_VERSION_COLUMNS = """doc_id, version_no, parent_version_id, text_hash, run_id, is_current,
    quality_status, quality_flags, char_count, license, title, authors, composed_years,
    composed_label, edition_years, genre, category, topics, legacy_category, field_basis, extra"""


def _version_values(meta: str) -> str:
    """从暂存 metadata 取出版本字段的 SQL 表达式。"""
    return f"""
    coalesce({meta}->>'quality_status', 'ok'),
    coalesce(ARRAY(SELECT jsonb_array_elements_text({meta}->'quality_flags')), '{{}}'::text[]),
    b.char_count,
    {meta}->>'license',
    {meta}->>'title',
    CASE WHEN jsonb_typeof({meta}->'authors') = 'array'
         THEN ARRAY(SELECT jsonb_array_elements_text({meta}->'authors')) END,
    CASE WHEN jsonb_typeof({meta}->'composed_years') = 'array'
         THEN int4range(({meta}->'composed_years'->>0)::int,
                        ({meta}->'composed_years'->>1)::int, '[]') END,
    {meta}->>'composed_label',
    NULL::int4range,
    {meta}->>'genre',
    {meta}->>'category',
    CASE WHEN jsonb_typeof({meta}->'topics') = 'array'
         THEN ARRAY(SELECT jsonb_array_elements_text({meta}->'topics')) END,
    {meta}->>'legacy_category',
    coalesce({meta}->'field_basis', '{{}}'::jsonb),
    coalesce({meta}->'extra', '{{}}'::jsonb)"""


def publish(conn: psycopg.Connection, batch_id: int, pool: ProcessPoolExecutor) -> IngestReport:
    """在一个事务里完成发布。"""
    status, source_id = conn.execute(
        "SELECT status, source_id FROM source_batch WHERE batch_id = %s FOR UPDATE", (batch_id,)
    ).fetchone()
    if status != "validated":
        raise IngestError(f"批次 {batch_id} 状态为 {status}，不能发布")

    plain_bytes, stored_bytes = _write_blobs(conn, batch_id, pool)
    parse_run = _run_id(conn, batch_id, "parse")
    clean_run = _run_id(conn, batch_id, "clean")

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
              AND previous.record_hash IS DISTINCT FROM r.record_hash"""
    )
    new_documents = conn.execute(
        """INSERT INTO document (source_id, source_key, unit_type)
           SELECT %s, c.source_key, s.metadata->>'unit_type'
           FROM changed c
           JOIN staging_text s ON s.batch_id = %s AND s.source_key = c.source_key
                              AND s.step = 'decoded'
           ON CONFLICT (source_id, source_key) DO NOTHING""",
        (source_id, batch_id),
    ).rowcount
    conn.execute(
        """UPDATE source_record r SET doc_id = d.doc_id
           FROM document d
           WHERE r.batch_id = %s AND r.parse_status = 'ok'
             AND d.source_id = %s AND d.source_key = r.source_key""",
        (batch_id, source_id),
    )
    conn.execute(
        f"""CREATE TEMP TABLE publishing ON COMMIT DROP AS
            SELECT c.source_key, d.doc_id, st.metadata AS dec_meta, sc.metadata AS cln_meta,
                   sha256(convert_to(st.content, 'UTF8')) AS dec_hash,
                   sha256(convert_to(sc.content, 'UTF8')) AS cln_hash,
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
    conn.execute("UPDATE document_version v SET is_current = false FROM publishing p"
                 " WHERE v.version_id = p.current_id")
    decoded = conn.execute(
        f"""INSERT INTO document_version ({_VERSION_COLUMNS})
            SELECT p.doc_id, p.last_no + 1, p.current_id, p.dec_hash, %s, false,
                   {_version_values('p.dec_meta')}
            FROM publishing p JOIN text_blob b ON b.text_hash = p.dec_hash""",
        (parse_run,),
    ).rowcount
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
    counts = conn.execute(
        """SELECT count(*) FILTER (WHERE parse_status = 'ok'),
                  count(*) FILTER (WHERE parse_status = 'failed')
           FROM source_record WHERE batch_id = %s""",
        (batch_id,),
    ).fetchone()
    changed_count = conn.execute("SELECT count(*) FROM changed").fetchone()[0]
    conn.execute(f"DROP TABLE staging_text_b{batch_id}")
    conn.execute(
        "UPDATE source_batch SET status = 'published', published_at = now() WHERE batch_id = %s",
        (batch_id,),
    )
    conn.commit()
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
    )


def ingest(conn: psycopg.Connection, adapter: Adapter, paths: list[Path]) -> IngestReport:
    batch_id = register_batch(conn, adapter, paths)
    try:
        stage(conn, batch_id, adapter)
    except Exception:
        conn.rollback()
        conn.execute("UPDATE source_batch SET status = 'failed' WHERE batch_id = %s", (batch_id,))
        conn.commit()
        raise
    status = validate(conn, batch_id)
    if status != "validated":
        raise IngestError(f"批次 {batch_id} 验证未通过，状态 {status}")
    # psycopg 带后台线程，fork 有死锁风险
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=WORKERS, mp_context=context) as pool:
        report = publish(conn, batch_id, pool)
    return replace(report, dedup=dedup.dedup(conn))
