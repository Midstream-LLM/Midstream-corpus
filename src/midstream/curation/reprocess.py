"""按规则给现有文档生成新版本。"""

import multiprocessing
import random
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from ..corpus import clean
from .. import config
from .. import store
from ..corpus.versions import _write_versions
from ..store import textcodec
from .tracking import record_checked


@dataclass(frozen=True)
class Change:
    text: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    version: str
    sources: list[str] | None
    apply: Callable[[dict], Change | None]
    variant: str | None = None
    config: dict = field(default_factory=dict)
    step: str = "rule_reprocess"
    # 当前版本若出自这条规则的运行，就把它的上一版正文作为 current["parent_text"] 一并给出，
    # 用于改进旧规则：从旧规则之前的正文重做，而不是在旧规则的结果上叠加。
    redo_after: str | None = None


def _write_blobs(conn: psycopg.Connection, texts: dict, pool: ProcessPoolExecutor) -> None:
    known = {
        bytes(row[0]) for row in conn.execute(
            "SELECT text_hash FROM text_blob WHERE text_hash = ANY(%s)", (list(texts),)
        )
    }
    fresh = {digest: text for digest, text in texts.items() if digest not in known}
    compressed = pool.map(
        textcodec.compress, fresh.values(), chunksize=max(1, len(fresh) // config.WORKERS)
    )
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO text_blob (text_hash, codec, char_count, byte_count, content)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [(digest, textcodec.CODEC, len(text), len(text.encode("utf-8")), data)
             for (digest, text), data in zip(fresh.items(), compressed)],
        )


def reprocess(conn: psycopg.Connection, rule: Rule, dry_run=None, samples=20) -> dict:
    if dry_run is None:
        dry_run = rule.variant is not None
    changed = 0
    preview = []
    randomizer = random.Random(0)
    with ExitStack() as stack:
        if not dry_run:
            stack.enter_context(conn.transaction())
            run_id = conn.execute(
                "INSERT INTO processing_run (kind, rule_version)"
                " VALUES ('reprocess', %s) RETURNING run_id", (rule.version,)
            ).fetchone()[0]
            pool = stack.enter_context(ProcessPoolExecutor(
                max_workers=config.WORKERS, mp_context=multiprocessing.get_context("spawn")
            ))
        source = stack.enter_context(store.reader(conn))
        reader = stack.enter_context(source.cursor(name="current_versions"))
        condition = " AND s.name = ANY(%s)" if rule.sources is not None else ""
        params = [rule.sources] if rule.sources is not None else []
        if rule.variant:
            condition += ' AND NOT EXISTS (SELECT 1 FROM document_version derived WHERE derived.doc_id=v.doc_id' \
                         ' AND derived.variant=%s AND derived.parent_version_id=v.version_id)'
            params.append(rule.variant)
        parent = ("(SELECT pb.content FROM document_version p JOIN text_blob pb ON pb.text_hash = p.text_hash"
                  " WHERE p.version_id = v.parent_version_id AND v.run_id IN"
                  " (SELECT run_id FROM processing_run WHERE rule_version = %s))") if rule.redo_after else "NULL::bytea"
        reader.execute(
            "SELECT to_jsonb(v), s.name, b.content,"
            " (SELECT r.raw_fields FROM source_record r WHERE r.doc_id = v.doc_id ORDER BY r.record_id DESC LIMIT 1), "
            " (SELECT w.canonical_title FROM document_work dw JOIN work w USING (work_id)"
            "  WHERE dw.doc_id = v.doc_id AND dw.relation IN ('whole','part','excerpt') ORDER BY w.work_id LIMIT 1), "
            + parent +
            " FROM document_version v"
            " JOIN document d USING (doc_id) JOIN source s USING (source_id)"
            " JOIN text_blob b USING (text_hash) WHERE v.is_current" + condition +
            " ORDER BY v.doc_id",
            ([rule.redo_after] if rule.redo_after else []) + params,
        )
        while rows := reader.fetchmany(config.CHUNK_DOCS):
            changes = []
            texts = {}
            unchanged = []
            for current, source_name, content, raw_fields, work_title, parent_content in rows:
                before = textcodec.decompress(bytes(content))
                # raw_fields 是来源原始字段（不含正文），规则可据此补回入库时没进正文的信息。
                current.update(source=source_name, text=before, raw_fields=raw_fields or {}, work_title=work_title,
                               parent_text=textcodec.decompress(bytes(parent_content)) if parent_content else None)
                change = rule.apply(current)
                if change is None:
                    unchanged.append(current["version_id"])
                    continue
                after = before if change.text is None else change.text
                metadata = dict(change.metadata)
                digest = None
                if after != before:
                    metadata["quality_status"], metadata["quality_flags"] = clean.assess(after)
                    digest = textcodec.text_hash(after)
                metadata = {key: value for key, value in metadata.items() if value != current[key]}
                if digest is None and not metadata and rule.variant is None:
                    unchanged.append(current["version_id"])
                    continue
                changed += 1
                slot = randomizer.randrange(changed)
                if len(preview) < samples or slot < samples:
                    position = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b),
                                    min(len(before), len(after)))
                    start = max(0, position - 60) if digest is not None else 0
                    sample = {
                        "doc_id": current["doc_id"], "title": current["title"],
                        "before": before[start:start + 200], "after": after[start:start + 200],
                        "metadata": {key: {"before": current[key], "after": value}
                                     for key, value in metadata.items()},
                    }
                    if len(preview) < samples:
                        preview.append(sample)
                    else:
                        preview[slot] = sample
                if not dry_run:
                    changes.append((current["doc_id"], current["version_id"], digest, metadata))
                    if digest is not None:
                        texts[digest] = after
            if not dry_run and changes:
                _write_blobs(conn, texts, pool)
                _write_versions(conn, run_id, changes, variant=rule.variant, step=rule.step)
            if not dry_run and unchanged:
                # 无需修改的文档不建新版本；检查结果只记在操作记录里。
                record_checked(conn, run_id, rule.step, unchanged)
        if not dry_run:
            conn.execute(
                "UPDATE processing_run SET config = %s, finished_at = now() WHERE run_id = %s",
                (Jsonb({"sources": rule.sources, "changed": changed, **rule.config,
                        **({'variant': rule.variant} if rule.variant else {})}), run_id),
            )
    if not dry_run:
        conn.commit()
    return {"changed": changed, "samples": preview}
