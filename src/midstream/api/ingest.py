"""导入识别、登记与批次管理。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import jobs
from ..ingest import batches, importing, detection as import_detection
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
    return {'items': rows[:limit], 'next_cursor': rows[limit-1]['batch_id'] if len(rows)>limit else None}


@router.get('/api/v1/batches/{batch_id}')
def batch_detail(batch_id: int, conn=Depends(connection)):
    return batches.inspect(conn, batch_id)


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
