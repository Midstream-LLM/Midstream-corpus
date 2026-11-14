"""导入识别、登记与批次管理。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import jobs
from ..ingest import batches, importing, discovery, detection as import_detection
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(min_length=1, max_length=200)
    plan: dict | None = None
    paths: list[str] = Field(min_length=1, max_length=20)


class ImportDetect(BaseModel):
    model_config = ConfigDict(extra='forbid')
    paths: list[str] = Field(min_length=1, max_length=20)
    use_llm: bool = False
    profile: str | None = None


class ImportRegister(ImportPreview, Operation):
    objects: list[dict] = Field(min_length=1, max_length=20)
    parser_version: str


@router.post('/api/v1/import/detect', dependencies=[Depends(browser_write)])
def import_detect(body: ImportDetect):
    return import_detection.detect(body.paths, body.use_llm, body.profile)


@router.get('/api/v1/import/adapters')
def import_adapters():
    return importing.adapters()


@router.post('/api/v1/import/preview', dependencies=[Depends(browser_write)])
def import_preview(body: ImportPreview):
    return importing.preview(body.source, body.paths, body.plan)


@router.post('/api/v1/import/register', dependencies=[Depends(browser_write)])
def import_register(body: ImportRegister, conn=Depends(write_connection)):
    return importing.register(conn, body.source, body.paths, body.objects,
                              body.parser_version, body.actor, body.reason, body.plan)


@router.get('/api/v1/batches')
def batch_list(include_published: bool = False, before: Annotated[int | None, Query(gt=0)] = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 30, conn=Depends(connection)):
    rows = batches.list_batches(conn, limit=limit+1, include_published=include_published, before=before)
    active = active_batch_jobs(conn, [row['batch_id'] for row in rows[:limit]])
    for row in rows[:limit]:
        row['active_job'] = active.get(str(row['batch_id']))
    return {'items': rows[:limit], 'next_cursor': rows[limit-1]['batch_id'] if len(rows)>limit else None}


@router.get('/api/v1/batches/{batch_id}')
def batch_detail(batch_id: int, conn=Depends(connection)):
    result = batches.inspect(conn, batch_id)
    result['active_job'] = active_batch_jobs(conn, [batch_id]).get(str(batch_id))
    return result


@router.post('/api/v1/batches/{batch_id}/resume', dependencies=[Depends(browser_write)])
def resume(batch_id: int, body: Operation, conn=Depends(write_connection)):
    return jobs.run_inline(conn, 'batch_resume', {'batch_id': batch_id}, body.actor, body.reason)['result']


@router.post('/api/v1/batches/{batch_id}/stage', dependencies=[Depends(browser_write)])
def batch_stage(batch_id: int, body: Operation, conn=Depends(write_connection)):
    return jobs.submit(conn, 'batch_stage', {'batch_id': batch_id}, body.actor, body.reason)


@router.post('/api/v1/batches/{batch_id}/publish', dependencies=[Depends(browser_write)])
def batch_publish(batch_id: int, body: Operation, conn=Depends(write_connection)):
    return jobs.submit(conn, 'batch_publish', {'batch_id': batch_id}, body.actor, body.reason)


@router.get('/api/v1/batches/{batch_id}/preview')
def batch_preview(batch_id: int, conn=Depends(connection)):
    return batches.preview(conn, batch_id)


@router.get('/api/v1/batches/{batch_id}/records')
def batch_records(batch_id: int, status: Literal['new','updated','unchanged','failed'] | None = None,
                  after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1,le=100)] = 30,
                  conn=Depends(connection)):
    return batches.records(conn, batch_id, status=status, after=after, limit=limit)


@router.get('/api/v1/batches/{batch_id}/records/{record_id}/diff')
def batch_diff(batch_id: int, record_id: int, conn=Depends(connection)):
    return batches.record_diff(conn, batch_id, record_id)


@router.post('/api/v1/batches/{batch_id}/abandon', dependencies=[Depends(browser_write)])
def abandon(batch_id: int, body: Operation, conn=Depends(write_connection)):
    return batches.abandon(conn, batch_id, actor=body.actor, reason=body.reason)


class BrowseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str | None = None
    offset: int = Field(default=0, ge=0)


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    paths: list[str] = Field(min_length=1, max_length=200)
    recursive: bool = True


@router.post('/api/v1/import/browse', dependencies=[Depends(browser_write)])
def import_browse(body: BrowseRequest):
    return discovery.browse(body.path, body.offset)


@router.post('/api/v1/import/scans', dependencies=[Depends(browser_write)])
def import_scan(body: ScanRequest):
    return discovery.start(body.paths, body.recursive)


@router.get('/api/v1/import/scans/{scan_id}')
def import_scan_status(scan_id: str, offset: int = Query(default=0, ge=0)):
    return discovery.get(scan_id, offset)


@router.post('/api/v1/import/scans/{scan_id}/cancel', dependencies=[Depends(browser_write)])
def import_scan_cancel(scan_id: str):
    return discovery.get(scan_id, cancel=True)


class ImportApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    method: Literal['manual', 'rule', 'llm']
    edited: bool = False
    model: str | None = Field(default=None, max_length=200)
    requested_by: str = Field(min_length=1, max_length=200)


class ImportStart(ImportRegister):
    request_token: UUID
    approval: ImportApproval | None = None


@router.post('/api/v1/import/start', dependencies=[Depends(browser_write)])
def import_start(body: ImportStart, conn=Depends(write_connection)):
    return importing.start_import(conn, body)


def active_batch_jobs(conn, batch_ids):
    if not batch_ids:
        return {}
    rows = conn.execute(
        """SELECT DISTINCT ON (params->>'batch_id') params->>'batch_id',
                  json_build_object('job_id', job_id, 'status', status, 'kind', kind)
           FROM job WHERE kind IN ('batch_stage', 'batch_publish', 'batch_resume')
           AND status IN ('queued', 'running') AND params->>'batch_id' = ANY(%s)
           ORDER BY params->>'batch_id', job_id DESC""",
        ([str(i) for i in batch_ids],)).fetchall()
    return dict(rows)
