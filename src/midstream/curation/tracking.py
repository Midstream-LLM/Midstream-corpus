"""One completion marker protocol for ingestion, rules, AI and manual edits."""
from psycopg.types.json import Jsonb
from ..corpus.metadata import step_flags


def record_operation(conn, run_id, steps, *, flags=None, version_id=None, record_id=None, details=None):
    marks = step_flags(steps)
    result = step_flags(flags)
    for step in steps:
        result[step] = marks[step]
        conn.execute('INSERT INTO processing_event(run_id,version_id,record_id,step,completed,details)'
                     ' VALUES(%s,%s,%s,%s,%s,%s)',
                     (run_id, version_id, record_id, step, marks[step], Jsonb(details or {})))
    return result


def record_batch_clean(conn, batch_id, run_id):
    """Bulk form of the same event contract; no per-document database round trips."""
    conn.execute("INSERT INTO processing_event(run_id,version_id,record_id,step,completed)"
                 " SELECT %s,v.version_id,r.record_id,'basic_clean',true"
                 " FROM document_version v JOIN source_record r USING(doc_id)"
                 " WHERE v.run_id=%s AND r.batch_id=%s", (run_id, run_id, batch_id))


def record_checked(conn, run_id, step, version_ids):
    """规则检查过、无需修改的版本：不建新版本，只登记检查结果。"""
    conn.execute("INSERT INTO processing_event(run_id,version_id,step,completed,details)"
                 " SELECT %s,unnest(%s::bigint[]),%s,true,'{\"changed\":false}'",
                 (run_id, version_ids, step))
