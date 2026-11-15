"""本机/SSH 隧道使用的 HTTP API，Web 与未来 SwiftUI 共用；路由按领域分在各子模块。"""
import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..corpus import management
from ..datasets.recipe import ExportError, TrainingLeak
from . import corpus, curation, datasets, dedup, ingest, jobs, llm, ops

# 注册顺序即匹配顺序；兜底的 /api/{unknown:path} 在 create_app 末尾。
ROUTERS = (curation.router, llm.router, ops.router, ingest.router, corpus.router, dedup.router, datasets.router, jobs.router)


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

    @app.exception_handler(ExportError)
    async def export_error(request, exc):
        return JSONResponse(status_code=409 if isinstance(exc, TrainingLeak) else 422,
                            content={'detail': str(exc)})

    @app.exception_handler(ValueError)
    async def recipe_error(request, exc):
        return JSONResponse(status_code=422, content={'detail': str(exc)})

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def duplicate(request, exc):
        return JSONResponse(status_code=409, content={'detail': '名称或记录已存在'})

    from ..llm.errors import LLMError

    @app.exception_handler(LLMError)
    async def llm_error(request, exc):
        return JSONResponse(status_code=502, content={'detail': str(exc), 'code': exc.code})

    from ..llm import settings as llm_settings
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith(('/api/v1/llm/','/api/v1/backup/')):
            return JSONResponse(status_code=422, content={'detail':
                '备份配置请求格式无效' if request.url.path.startswith('/api/v1/backup/') else 'LLM 配置请求格式无效'})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(llm_settings.SettingsError)
    async def settings_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail': str(exc)})

    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(psycopg.OperationalError)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "语料库暂不可用，请检查数据库服务"})

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "api_version": "1"}

    @app.get("/api/{unknown:path}", include_in_schema=False)
    def missing(unknown: str):
        raise HTTPException(404, "接口不存在")

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


def serve(host='127.0.0.1', port=8765, static_dir=None, *, worker=False):
    """确保内置 PostgreSQL 运行，可选以子进程启动 worker，前台运行 API；退出时结束 worker。"""
    import signal
    import subprocess
    import sys
    import uvicorn
    from ..ops import server
    if 'MIDSTREAM_DSN' not in os.environ:
        server.ensure_running()
    process = subprocess.Popen([sys.executable, '-m', 'midstream.cli', 'worker']) if worker else None
    # uvicorn 关闭后会把收到的信号交还给原处理函数；让 SIGTERM 正常退出，下面的 finally 才会执行。
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        uvicorn.run(create_app(static_dir), host=host, port=port)
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


STATIC_DIR = Path(__file__).resolve().parents[3] / 'web/dist'

