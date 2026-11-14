"""重复簇、对照与否决。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from ..dedup import duplicates
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


class DedupOverride(Operation):
    left_doc_id: int = Field(gt=0)
    right_doc_id: int | None = Field(default=None,gt=0)
    cluster_id: int | None = Field(default=None,gt=0)


@router.get('/api/v1/duplicates/clusters')
def duplicate_clusters(before: Annotated[int | None,Query(gt=0)]=None,
                       kind: Literal['exact','near'] | None=None, source: str | None=None,
                       min_size: Annotated[int,Query(ge=2)]=2, max_size: Annotated[int | None,Query(ge=2)]=None,
                       limit: Annotated[int,Query(ge=1,le=100)]=30,conn=Depends(connection)):
    return duplicates.clusters(conn,before=before,kind=kind,source=source,min_size=min_size,max_size=max_size,limit=limit)


@router.get('/api/v1/duplicates/clusters/{cluster_id}')
def duplicate_cluster(cluster_id:int,after:Annotated[int,Query(ge=0)]=0,
                      limit:Annotated[int,Query(ge=1,le=100)]=30,conn=Depends(connection)):
    return duplicates.cluster(conn,cluster_id,after=after,limit=limit)


@router.get('/api/v1/duplicates/compare')
def duplicate_compare(left:Annotated[int,Query(gt=0)],right:Annotated[int,Query(gt=0)],
                      length:Annotated[int,Query(ge=1,le=20000)]=10000,conn=Depends(connection)):
    return duplicates.compare(conn,left,right,length)


@router.get('/api/v1/duplicates/overrides')
def duplicate_overrides(doc_id:int|None=None,conn=Depends(connection)):
    return duplicates.overrides(conn,doc_id)


@router.post('/api/v1/duplicates/overrides',dependencies=[Depends(browser_write)])
def duplicate_override(body:DedupOverride,conn=Depends(write_connection)):
    return duplicates.override(conn,body.left_doc_id,body.right_doc_id,body.cluster_id,body.actor,body.reason)


@router.get('/api/v1/documents/{doc_id}/spans')
def duplicate_spans(doc_id:int,version:Annotated[int|None,Query(ge=1)]=None,conn=Depends(connection)):
    return duplicates.document_spans(conn,doc_id,version)
