"""持久任务与操作清单。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from .. import jobs, operations
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


class JobSubmit(Operation):
    kind: str = Field(min_length=1, max_length=200)
    params: dict = Field(default_factory=dict)


@router.get('/api/v1/jobs')
def job_list(before: Annotated[int | None, Query(gt=0)] = None,
             limit: Annotated[int, Query(ge=1, le=100)] = 30, conn=Depends(connection)):
    rows = jobs.list_jobs(conn, limit=limit+1, before=before)
    return {'items': rows[:limit], 'next_cursor': rows[limit-1]['job_id'] if len(rows)>limit else None}


@router.get('/api/v1/operations')
def operation_list():
    return operations.describe()


@router.get('/api/v1/jobs/{job_id}')
def job_detail(job_id: int, conn=Depends(connection)):
    return jobs.show(conn, job_id)


@router.post('/api/v1/jobs', dependencies=[Depends(browser_write)])
def job_submit(body: JobSubmit, conn=Depends(write_connection)):
    return jobs.submit(conn, body.kind, body.params, body.actor, body.reason)


@router.post('/api/v1/jobs/{job_id}/cancel', dependencies=[Depends(browser_write)])
def job_cancel(job_id: int, body: Operation, conn=Depends(write_connection)):
    return jobs.cancel(conn, job_id, body.actor, body.reason)


@router.post('/api/v1/jobs/{job_id}/retry', dependencies=[Depends(browser_write)])
def job_retry(job_id: int, body: Operation, conn=Depends(write_connection)):
    return jobs.retry(conn, job_id, body.actor, body.reason)
