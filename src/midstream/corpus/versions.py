"""给文档追加新版本的公共函数；入库之外的改动都通过这里生成新版本。"""

import psycopg
from psycopg.types.json import Jsonb


_METADATA = """quality_status quality_flags license title authors composed_years
    composed_label edition_years genre category topics legacy_category field_basis extra""".split()


def _write_versions(conn: psycopg.Connection, run_id: int, changes: list[tuple], *, variant=None) -> None:
    """每项为 (doc_id, parent_version_id, 新 text_hash 或 None, 字段覆盖)。"""
    docs, parents, hashes, metadata = zip(*changes)
    if variant is None:
        conn.execute(
            "UPDATE document_version SET is_current = false WHERE version_id = ANY(%s)",
            (list(parents),),
        )
    conn.execute(
        f"""INSERT INTO document_version
            (doc_id, version_no, parent_version_id, text_hash, run_id, is_current, variant,
             char_count, {', '.join(_METADATA)})
            SELECT c.doc_id, latest.max_no + 1, p.version_id,
                   coalesce(c.text_hash, p.text_hash), %s, %s, %s,
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
        (run_id, variant is None, variant, list(docs), list(parents), list(hashes), [Jsonb(m) for m in metadata]),
    )
