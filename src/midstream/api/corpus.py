"""文档、元数据、人工审核与统计。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from ..corpus import management, query
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


class DocumentReview(Operation):
    expected_version_id: int = Field(gt=0)
    metadata: dict


class WorkReview(Operation):
    work_id: int = Field(gt=0)
    relation: Literal['whole', 'part', 'excerpt', 'contains']
    decision: Literal['confirmed', 'rejected']
    expected_status: Literal['candidate', 'confirmed', 'rejected']


@router.get('/api/v1/stats')
def stats(conn=Depends(connection)):
    return query.overview(conn)


@router.get('/api/v1/years/options')
def year_options(conn=Depends(connection)):
    return query.year_options(conn)


@router.get('/api/v1/stats/years')
def year_stats(granularity: Literal['dynasty','century','year'] = 'century',
               year_from: Annotated[int | None, Query(ge=-2147483647,le=2147483646)] = None,
               year_to: Annotated[int | None, Query(ge=-2147483647,le=2147483646)] = None,
               conn=Depends(connection)):
    if year_from is not None and year_to is not None and year_from > year_to:
        raise HTTPException(422, '起始年份不能晚于结束年份')
    if year_from == 0 or year_to == 0:
        raise HTTPException(422, '没有公元0年；公元前用负数表示')
    if granularity == 'dynasty' and (year_from is not None or year_to is not None):
        raise HTTPException(422, '时代标签按原记录分组，年份窗口请使用世纪或逐年统计')
    try:
        return query.year_distribution(conn, granularity, year_from, year_to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/api/v1/documents/{doc_id}/metadata')
def metadata(doc_id: int, version: Annotated[int | None, Query(ge=1)] = None, conn=Depends(connection)):
    result = query.metadata(conn, doc_id, version)
    if result is None:
        raise HTTPException(404, '文档或版本不存在')
    return result


@router.post('/api/v1/documents/{doc_id}/review', dependencies=[Depends(browser_write)])
def review(doc_id: int, body: DocumentReview, conn=Depends(write_connection)):
    return management.review_document(conn, doc_id, body.expected_version_id, body.metadata,
                                      actor=body.actor, reason=body.reason)


@router.get('/api/v1/documents/{doc_id}/works')
def works(doc_id: int, conn=Depends(connection)):
    return management.work_links(conn, doc_id)


@router.post('/api/v1/documents/{doc_id}/works/review', dependencies=[Depends(browser_write)])
def review_work(doc_id: int, body: WorkReview, conn=Depends(write_connection)):
    return management.review_work(conn, doc_id, body.work_id, body.relation, body.decision,
                                  actor=body.actor, reason=body.reason, expected_status=body.expected_status)


@router.get('/api/v1/events')
def events(doc_id: int | None = None, batch_id: int | None = None, conn=Depends(connection)):
    return management.events(conn, doc_id=doc_id, batch_id=batch_id, limit=100)


@router.get("/api/v1/sources")
def sources(conn=Depends(connection)):
    return [{"name": name} for name, in conn.execute("SELECT name FROM source ORDER BY name")]


@router.get("/api/v1/documents")
def documents(title: str | None = None, author: str | None = None, source: str | None = None,
              category: str | None = None, genre: str | None = None,
              quality: Literal['ok','flagged','quarantined'] | None = None,
              year_from: Annotated[int | None, Query(ge=-2147483647,le=2147483646)] = None,
              year_to: Annotated[int | None, Query(ge=-2147483647,le=2147483646)] = None,
              era: str | None = None, include_unknown_years: bool = False,
              only_unknown_years: bool = False,
              limit: Annotated[int, Query(ge=1, le=100)] = 30,
              after: Annotated[int, Query(ge=0)] = 0, conn=Depends(connection)):
    if year_from is not None and year_to is not None and year_from > year_to:
        raise HTTPException(422, "起始年份不能晚于结束年份")
    if year_from == 0 or year_to == 0:
        raise HTTPException(422, '没有公元0年；公元前用负数表示')
    if only_unknown_years and (year_from is not None or year_to is not None):
        raise HTTPException(422, '仅看年份未知不能同时指定年份区间')
    rows = query.search(conn, query.Filters(title_prefix=title, author=author,
        sources=[source] if source else [], category=category, genre=genre,
        quality=[quality] if quality else ['ok','flagged','quarantined'],
        year_from=year_from, year_to=year_to, composed_label=era,
        include_unknown_years=include_unknown_years,
        only_unknown_years=only_unknown_years), limit=limit + 1, after_doc=after)
    return {"items": rows[:limit], "next_cursor": rows[limit-1]['doc_id'] if len(rows)>limit else None}


@router.get("/api/v1/documents/{doc_id}/versions")
def versions(doc_id: int, conn=Depends(connection)):
    rows = conn.execute("SELECT version_id,version_no,is_current,title,char_count,quality_status,created_at,variant,"
        " variant IS NOT NULL AND parent_version_id IS DISTINCT FROM"
        " (SELECT version_id FROM document_version WHERE doc_id=%s AND is_current) AS variant_stale"
        " FROM document_version WHERE doc_id=%s ORDER BY version_no DESC", (doc_id,doc_id)).fetchall()
    if not rows:
        raise HTTPException(404, "文档不存在")
    keys = ('version_id','version_no','is_current','title','char_count','quality_status','created_at','variant','variant_stale')
    return [dict(zip(keys, row)) for row in rows]


@router.get("/api/v1/documents/{doc_id}")
def document(doc_id: int, version: Annotated[int | None, Query(ge=1)] = None,
             offset: Annotated[int, Query(ge=0)] = 0,
             length: Annotated[int, Query(ge=1, le=20000)] = 10000, conn=Depends(connection)):
    result = query.get_text(conn, doc_id, version)
    if result is None:
        raise HTTPException(404, "文档或版本不存在")
    text = result.pop('text')
    return {**result, "text": text[offset:offset+length], "offset": offset,
            "next_offset": offset+length if offset+length<len(text) else None}
