"""未发布批次的检查、恢复与放弃。"""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from . import dedup, ingest
from .adapters import ADAPTERS
from .config import WORKERS
from .management import ManagementError, _identity


def _batch(conn: psycopg.Connection, batch_id: int, *, lock: bool = False) -> tuple:
    row = conn.execute(
        "SELECT b.status, s.name, b.parser_version, b.published_at"
        " FROM source_batch b JOIN source s USING (source_id)"
        " WHERE b.batch_id = %s" + (" FOR UPDATE OF b" if lock else ""),
        (batch_id,),
    ).fetchone()
    if row is None:
        raise ManagementError("not_found", f"批次 {batch_id} 不存在")
    return row


def _partition(conn: psycopg.Connection, batch_id: int) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_inherits i"
        " WHERE i.inhparent = 'staging_text'::regclass"
        " AND i.inhrelid = to_regclass(%s))",
        (f"staging_text_b{int(batch_id)}",),
    ).fetchone()[0]


def inspect(conn: psycopg.Connection, batch_id: int) -> dict:
    status, source, parser_version, published_at = _batch(conn, batch_id)
    ok, failed = conn.execute(
        "SELECT count(*) FILTER (WHERE parse_status = 'ok'),"
        " count(*) FILTER (WHERE parse_status = 'failed')"
        " FROM source_record WHERE batch_id = %s", (batch_id,),
    ).fetchone()
    staged = dict(conn.execute(
        "SELECT step, count(*) FROM staging_text WHERE batch_id = %s GROUP BY step",
        (batch_id,),
    ).fetchall())
    objects = conn.execute(
        "SELECT path, encode(sha256, 'hex'), size_bytes"
        " FROM raw_object WHERE batch_id = %s ORDER BY raw_id", (batch_id,),
    ).fetchall()
    failures = conn.execute(
        "SELECT source_key, locator, parse_error FROM source_record"
        " WHERE batch_id = %s AND parse_status = 'failed'"
        " ORDER BY record_id LIMIT 20", (batch_id,),
    ).fetchall()
    return {
        "batch_id": batch_id, "status": status, "source": source,
        "parser_version": parser_version, "published_at": published_at,
        "records_ok": ok, "records_failed": failed,
        "staged": {"decoded": staged.get("decoded", 0), "cleaned": staged.get("cleaned", 0)},
        "validation_ready": ok > 0 and staged.get("decoded", 0) == ok
                            and staged.get("cleaned", 0) == ok,
        "has_partition": _partition(conn, batch_id),
        "failure_samples": [
            {"source_key": key, "locator": locator, "parse_error": error}
            for key, locator, error in failures
        ],
        "raw_objects": [
            {"path": path, "sha256": digest, "size_bytes": size}
            for path, digest, size in objects
        ],
    }


def list_batches(conn: psycopg.Connection, *, limit: int = 50,
                 include_published: bool = False, before: int | None = None) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ManagementError("invalid_input", "limit 必须在 1 到 1000 之间")
    rows = conn.execute(
        "SELECT b.batch_id, s.name, b.status, b.received_at, b.published_at,"
        " (SELECT count(*) FROM raw_object o WHERE o.batch_id = b.batch_id)"
        " FROM source_batch b JOIN source s USING (source_id)"
        " WHERE (%s OR b.status <> 'published')"
        " AND (%s::bigint IS NULL OR b.batch_id < %s)"
        " ORDER BY b.batch_id DESC LIMIT %s", (include_published, before, before, limit),
    ).fetchall()
    keys = ("batch_id", "source", "status", "received_at", "published_at", "raw_objects")
    return [dict(zip(keys, row)) for row in rows]


def _verify_inputs(conn: psycopg.Connection, batch_id: int, source: str,
                   parser_version: str) -> tuple:
    plan = conn.execute("SELECT import_plan FROM source_batch WHERE batch_id = %s", (batch_id,)).fetchone()[0]
    if plan is not None:
        from .adapters.mapped import MappedAdapter
        adapter = MappedAdapter(source, plan)
    else:
        adapter = ADAPTERS.get(source)
    if adapter is None or adapter.parser_version != parser_version:
        raise ManagementError("not_recoverable", "来源适配器缺失或解析器版本与登记时不同")
    rows = conn.execute(
        "SELECT path, sha256, size_bytes FROM raw_object"
        " WHERE batch_id = %s ORDER BY raw_id", (batch_id,),
    ).fetchall()
    if not rows:
        raise ManagementError("not_recoverable", "批次没有登记原文件")
    paths = []
    for name, digest, size in rows:
        path = Path(name)
        if not path.is_file() or path.stat().st_size != size:
            raise ManagementError("input_changed", f"原文件不存在或大小变化：{name}")
        if ingest._sha256_file(path) != bytes(digest):
            raise ManagementError("input_changed", f"原文件哈希变化：{name}")
        paths.append(path)
    return adapter, paths


def _event(conn: psycopg.Connection, action: str, batch_id: int, actor: str,
           reason: str, before: str, after: str) -> int:
    return conn.execute(
        "INSERT INTO management_event"
        " (action, actor, reason, batch_id, before_state, after_state)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING event_id",
        (action, actor, reason, batch_id,
         Jsonb({"status": before}), Jsonb({"status": after})),
    ).fetchone()[0]


def resume(conn: psycopg.Connection, batch_id: int, *, actor: str, reason: str,
           progress: Callable[[int, int, str], None] | None = None) -> dict:
    """按状态重入流程；发布成功后的查重失败不会触发二次发布。"""
    _identity(actor, reason)
    conn.execute("SELECT pg_advisory_lock(%s)", (batch_id,))
    try:
        status, source, version, _ = _batch(conn, batch_id)
        if status == "published":
            return {"status": "already_published", "batch_id": batch_id,
                    "next_action": "dedup"}
        if status == "abandoned":
            raise ManagementError("not_recoverable", "已放弃的批次不能恢复")
        if progress:
            progress(0, 6, "核对原文件")
        adapter, _ = _verify_inputs(conn, batch_id, source, version)
        before = status
        with ProcessPoolExecutor(
            max_workers=WORKERS, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            if status in ("received", "failed"):
                if progress:
                    progress(1, 6, "解析与暂存")
                # stage 是整笔事务；上次失败不应留下半批记录，仍防御性清理。
                if conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM document_version v"
                    " JOIN processing_run r USING (run_id) WHERE r.batch_id = %s)",
                    (batch_id,),
                ).fetchone()[0]:
                    raise ManagementError("not_recoverable", "批次已有发布版本，不能重暂存")
                if _partition(conn, batch_id):
                    conn.execute(f"DROP TABLE staging_text_b{int(batch_id)}")
                conn.execute("DELETE FROM source_record WHERE batch_id = %s", (batch_id,))
                conn.execute("DELETE FROM processing_run WHERE batch_id = %s", (batch_id,))
                conn.commit()
                try:
                    ingest.stage(conn, batch_id, adapter, pool)
                except Exception:
                    conn.rollback()
                    conn.execute(
                        "UPDATE source_batch SET status = 'failed' WHERE batch_id = %s", (batch_id,),
                    )
                    _event(conn, "batch_resume", batch_id, actor, reason, before, "failed")
                    conn.commit()
                    raise
                status = "staged"
            if status in ("staged", "needs_review"):
                if progress:
                    progress(2, 6, "验证暂存数据")
                if not _partition(conn, batch_id):
                    raise ManagementError("not_recoverable", "暂存分区缺失，不能验证")
                status = ingest.validate(conn, batch_id)
                if status == "needs_review":
                    event_id = _event(conn, "batch_resume", batch_id, actor, reason,
                                      before, status)
                    conn.commit()
                    return {"status": status, "batch_id": batch_id, "event_id": event_id}
            if status != "validated" or not _partition(conn, batch_id):
                raise ManagementError("not_recoverable", "批次缺少完整的已验证暂存层")
            if progress:
                progress(3, 6, "发布版本")
            report = ingest.publish(conn, batch_id, pool)
        event_id = _event(conn, "batch_resume", batch_id, actor, reason,
                          before, "published")
        conn.commit()
        result = {"status": "published", "batch_id": batch_id, "event_id": event_id,
                  "new_documents": report.new_documents,
                  "new_versions": report.new_versions, "unchanged": report.unchanged}
        if progress:
            result["dedup"] = dedup.dedup(
                conn, progress=lambda current, total, message: progress(4, 6, message))
            result["dedup_spans"] = dedup.dedup_spans(
                conn, progress=lambda current, total, message: progress(5, 6, message))
        else:
            result["dedup"] = dedup.dedup(conn)
            result["dedup_spans"] = dedup.dedup_spans(conn)
        return result
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (batch_id,))
        conn.commit()


def abandon(conn: psycopg.Connection, batch_id: int, *, actor: str, reason: str) -> dict:
    """只清理未发布批次的暂存分区，保留登记和失败记录。"""
    _identity(actor, reason)
    conn.execute("SELECT pg_advisory_lock(%s)", (batch_id,))
    try:
        status, _, _, _ = _batch(conn, batch_id, lock=True)
        if status == "published":
            raise ManagementError("not_recoverable", "已发布批次不能放弃")
        if status == "abandoned":
            return {"status": "already_abandoned", "batch_id": batch_id}
        if conn.execute(
            "SELECT EXISTS (SELECT 1 FROM document_version v"
            " JOIN processing_run r USING (run_id) WHERE r.batch_id = %s)",
            (batch_id,),
        ).fetchone()[0]:
            raise ManagementError("not_recoverable", "批次已有发布版本，不能放弃")
        if _partition(conn, batch_id):
            conn.execute(f"DROP TABLE staging_text_b{int(batch_id)}")
        conn.execute(
            "UPDATE source_batch SET status = 'abandoned' WHERE batch_id = %s", (batch_id,),
        )
        event_id = _event(conn, "batch_abandon", batch_id, actor, reason,
                          status, "abandoned")
        conn.commit()
        return {"status": "abandoned", "batch_id": batch_id, "event_id": event_id}
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (batch_id,))
        conn.commit()
