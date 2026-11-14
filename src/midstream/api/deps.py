"""路由共用的数据库连接、浏览器写保护和操作人请求体。"""

from urllib.parse import urlsplit

import psycopg
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import config


def write_connection():
    with psycopg.connect(config.dsn()) as conn:
        yield conn


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)


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
