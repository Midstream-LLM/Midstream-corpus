"""Curation storage: metadata-only selection, immutable evidence, optional review."""
from psycopg.types.json import Jsonb
from ..corpus.metadata import VERSION_FIELDS
from ..corpus.management import ManagementError, _identity
from .retrieval import digest


def fingerprint(meta, text_hash):
    return digest({'metadata': {k: meta.get(k) for k in VERSION_FIELDS}, 'text_hash': text_hash})


def save_result(conn, item_id, result, decisions):
    ids = {}
    referenced = {key for claim in result.claims for key in claim.evidence_keys}
    for e in result.evidence:
        if e.key not in referenced:
            continue
        body = e.model_dump(exclude={'key'})
        key = digest(body)
        ids[e.key] = conn.execute('INSERT INTO curation_evidence(digest,provider,reference,content)'
            ' VALUES(%s,%s,%s,%s) ON CONFLICT(digest) DO UPDATE SET digest=excluded.digest RETURNING evidence_id',
            (key, e.provider, e.reference, Jsonb(body))).fetchone()[0]
    # A proposed result is written once and can be applied after a crash without calling the model again.
    if conn.execute('SELECT 1 FROM curation_claim WHERE item_id=%s', (item_id,)).fetchone():
        raise ManagementError('conflict', '工作项已有候选结果')
    for c, decision in zip(result.claims, decisions):
        claim_id = conn.execute('INSERT INTO curation_claim(item_id,kind,field,value,source_kind,confidence,rationale,application_status)'
            " VALUES(%s,%s,%s,%s,'llm',%s,%s,%s) RETURNING claim_id",
            (item_id, c.kind, c.field, Jsonb(c.value), c.confidence, c.rationale,
             'proposed' if decision == 'applied' else decision)).fetchone()[0]
        for key in set(c.evidence_keys):
            conn.execute('INSERT INTO curation_claim_evidence VALUES(%s,%s)', (claim_id, ids[key]))
    conn.execute("UPDATE curation_item SET status='proposed',completed_steps=%s,summary=%s,usage=%s,error_code=NULL WHERE item_id=%s",
        (Jsonb(result.completed_steps), result.summary, Jsonb({'model_calls': result.usage, 'tool_calls': result.tool_calls}), item_id))


def item(conn, item_id):
    row = conn.execute('SELECT to_jsonb(i) FROM curation_item i WHERE item_id=%s', (item_id,)).fetchone()
    if not row:
        raise ManagementError('not_found', '整理工作项不存在')
    claims = conn.execute('SELECT to_jsonb(c) FROM curation_claim c WHERE item_id=%s ORDER BY claim_id', (item_id,)).fetchall()
    return {**row[0], 'claims': [c[0] for c in claims]}


def evidence(conn, claim_id):
    return [r[0] for r in conn.execute('SELECT to_jsonb(e) FROM curation_claim_evidence ce'
                ' JOIN curation_evidence e USING(evidence_id) WHERE ce.claim_id=%s ORDER BY evidence_id', (claim_id,))]


def review(conn, claim_id, outcome, actor, reason):
    _identity(actor, reason)
    if outcome not in ('confirmed', 'rejected'):
        raise ValueError('无效审核结果')
    row = conn.execute('SELECT human_reviewed,review_outcome FROM curation_claim WHERE claim_id=%s FOR UPDATE', (claim_id,)).fetchone()
    if not row:
        raise ManagementError('not_found', '候选结论不存在')
    conn.execute('UPDATE curation_claim SET human_reviewed=true,review_outcome=%s,reviewed_by=%s,reviewed_at=now()'
                 ' WHERE claim_id=%s', (outcome, actor, claim_id))
    review_id = conn.execute('INSERT INTO curation_review_event(claim_id,actor,reason,outcome) VALUES(%s,%s,%s,%s) RETURNING review_id',
                             (claim_id, actor, reason, outcome)).fetchone()[0]
    conn.commit()
    return {'review_id': review_id, 'claim_id': claim_id, 'human_reviewed': True, 'review_outcome': outcome}
