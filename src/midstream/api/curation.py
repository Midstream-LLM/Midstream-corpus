"""Curation metadata, evidence and optional field-level review; no text reads in lists."""
from typing import Literal
from fastapi import APIRouter, Depends, Query
from pydantic import Field
from psycopg.types.json import Jsonb
from .. import jobs
from ..corpus.metadata import RecordMetadata
from ..curation.models import CurationConfig, ResearchResponse, TAXONOMY, TAXONOMY_VERSION
from ..curation.operations import CurateParams
from ..curation import repository
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


@router.get('/api/v1/curation/schema')
def schema():
    return {'metadata': RecordMetadata.model_json_schema(), 'config': CurationConfig.model_json_schema(),
            'research': ResearchResponse.model_json_schema(), 'taxonomy': TAXONOMY, 'taxonomy_version': TAXONOMY_VERSION}


class Start(Operation):
    params: CurateParams = Field(default_factory=CurateParams)


@router.post('/api/v1/curation/runs', dependencies=[Depends(browser_write)])
def start(body: Start, conn=Depends(write_connection)):
    return jobs.submit(conn, 'curate', body.params.model_dump(), body.actor, body.reason)


@router.get('/api/v1/curation/runs')
def runs(before: int | None = Query(default=None, gt=0), limit: int = Query(default=30, ge=1, le=100), conn=Depends(connection)):
    rows = conn.execute("SELECT r.run_id,r.batch_id,r.started_at,r.finished_at,r.config,"
        " (SELECT jsonb_object_agg(status,n) FROM (SELECT status,count(*) n FROM curation_item"
        " WHERE run_id=r.run_id GROUP BY status) counts) FROM processing_run r"
        " WHERE kind='curation' AND (%s::bigint IS NULL OR run_id<%s) ORDER BY run_id DESC LIMIT %s",
        (before,before,limit+1)).fetchall()
    keys = ('run_id','batch_id','started_at','finished_at','config','counts')
    return {'items': [dict(zip(keys,r)) for r in rows[:limit]],
            'next_cursor': rows[limit-1][0] if len(rows)>limit else None}


@router.get('/api/v1/curation/items')
def items(run_id: int | None = Query(default=None, gt=0), doc_id: int | None = Query(default=None, gt=0),
          after: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100), conn=Depends(connection)):
    rows = conn.execute('SELECT item_id,run_id,doc_id,record_id,base_version_id,result_version_id,status,completed_steps,summary,error_code'
        ' FROM curation_item WHERE (%s::bigint IS NULL OR run_id=%s) AND (%s::bigint IS NULL OR doc_id=%s)'
        ' AND item_id>%s ORDER BY item_id LIMIT %s', (run_id,run_id,doc_id,doc_id,after,limit+1)).fetchall()
    keys = ('item_id','run_id','doc_id','record_id','base_version_id','result_version_id','status','completed_steps','summary','error_code')
    return {'items': [dict(zip(keys,r)) for r in rows[:limit]], 'next_cursor': rows[limit-1][0] if len(rows)>limit else None}


@router.get('/api/v1/curation/items/{item_id}')
def detail(item_id: int, conn=Depends(connection)):
    return repository.item(conn,item_id)


@router.get('/api/v1/curation/claims/{claim_id}/evidence')
def evidence(claim_id: int, conn=Depends(connection)):
    return {'items': repository.evidence(conn,claim_id)}


class Review(Operation):
    outcome: Literal['confirmed','rejected']


@router.post('/api/v1/curation/claims/{claim_id}/review', dependencies=[Depends(browser_write)])
def review(claim_id: int, body: Review, conn=Depends(write_connection)):
    return repository.review(conn,claim_id,body.outcome,body.actor,body.reason)


@router.post('/api/v1/curation/items/{item_id}/apply', dependencies=[Depends(browser_write)])
def apply(item_id: int, body: Operation, conn=Depends(write_connection)):
    from ..curation.apply import apply_item
    from ..corpus.management import _identity
    _identity(body.actor,body.reason)
    item = repository.item(conn,item_id)
    raw = conn.execute('SELECT config FROM processing_run WHERE run_id=%s',(item['run_id'],)).fetchone()[0]
    version_id = apply_item(conn,item_id,CurationConfig.model_validate(raw['curation']))
    conn.execute('INSERT INTO management_event(action,actor,reason,doc_id,after_state) VALUES(%s,%s,%s,%s,%s)',
                 ('curation_apply',body.actor,body.reason,item['doc_id'],Jsonb({'item_id':item_id,'version_id':version_id})))
    conn.commit()
    return {'item_id':item_id,'version_id':version_id,'status':'applied'}


@router.post('/api/v1/curation/items/{item_id}/rollback', dependencies=[Depends(browser_write)])
def rollback(item_id: int, body: Operation, conn=Depends(write_connection)):
    from ..curation.apply import rollback_item
    return rollback_item(conn,item_id,body.actor,body.reason)


@router.get('/api/v1/documents/{doc_id}/segments')
def segments(doc_id: int, version_id: int | None = Query(default=None, gt=0), conn=Depends(connection)):
    return {'items': [row[0] for row in conn.execute('SELECT to_jsonb(s) FROM document_segment s'
        ' JOIN document_version v USING(version_id) WHERE v.doc_id=%s AND'
        ' ((%s::bigint IS NULL AND v.is_current) OR v.version_id=%s) ORDER BY s.start_char', (doc_id,version_id,version_id))]}
