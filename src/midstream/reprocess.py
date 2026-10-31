"""按规则给现有文档生成新版本。"""

import multiprocessing
import random
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from . import clean, config, textcodec


@dataclass(frozen=True)
class Change:
    text: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    version: str
    sources: list[str] | None
    apply: Callable[[dict], Change | None]


_METADATA = """quality_status quality_flags license title authors composed_years
    composed_label edition_years genre category topics legacy_category field_basis extra""".split()


def _write_versions(conn: psycopg.Connection, run_id: int, changes: list[tuple]) -> None:
    """每项为 (doc_id, parent_version_id, 新 text_hash 或 None, 字段覆盖)。"""
    docs, parents, hashes, metadata = zip(*changes)
    conn.execute(
        "UPDATE document_version SET is_current = false WHERE version_id = ANY(%s)",
        (list(parents),),
    )
    conn.execute(
        f"""INSERT INTO document_version
            (doc_id, version_no, parent_version_id, text_hash, run_id, is_current,
             char_count, {', '.join(_METADATA)})
            SELECT c.doc_id, latest.max_no + 1, p.version_id,
                   coalesce(c.text_hash, p.text_hash), %s, true,
                   b.char_count, {', '.join('m.' + key for key in _METADATA)}
            FROM unnest(%s::bigint[], %s::bigint[], %s::bytea[], %s::jsonb[])
                 AS c(doc_id, parent_version_id, text_hash, metadata)
            JOIN document_version p ON p.doc_id = c.doc_id
                                   AND p.version_id = c.parent_version_id
            CROSS JOIN LATERAL (
                SELECT max(version_no) AS max_no FROM document_version
                WHERE doc_id = c.doc_id
            ) latest
            CROSS JOIN LATERAL jsonb_populate_record(p, c.metadata) m
            JOIN text_blob b ON b.text_hash = coalesce(c.text_hash, p.text_hash)""",
        (run_id, list(docs), list(parents), list(hashes), [Jsonb(m) for m in metadata]),
    )


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


def reprocess(conn: psycopg.Connection, rule: Rule, dry_run=False, samples=20) -> dict:
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
        source = stack.enter_context(psycopg.connect(conn.info.dsn))
        reader = stack.enter_context(source.cursor(name="current_versions"))
        condition = " AND s.name = ANY(%s)" if rule.sources is not None else ""
        reader.execute(
            "SELECT to_jsonb(v), s.name, b.content FROM document_version v"
            " JOIN document d USING (doc_id) JOIN source s USING (source_id)"
            " JOIN text_blob b USING (text_hash) WHERE v.is_current" + condition +
            " ORDER BY v.doc_id",
            (rule.sources,) if rule.sources is not None else (),
        )
        while rows := reader.fetchmany(config.CHUNK_DOCS):
            changes = []
            texts = {}
            for current, source_name, content in rows:
                before = textcodec.decompress(bytes(content))
                current.update(source=source_name, text=before)
                change = rule.apply(current)
                if change is None:
                    continue
                after = before if change.text is None else change.text
                metadata = dict(change.metadata)
                digest = None
                if after != before:
                    metadata["quality_status"], metadata["quality_flags"] = clean.assess(after)
                    digest = textcodec.text_hash(after)
                metadata = {key: value for key, value in metadata.items() if value != current[key]}
                if digest is None and not metadata:
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
                _write_versions(conn, run_id, changes)
        if not dry_run:
            conn.execute(
                "UPDATE processing_run SET config = %s, finished_at = now() WHERE run_id = %s",
                (Jsonb({"sources": rule.sources, "changed": changed}), run_id),
            )
    if not dry_run:
        conn.commit()
    return {"changed": changed, "samples": preview}
