"""本机/SSH 隧道使用的 HTTP API，Web 与未来 SwiftUI 共用。"""
import os
from urllib.parse import urlsplit
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import batches, config, management, query, importing, import_detection


def write_connection():
    with psycopg.connect(config.dsn()) as conn:
        yield conn


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)


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


class DocumentReview(Operation):
    expected_version_id: int = Field(gt=0)
    metadata: dict


class WorkReview(Operation):
    work_id: int = Field(gt=0)
    relation: Literal['whole', 'part', 'excerpt', 'contains']
    decision: Literal['confirmed', 'rejected']
    expected_status: Literal['candidate', 'confirmed', 'rejected']


def browser_write(request: Request):
    if request.headers.get('x-midstream-action') != '1':
        raise HTTPException(403, '写操作需要 X-Midstream-Action 请求头')
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != request.url.scheme or parsed.netloc != request.headers.get('host'):
            raise HTTPException(403, '不接受跨站写请求')


def connection():
    with psycopg.connect(config.dsn()) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        yield conn


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Midstream Corpus", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get(
        'MIDSTREAM_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver').split(','))

    @app.exception_handler(management.ManagementError)
    async def management_error(request, exc):
        status = {'not_found': 404, 'conflict': 409, 'no_change': 409,
                  'not_recoverable': 409, 'input_changed': 409}.get(exc.code, 422)
        return JSONResponse(status_code=status, content={'detail': str(exc), 'code': exc.code})

    @app.exception_handler(psycopg.DataError)
    async def invalid_data(request, exc):
        return JSONResponse(status_code=422, content={'detail': '元数据格式无效，请检查年份区间和字段类型'})

    from .llm.errors import LLMError

    @app.exception_handler(LLMError)
    async def llm_error(request, exc):
        return JSONResponse(status_code=502, content={'detail': str(exc), 'code': exc.code})

    @app.post('/api/v1/import/detect', dependencies=[Depends(browser_write)])
    def import_detect(body: ImportDetect):
        return import_detection.detect(body.paths, body.use_llm, body.profile)

    @app.get('/api/v1/import/adapters')
    def import_adapters():
        return importing.adapters()

    @app.post('/api/v1/import/preview', dependencies=[Depends(browser_write)])
    def import_preview(body: ImportPreview):
        return importing.preview(body.source, body.paths, body.plan)

    @app.post('/api/v1/import/register', dependencies=[Depends(browser_write)])
    def import_register(body: ImportRegister, conn=Depends(write_connection)):
        return importing.register(conn, body.source, body.paths, body.objects,
                                  body.parser_version, body.actor, body.reason, body.plan)

    @app.get('/api/v1/stats')
    def stats(conn=Depends(connection)):
        return query.overview(conn)

    @app.get('/api/v1/years/options')
    def year_options(conn=Depends(connection)):
        return query.year_options(conn)

    @app.get('/api/v1/stats/years')
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

    @app.get('/api/v1/documents/{doc_id}/metadata')
    def metadata(doc_id: int, version: Annotated[int | None, Query(ge=1)] = None, conn=Depends(connection)):
        result = query.metadata(conn, doc_id, version)
        if result is None:
            raise HTTPException(404, '文档或版本不存在')
        return result

    @app.post('/api/v1/documents/{doc_id}/review', dependencies=[Depends(browser_write)])
    def review(doc_id: int, body: DocumentReview, conn=Depends(write_connection)):
        return management.review_document(conn, doc_id, body.expected_version_id, body.metadata,
                                          actor=body.actor, reason=body.reason)

    @app.get('/api/v1/documents/{doc_id}/works')
    def works(doc_id: int, conn=Depends(connection)):
        return management.work_links(conn, doc_id)

    @app.post('/api/v1/documents/{doc_id}/works/review', dependencies=[Depends(browser_write)])
    def review_work(doc_id: int, body: WorkReview, conn=Depends(write_connection)):
        return management.review_work(conn, doc_id, body.work_id, body.relation, body.decision,
                                      actor=body.actor, reason=body.reason, expected_status=body.expected_status)

    @app.get('/api/v1/events')
    def events(doc_id: int | None = None, batch_id: int | None = None, conn=Depends(connection)):
        return management.events(conn, doc_id=doc_id, batch_id=batch_id, limit=100)

    @app.get('/api/v1/batches')
    def batch_list(include_published: bool = False, before: Annotated[int | None, Query(gt=0)] = None,
                   limit: Annotated[int, Query(ge=1, le=100)] = 30, conn=Depends(connection)):
        rows = batches.list_batches(conn, limit=limit+1, include_published=include_published, before=before)
        return {'items': rows[:limit], 'next_cursor': rows[limit-1]['batch_id'] if len(rows)>limit else None}

    @app.get('/api/v1/batches/{batch_id}')
    def batch_detail(batch_id: int, conn=Depends(connection)):
        return batches.inspect(conn, batch_id)

    @app.post('/api/v1/batches/{batch_id}/resume', dependencies=[Depends(browser_write)])
    def resume(batch_id: int, body: Operation, conn=Depends(write_connection)):
        return batches.resume(conn, batch_id, actor=body.actor, reason=body.reason)

    @app.post('/api/v1/batches/{batch_id}/abandon', dependencies=[Depends(browser_write)])
    def abandon(batch_id: int, body: Operation, conn=Depends(write_connection)):
        return batches.abandon(conn, batch_id, actor=body.actor, reason=body.reason)

    @app.exception_handler(psycopg.OperationalError)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "语料库暂不可用，请检查数据库服务"})

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "api_version": "1"}

    @app.get("/api/v1/sources")
    def sources(conn=Depends(connection)):
        return [{"name": name} for name, in conn.execute("SELECT name FROM source ORDER BY name")]

    @app.get("/api/v1/documents")
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

    @app.get("/api/v1/documents/{doc_id}/versions")
    def versions(doc_id: int, conn=Depends(connection)):
        rows = conn.execute("SELECT version_id, version_no, is_current, title, char_count, quality_status, created_at"
            " FROM document_version WHERE doc_id = %s ORDER BY version_no DESC", (doc_id,)).fetchall()
        if not rows:
            raise HTTPException(404, "文档不存在")
        keys = ('version_id','version_no','is_current','title','char_count','quality_status','created_at')
        return [dict(zip(keys, row)) for row in rows]

    @app.get("/api/v1/documents/{doc_id}")
    def document(doc_id: int, version: Annotated[int | None, Query(ge=1)] = None,
                 offset: Annotated[int, Query(ge=0)] = 0,
                 length: Annotated[int, Query(ge=1, le=20000)] = 10000, conn=Depends(connection)):
        result = query.get_text(conn, doc_id, version)
        if result is None:
            raise HTTPException(404, "文档或版本不存在")
        text = result.pop('text')
        return {**result, "text": text[offset:offset+length], "offset": offset,
                "next_offset": offset+length if offset+length<len(text) else None}

    @app.get("/api/{unknown:path}", include_in_schema=False)
    def missing(unknown: str):
        raise HTTPException(404, "接口不存在")

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


def main():
    import argparse
    import os
    import uvicorn
    from . import server
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--static-dir', type=Path, default=Path(__file__).resolve().parents[2] / 'web/dist')
    args = parser.parse_args()
    if 'MIDSTREAM_DSN' not in os.environ:
        server.ensure_running()
    uvicorn.run(create_app(args.static_dir), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
