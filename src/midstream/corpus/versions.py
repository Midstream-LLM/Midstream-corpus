"""给文档追加新版本的公共函数；入库之外的改动都通过这里生成新版本。"""

import psycopg
from psycopg.types.json import Jsonb


from .metadata import VERSION_FIELDS

_METADATA = VERSION_FIELDS

def _write_versions(conn: psycopg.Connection, run_id: int, changes: list[tuple], *, variant=None, step=None) -> None:
    """每项为 (doc_id, parent_version_id, 新 text_hash 或 None, 字段覆盖)。"""
    if not changes:
        return
    from .metadata import step_flags, consistent_patch, database_patch
    from ..curation.tracking import record_operation
    run = conn.execute('SELECT kind,rule_version FROM processing_run WHERE run_id=%s',(run_id,)).fetchone()
    if step is None and run[0] == 'manual':
        step = 'manual_edit'
    changes = [(doc, parent, digest, dict(patch)) for doc,parent,digest,patch in changes]
    parents_by_id = {row[0]: row for row in conn.execute(
        'SELECT version_id,doc_id,is_current,to_jsonb(document_version) FROM document_version'
        ' WHERE version_id=ANY(%s) ORDER BY version_id FOR UPDATE', ([c[1] for c in changes],))}
    from .management import ManagementError
    for doc,parent,digest,patch in changes:
        previous = parents_by_id.get(parent)
        if not previous or previous[1] != doc or not previous[2]:
            raise ManagementError('conflict', '当前版本已改变，请重新处理')
        if step:
            patch['processed_steps'] = {**step_flags(previous[3].get("processed_steps")), **patch.get('processed_steps',{}), step: True}
        normalized = database_patch(consistent_patch(previous[3], patch))
        patch.clear()
        patch.update(normalized)
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

    if step:
        rows = conn.execute('SELECT version_id,processed_steps FROM document_version WHERE run_id=%s AND parent_version_id=ANY(%s)',
                            (run_id,list(parents))).fetchall()
        for version_id,flags in rows:
            record_operation(conn,run_id,{step:True},version_id=version_id,flags=flags)

    conn.execute('INSERT INTO document_segment(version_id,claim_id,start_char,end_char,title,unit_type)'
        ' SELECT n.version_id,s.claim_id,s.start_char,s.end_char,s.title,s.unit_type'
        ' FROM document_version n JOIN document_version p ON p.version_id=n.parent_version_id'
        ' JOIN document_segment s ON s.version_id=p.version_id'
        ' WHERE n.run_id=%s AND n.parent_version_id=ANY(%s) AND n.text_hash=p.text_hash ON CONFLICT DO NOTHING',
        (run_id,list(parents)))
