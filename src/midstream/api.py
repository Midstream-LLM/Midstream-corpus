"""本机/SSH 隧道使用的只读 HTTP API，Web 与未来 SwiftUI 共用。"""
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, query


def connection():
    with psycopg.connect(config.dsn()) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        yield conn


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Midstream Corpus", version="1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

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
                  year_from: int | None = None, year_to: int | None = None,
                  limit: Annotated[int, Query(ge=1, le=100)] = 30,
                  after: Annotated[int, Query(ge=0)] = 0, conn=Depends(connection)):
        if year_from is not None and year_to is not None and year_from > year_to:
            raise HTTPException(422, "起始年份不能晚于结束年份")
        rows = query.search(conn, query.Filters(title_prefix=title, author=author,
            sources=[source] if source else [], category=category, genre=genre,
            quality=[quality] if quality else ['ok','flagged','quarantined'],
            year_from=year_from, year_to=year_to), limit=limit + 1, after_doc=after)
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
