"""The same resumable engine targets staging records or existing document versions."""
from contextlib import ExitStack
import psycopg

from psycopg.types.json import Jsonb
from ..corpus import query
from ..corpus.management import ManagementError, _identity
from ..llm.services import LLMServices
from ..llm.errors import LLMError
from .models import CurationConfig, CURATION_VERSION, TAXONOMY_VERSION
from .retrieval import Retrieval
from .research import analyze
from .repository import fingerprint, save_result
from .apply import load_target, apply_item
from .policy import decide


def run(conn, *, actor, reason, config=None, batch_id=None, filters=None, doc_ids=None,
        limit=100, resume_run_id=None, resume_options=None, progress=None, services=None, retrieval=None):
    _identity(actor, reason)
    config = config or CurationConfig()
    if batch_id is not None:
        conn.execute('SELECT pg_advisory_lock(%s)', (batch_id,))
    try:
        return _run(conn, actor=actor, reason=reason, config=config, batch_id=batch_id,
                    filters=filters, doc_ids=doc_ids, limit=limit, resume_run_id=resume_run_id,
                    resume_options=resume_options, progress=progress, services=services, retrieval=retrieval)
    finally:
        if batch_id is not None:
            conn.rollback()
            conn.execute('SELECT pg_advisory_unlock(%s)', (batch_id,))
            conn.commit()


def _run(conn, *, actor, reason, config, batch_id, filters, doc_ids, limit,
         resume_run_id, resume_options, progress, services, retrieval):
    if resume_run_id is not None:
        row = conn.execute("SELECT config,batch_id FROM processing_run WHERE run_id=%s AND kind='curation'",
                            (resume_run_id,)).fetchone()
        if not row:
            raise ManagementError('not_found', '整理运行不存在')
        if row[1] != batch_id:
            raise ValueError('恢复时必须提供原来的 batch_id')
        overrides = resume_options.model_dump(exclude_none=True) if resume_options else {}
        config = CurationConfig.model_validate({**row[0]['curation'], **overrides})
        run_id = resume_run_id
        if overrides:
            conn.execute('UPDATE processing_run SET config=%s WHERE run_id=%s',
                         (Jsonb({**row[0], 'curation':config.model_dump(),
                                 'last_resume':{'actor':actor,'reason':reason,'overrides':overrides}}), run_id))
            conn.commit()
    else:
        filters = query.Filters.from_dict(filters or {})
        where, params = query.where_clause(filters)
        run_id = conn.execute("INSERT INTO processing_run(kind,rule_version,batch_id,config)"
            " VALUES('curation',%s,%s,%s) RETURNING run_id",
            (CURATION_VERSION, batch_id, Jsonb({'actor': actor, 'reason': reason, 'curation': config.model_dump(),
                'taxonomy_version': TAXONOMY_VERSION, 'filters': filters.__dict__, 'doc_ids': doc_ids, 'limit': limit}))).fetchone()[0]
        if batch_id is not None:
            batch = conn.execute('SELECT status FROM source_batch WHERE batch_id=%s', (batch_id,)).fetchone()
            if not batch or batch[0] not in ('staged', 'validated', 'needs_review'):
                raise ManagementError('conflict', '批次需要先完成暂存')
            from .ingestion import reuse_unchanged
            reuse_unchanged(conn, batch_id)
            conn.execute("INSERT INTO curation_item(run_id,record_id,base_version_id,input_hash,status)"
                " SELECT %s,r.record_id,r.curation_base_version_id,'','pending' FROM source_record r JOIN staging_text st"
                " ON st.batch_id=r.batch_id AND st.source_key=r.source_key AND st.step='cleaned'"
                " WHERE r.batch_id=%s AND (%s OR NOT coalesce((st.metadata->'processed_steps'->>'ai_curation')::boolean,false))",
                (run_id, batch_id, config.force))
        else:
            if not config.force:
                where += " AND NOT coalesce((v.processed_steps->>'ai_curation')::boolean,false)"
            if doc_ids is not None:
                where += ' AND d.doc_id=ANY(%(doc_ids)s)'
                params['doc_ids'] = doc_ids
            params.update(run=run_id, limit=limit)
            conn.execute("INSERT INTO curation_item(run_id,doc_id,base_version_id,input_hash,status)"
                " SELECT %(run)s,d.doc_id,v.version_id,'','pending' FROM document_version v"
                " JOIN document d USING(doc_id) JOIN source s USING(source_id)"
                f" WHERE {where} ORDER BY d.doc_id LIMIT %(limit)s", params)
        conn.commit()
    total = conn.execute('SELECT count(*) FROM curation_item WHERE run_id=%s', (run_id,)).fetchone()[0]
    # Session-level lock survives per-record commits; another resume cannot duplicate paid work.
    if not conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (f'curation-run:{run_id}',)).fetchone()[0]:
        raise ManagementError('conflict', '该整理运行正在执行')
    conn.commit()
    try:
        with ExitStack() as stack:
            after = done = 0
            while True:
                rows = conn.execute('SELECT to_jsonb(i) FROM curation_item i WHERE run_id=%s AND item_id>%s'
                                    ' ORDER BY item_id LIMIT 50', (run_id, after)).fetchall()
                if not rows:
                    break
                conn.commit()
                for (item,) in rows:
                    after = item['item_id']
                    if progress:
                        progress(done, total, f'整理工作项 {after}')
                    done += 1
                    if item['status'] in ('applied', 'rolled_back', 'conflict'):
                        continue
                    try:
                        if item['status'] != 'proposed':
                            target = load_target(conn, item)
                            input_hash = fingerprint(target['metadata'], target['text_hash'])
                            if item['input_hash'] and input_hash != item['input_hash']:
                                raise ManagementError('conflict', '输入已变化，请新建整理运行')
                            conn.execute('UPDATE curation_item SET input_hash=%s WHERE item_id=%s', (input_hash, after))
                            conn.commit()
                            if services is None:
                                services = stack.enter_context(LLMServices.from_config())
                            if retrieval is None:
                                retrieval = Retrieval(conn, services.client.config, config.search_profile)
                                stack.callback(retrieval.close)
                            metadata = target['metadata']
                            # Only the common metadata contract enters a model prompt.
                            from ..corpus.metadata import VERSION_FIELDS
                            clean_meta = {k: metadata.get(k) for k in VERSION_FIELDS if k not in ('extra', 'processed_steps', 'field_basis', 'field_claims')}
                            clean_meta.update(target.get('context', {}))
                            if metadata.get('work_title'):
                                clean_meta['work_title'] = metadata['work_title']
                                clean_meta['work_relation'] = metadata.get('work_relation')
                            from .journal import Journal, JournalServices, JournalRetrieval
                            journal = Journal(conn, after, (lambda: progress(done-1,total,f'工作项 {after}：模型与检索')) if progress else None)
                            result = analyze(clean_meta, target['text'], config,
                                             JournalServices(services, journal), JournalRetrieval(retrieval, journal))
                            decisions = decide(result.claims, metadata, result.evidence, config)
                            save_result(conn, after, result, decisions)
                            conn.commit()
                        if config.apply:
                            apply_item(conn, after, config)
                            conn.commit()
                    except (ValueError, LLMError, psycopg.DataError) as exc:
                        conn.rollback()
                        code = getattr(exc, 'code', 'invalid_result')
                        status = 'conflict' if code == 'conflict' else 'failed'
                        # Retain proposed results on application errors for explicit retry.
                        conn.execute("UPDATE curation_item SET status=CASE WHEN status='proposed' AND %s<>'conflict'"
                                     " THEN status ELSE %s END,error_code=%s WHERE item_id=%s", (status, status, code, after))
                        conn.commit()
            conn.execute('UPDATE processing_run SET finished_at=now() WHERE run_id=%s', (run_id,))
            conn.commit()
    finally:
        conn.rollback()
        conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (f'curation-run:{run_id}',))
        conn.commit()
    counts = dict(conn.execute('SELECT status,count(*) FROM curation_item WHERE run_id=%s GROUP BY status', (run_id,)))
    if progress:
        progress(total, total, '整理运行结束')
    return {'run_id': run_id, 'items': total, 'counts': counts, 'batch_id': batch_id,
            'complete': not any(counts.get(s, 0) for s in ('pending','failed','conflict','proposed'))}
