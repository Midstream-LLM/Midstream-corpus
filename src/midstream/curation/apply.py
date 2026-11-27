"""Atomic application for both staged records and published document versions."""
from psycopg.types.json import Jsonb
from ..corpus.metadata import VERSION_FIELDS, RecordMetadata, database_patch, step_flags, consistent_patch
from ..corpus.versions import _write_versions
from ..corpus import clean
from ..corpus.management import ManagementError
from ..store import textcodec
from .models import Claim
from .policy import patched_text
from .repository import fingerprint
from .tracking import record_operation


def load_target(conn, item, *, lock=False, require_current=True):
    if item['record_id'] is not None:
        row = conn.execute("SELECT sc.metadata,coalesce(sc.content,st.content),encode(sc.text_hash,'hex'),"
            "r.source_key,r.batch_id,b.status,s.name FROM source_record r JOIN source_batch b USING(batch_id) JOIN source s USING(source_id)"
            " JOIN staging_text sc ON sc.batch_id=r.batch_id AND sc.source_key=r.source_key AND sc.step='cleaned'"
            " JOIN staging_text st ON st.batch_id=r.batch_id AND st.source_key=r.source_key AND st.step='decoded'"
            " WHERE r.record_id=%s" + (' FOR UPDATE OF sc,b' if lock else ''), (item['record_id'],)).fetchone()
        if not row or row[5] not in ('staged', 'validated', 'needs_review'):
            raise ManagementError('conflict', '暂存记录已发布、放弃或不存在')
        return {'metadata': row[0], 'text': row[1], 'text_hash': row[2],
                'source_key': row[3], 'batch_id': row[4], 'context':{'source':row[6], 'source_key':row[3]}}
    row = conn.execute('SELECT to_jsonb(v),b.content,encode(v.text_hash,\'hex\')'
        ' FROM document_version v JOIN text_blob b USING(text_hash) WHERE v.version_id=%s' +
        (' FOR UPDATE OF v' if lock else ''), (item['base_version_id'],)).fetchone()
    if not row or (require_current and not row[0]['is_current']):
        raise ManagementError('conflict', '当前版本已改变；已有结果保留，请重新选择该文档处理')
    identity = conn.execute('SELECT s.name,d.source_key FROM document d JOIN source s USING(source_id) WHERE doc_id=%s', (item['doc_id'],)).fetchone()
    works = conn.execute("SELECT w.work_id,w.canonical_title,dw.relation FROM document_work dw JOIN work w USING(work_id)"
                          " WHERE dw.doc_id=%s AND dw.status='confirmed' AND dw.relation IN ('whole','part','excerpt') LIMIT 2", (item['doc_id'],)).fetchall()
    context = {'source':identity[0], 'source_key':identity[1]}
    if len(works) == 1:
        context.update(work_id=works[0][0],work_title=works[0][1],work_relation=works[0][2])
    return {'metadata': row[0], 'text': textcodec.decompress(bytes(row[1])), 'text_hash': row[2], 'context':context}


def store_blob(conn, text):
    digest = textcodec.text_hash(text)
    conn.execute('INSERT INTO text_blob(text_hash,codec,char_count,byte_count,content) VALUES(%s,%s,%s,%s,%s)'
                 ' ON CONFLICT(text_hash) DO NOTHING',
                 (digest, textcodec.CODEC, len(text), len(text.encode()), textcodec.compress(text)))
    return digest


def segments_after_patches(segments, patches):
    output = []
    for cid, segment in segments:
        a, b = segment['start_char'], segment['end_char']
        if any(p.value['start_char'] < boundary < p.value['end_char'] for p in patches for boundary in (a,b)):
            continue
        def mapped(position):
            return position + sum(len(p.value['after']) - len(p.value['before']) for p in patches
                                  if p.value['end_char'] <= position)
        output.append({'claim_id': cid, **segment, 'start_char': mapped(a), 'end_char': mapped(b)})
    return output


def apply_item(conn, item_id, config):
    batch = conn.execute('SELECT r.batch_id FROM curation_item i JOIN source_record r USING(record_id) WHERE i.item_id=%s', (item_id,)).fetchone()
    if batch:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (batch[0],))
    item = conn.execute('SELECT to_jsonb(i) FROM curation_item i WHERE item_id=%s FOR UPDATE', (item_id,)).fetchone()[0]
    # 已应用后又有人工确认的扣下候选时，从原输入重算全部已接受候选，写在本次结果版本之后。
    reapply = item['status'] == 'applied'
    if reapply:
        pending = conn.execute("SELECT 1 FROM curation_claim WHERE item_id=%s AND application_status='withheld'"
                               " AND review_outcome='confirmed' LIMIT 1", (item_id,)).fetchone()
        if not pending or item['record_id'] is not None or item['result_version_id'] is None:
            return item.get('result_version_id')
        load_target(conn, {**item, 'base_version_id': item['result_version_id']}, lock=True)
        target = load_target(conn, item, require_current=False)
    elif item['status'] != 'proposed':
        raise ManagementError('conflict', '工作项尚无完整可应用结果')
    else:
        target = load_target(conn, item, lock=True)
    current = target['metadata']
    if fingerprint(current, target['text_hash']) != item['input_hash']:
        raise ManagementError('conflict', '输入已改变；候选结果未覆盖新内容')
    rows = conn.execute('SELECT claim_id,kind,field,value,confidence,rationale,application_status,review_outcome'
                         ' FROM curation_claim WHERE item_id=%s ORDER BY claim_id FOR UPDATE', (item_id,)).fetchall()
    patch, edits, segments, accepted, field_claims = {}, [], [], [], {}
    for cid, kind, field, value, confidence, rationale, status, review in rows:
        # 人工确认解除策略扣留；否决的候选一律不用。
        if status not in ('proposed', 'applied', 'withheld') or review == 'rejected' or (
                status == 'withheld' and review != 'confirmed'):
            continue
        claim = Claim(kind=kind, field=field, value=value, confidence=confidence, rationale=rationale)
        accepted.append(cid)
        field_claims.setdefault(field, []).append(cid)
        if kind == 'metadata':
            patch[field] = value
        elif kind == 'text_patch':
            edits.append(claim)
        else:
            segments.append((cid, value))
    text = patched_text(target['text'], edits)
    output_segments = segments_after_patches(segments, edits)
    accepted_segments = {s['claim_id'] for s in output_segments}
    for cid, _ in segments:
        if cid not in accepted_segments:
            accepted.remove(cid)
            conn.execute("UPDATE curation_claim SET application_status='conflict' WHERE claim_id=%s", (cid,))
    if 'segments' in field_claims:
        field_claims['segments'] = sorted(accepted_segments)
    basis = dict(current.get('field_basis') or {})
    comparable = database_patch(patch)
    old_values = database_patch({field:current.get(field) for field in patch})
    basis.update({field: 'llm' for field in patch if comparable[field] != old_values[field]})
    patch['field_basis'] = basis
    if edits:
        patch['quality_status'], patch['quality_flags'] = clean.assess(text)
        basis.update(quality_status='rule', quality_flags='rule')
        field_claims.pop('quality_flags', None)
    flags = step_flags(current.get('processed_steps'))
    flags.update(item['completed_steps'])
    flags['ai_curation'] = all(flags[s] for s in ('metadata_enrichment', 'tagging', 'text_cleanup', 'segmentation'))
    patch['processed_steps'] = flags
    patch['field_claims'] = {**current.get('field_claims', {}), **field_claims}
    if 'contributors' in field_claims:
        patch['field_claims']['authors'] = field_claims['contributors']
    elif 'authors' in field_claims:
        patch['field_claims']['contributors'] = field_claims['authors']
    patch = consistent_patch(current, patch)
    # Persist the pre-application staging input as a deduplicated blob as well.
    before_hash = store_blob(conn, target['text']) if item['record_id'] is not None else bytes.fromhex(target['text_hash'])
    snapshot = {'text_hash': before_hash.hex(), **(
        {'metadata': {k: current.get(k) for k in VERSION_FIELDS}} if item['record_id'] is not None
        else {'version_id':item['base_version_id']})}
    version_id = None
    if item['record_id'] is not None:
        new_meta = RecordMetadata.model_validate({**current, **patch}).model_dump()
        conn.execute("UPDATE staging_text SET metadata=%s,content=%s,text_hash=%s,char_count=%s"
                     " WHERE batch_id=%s AND source_key=%s AND step='cleaned'",
                     (Jsonb(new_meta), text, textcodec.text_hash(text), len(text), target['batch_id'], target['source_key']))
    else:
        digest = store_blob(conn, text) if edits else None
        db_patch = database_patch(patch)
        if not reapply and digest is None and not output_segments and all(value == current.get(key) for key,value in db_patch.items()):
            version_id = item['base_version_id']
        else:
            parent = item['result_version_id'] if reapply else item['base_version_id']
            _write_versions(conn, item['run_id'], [(item['doc_id'], parent, digest, db_patch)])
            version_id = conn.execute('SELECT version_id FROM document_version WHERE doc_id=%s AND is_current',
                                      (item['doc_id'],)).fetchone()[0]
        if output_segments or edits:
            conn.execute('DELETE FROM document_segment WHERE version_id=%s',(version_id,))
        save_segments(conn, version_id, output_segments)
    conn.execute("UPDATE curation_claim SET application_status='applied' WHERE claim_id=ANY(%s)", (accepted,))
    output = {'text_hash':textcodec.text_hash(text).hex(), **(
        {'metadata':{**snapshot['metadata'],**patch}} if item['record_id'] is not None else {'version_id':version_id})}
    conn.execute("UPDATE curation_item SET status='applied',result_version_id=%s,input_snapshot=%s,output_snapshot=%s,output_segments=%s WHERE item_id=%s",
                 (version_id, Jsonb(snapshot), Jsonb(output), Jsonb(output_segments), item_id))
    record_operation(conn, item['run_id'], {**item['completed_steps'], 'ai_curation': flags['ai_curation']},
                     flags=current.get('processed_steps'), version_id=version_id, record_id=item['record_id'],
                     details={'item_id': item_id, 'accepted_claims': accepted})
    return version_id


def save_segments(conn, version_id, segments):
    for segment in segments:
        conn.execute('INSERT INTO document_segment(version_id,claim_id,start_char,end_char,title,unit_type)'
                     ' VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (version_id, segment['claim_id'], segment['start_char'], segment['end_char'], segment['title'], segment['unit_type']))


def bind_published(conn, batch_id):
    conn.execute('UPDATE curation_item i SET doc_id=r.doc_id FROM source_record r WHERE r.record_id=i.record_id AND r.batch_id=%s', (batch_id,))
    rows = conn.execute("UPDATE curation_item i SET doc_id=r.doc_id,result_version_id=v.version_id"
        " FROM source_record r,document_version v WHERE r.record_id=i.record_id AND r.batch_id=%s"
        " AND v.doc_id=r.doc_id AND v.is_current AND i.status='applied' AND i.result_version_id IS NULL"
        " AND i.item_id=(SELECT max(latest.item_id) FROM curation_item latest WHERE latest.record_id=r.record_id AND latest.status='applied')"
        " RETURNING v.version_id,i.output_segments", (batch_id,)).fetchall()
    for version_id, segments in rows:
        save_segments(conn, version_id, segments)


def rollback_item(conn, item_id, actor, reason):
    from ..corpus.management import _identity
    _identity(actor, reason)
    item = conn.execute('SELECT to_jsonb(i) FROM curation_item i WHERE item_id=%s FOR UPDATE', (item_id,)).fetchone()
    if not item:
        raise ManagementError('not_found', '工作项不存在')
    item = item[0]
    if item['status'] != 'applied' or item['result_version_id'] is None:
        raise ManagementError('conflict', '仅支持撤销已发布并应用的整理结果')
    current = conn.execute('SELECT version_id FROM document_version WHERE doc_id=%s AND is_current FOR UPDATE', (item['doc_id'],)).fetchone()
    if not current or current[0] != item['result_version_id']:
        raise ManagementError('conflict', '文档已有后续修订，请对当前版本修正具体字段')
    snapshot = item['input_snapshot']
    if not snapshot:
        raise ManagementError('conflict', '缺少可恢复的输入快照')
    run_id = conn.execute("INSERT INTO processing_run(kind,rule_version,config,finished_at)"
        " VALUES('rollback','curation-rollback-v1',%s,now()) RETURNING run_id",
        (Jsonb({'actor': actor, 'reason': reason, 'item_id': item_id}),)).fetchone()[0]
    original = snapshot.get('metadata')
    if original is None:
        version = conn.execute('SELECT to_jsonb(v) FROM document_version v WHERE version_id=%s', (snapshot['version_id'],)).fetchone()[0]
        original = {k:version[k] for k in VERSION_FIELDS}
    patch = database_patch(original)
    _write_versions(conn,run_id,[(item['doc_id'],current[0],bytes.fromhex(snapshot['text_hash']),patch)],step='curation_rollback')
    version_id = conn.execute('SELECT version_id FROM document_version WHERE doc_id=%s AND is_current',(item['doc_id'],)).fetchone()[0]
    conn.execute("UPDATE curation_item SET status='rolled_back' WHERE item_id=%s", (item_id,))
    conn.execute("UPDATE curation_claim SET application_status='reverted' WHERE item_id=%s AND application_status='applied'", (item_id,))
    conn.execute('INSERT INTO management_event(action,actor,reason,doc_id,from_version_id,to_version_id,before_state,after_state)'
                 ' VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                 ('curation_rollback',actor,reason,item['doc_id'],current[0],version_id,
                  Jsonb({'item_id':item_id}),Jsonb({'restored_input':True})))
    conn.commit()
    return {'item_id':item_id,'version_id':version_id,'status':'rolled_back'}
