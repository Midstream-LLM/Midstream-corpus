"""Import integration: finish configured curation before publication."""
from psycopg.types.json import Jsonb
from ..corpus.management import ManagementError
from .models import CurationConfig


def configure_batch(conn, batch_id, config):
    if not config.apply or set(config.steps) != {'metadata_enrichment', 'tagging', 'text_cleanup', 'segmentation'}:
        raise ValueError('入库清洗须应用结果并启用完整步骤；仅建议模式可用于库内预览')
    changed = conn.execute("UPDATE source_batch SET curation_config=%s WHERE batch_id=%s"
                           " AND status IN ('received','staged','validated','needs_review')",
                           (Jsonb(config.model_dump()), batch_id)).rowcount
    if not changed:
        raise ManagementError('conflict', '该批次不能配置入库清洗')


def curate_batch(conn, batch_id, *, actor, reason, progress=None, allow_uncurated=False):
    data = conn.execute('SELECT curation_config FROM source_batch WHERE batch_id=%s', (batch_id,)).fetchone()[0]
    if data is None:
        return None
    from .pipeline import run
    if not conn.execute("SELECT 1 FROM staging_text WHERE batch_id=%s AND step='cleaned'"
        " AND NOT coalesce((metadata->'processed_steps'->>'ai_curation')::boolean,false) LIMIT 1", (batch_id,)).fetchone():
        ensure_ready(conn,batch_id)
        return {'complete':True,'batch_id':batch_id,'items':0}
    previous = conn.execute("SELECT run_id FROM processing_run r WHERE batch_id=%s AND kind='curation'"
        " AND config->'curation'=%s AND EXISTS(SELECT 1 FROM curation_item i WHERE i.run_id=r.run_id"
        " AND i.status IN ('pending','failed','proposed')) ORDER BY run_id DESC LIMIT 1", (batch_id,Jsonb(data))).fetchone()
    # Per-step marks survive retries. Completed records are skipped by the same selector.
    result = run(conn, actor=actor, reason=reason, config=CurationConfig.model_validate(data),
                 batch_id=batch_id, progress=progress, resume_run_id=previous[0] if previous else None)
    ensure_ready(conn, batch_id, allow_uncurated=allow_uncurated)
    return result


def uncurated(conn, batch_id):
    """配置了入库清洗、但还没完成的暂存记录数；未配置时为 None。"""
    configured = conn.execute('SELECT curation_config IS NOT NULL FROM source_batch WHERE batch_id=%s', (batch_id,)).fetchone()
    if not configured or not configured[0]:
        return None
    return conn.execute("SELECT count(*) FROM staging_text WHERE batch_id=%s AND step='cleaned'"
        " AND NOT coalesce((metadata->'processed_steps'->>'ai_curation')::boolean,false)", (batch_id,)).fetchone()[0]


def ensure_ready(conn, batch_id, *, allow_uncurated=False):
    """返回未完成清洗的记录数。操作人确认放行时，这些记录以 ai_curation=false 发布，留给库内补清洗。"""
    remaining = uncurated(conn, batch_id) or 0
    if remaining and not allow_uncurated:
        raise ManagementError('conflict', f'还有 {remaining} 条记录未完成入库清洗；可恢复整理运行，'
                                          '或确认后发布、留到库内补清洗')

    rows = conn.execute('SELECT r.curation_base_version_id,v.version_id FROM source_record r'
        ' JOIN source_batch b USING(batch_id) JOIN document d ON d.source_id=b.source_id AND d.source_key=r.source_key'
        ' JOIN document_version v ON v.doc_id=d.doc_id AND v.is_current'
        ' WHERE r.batch_id=%s AND r.curation_base_version_id IS NOT NULL ORDER BY v.version_id FOR UPDATE OF v', (batch_id,))
    if any(expected != current for expected,current in rows):
        raise ManagementError('conflict', '复用的已入库记录有后续修订，请重新建立暂存批次')
    return remaining


def reuse_unchanged(conn, batch_id):
    """Unchanged source records start from their curated current version, preserving manual edits."""
    from ..corpus.metadata import RecordMetadata, VERSION_FIELDS
    from ..store import textcodec
    after = 0
    while True:
        rows = conn.execute("SELECT r.record_id,r.source_key,sc.metadata,to_jsonb(v),v.text_hash,v.char_count,"
            " CASE WHEN sc.text_hash IS DISTINCT FROM v.text_hash THEN tb.content END"
            " FROM source_record r JOIN source_batch b USING(batch_id)"
            " JOIN document d ON d.source_id=b.source_id AND d.source_key=r.source_key"
            " JOIN document_version v ON v.doc_id=d.doc_id AND v.is_current JOIN text_blob tb USING(text_hash)"
            " JOIN staging_text sc ON sc.batch_id=r.batch_id AND sc.source_key=r.source_key AND sc.step='cleaned'"
            " JOIN LATERAL (SELECT old.record_hash FROM source_record old JOIN source_batch ob USING(batch_id)"
            " WHERE ob.source_id=b.source_id AND ob.status='published' AND old.source_key=r.source_key"
            " ORDER BY ob.batch_id DESC LIMIT 1) previous ON previous.record_hash=r.record_hash"
            " WHERE r.batch_id=%s AND r.record_id>%s AND r.curation_base_version_id IS NULL"
            " AND NOT EXISTS(SELECT 1 FROM curation_item ci WHERE ci.record_id=r.record_id AND ci.status='applied')"
            " ORDER BY r.record_id LIMIT 100", (batch_id,after)).fetchall()
        if not rows:
            break
        for record_id,key,staged,current,digest,length,blob in rows:
            meta=RecordMetadata.model_validate({**staged,**{k:current[k] for k in VERSION_FIELDS}}).model_dump()
            conn.execute("UPDATE staging_text SET metadata=%s,text_hash=%s,char_count=%s,content=coalesce(%s,content)"
                         " WHERE batch_id=%s AND source_key=%s AND step='cleaned'",
                         (Jsonb(meta),digest,length,textcodec.decompress(bytes(blob)) if blob is not None else None,batch_id,key))
            conn.execute('UPDATE source_record SET curation_base_version_id=%s WHERE record_id=%s',(current['version_id'],record_id))
            after=record_id
        conn.commit()
