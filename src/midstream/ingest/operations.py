"""入库操作：整源入库与批次处理。"""

import dataclasses
from pathlib import Path

from pydantic import Field, field_validator

from ..operations import Context, Params, operation
from . import batches, pipeline as ingest
from .adapters import ADAPTERS

# 发布改变了当前版本；查重作为后续任务，不在发布事务里。
DEDUP = [('dedup', {}), ('dedup_spans', {})]


class BatchParams(Params):
    batch_id: int = Field(gt=0)


class IngestParams(Params):
    source: str
    paths: list[str] = Field(default_factory=list)

    @field_validator('source')
    @classmethod
    def known(cls, value):
        if value not in ADAPTERS:
            raise ValueError(f'未知来源：{value}')
        return value


@operation('ingest', '登记、暂存、验证并发布一个来源的文件')
def _ingest(conn, params: IngestParams, ctx: Context):
    adapter = ADAPTERS[params.source]
    paths = [Path(p) for p in params.paths] or adapter.default_paths()
    try:
        report = ingest.ingest(conn, adapter, paths)
    except ingest.AlreadyImported as exc:
        return {'status': 'skipped', 'message': str(exc)}
    ctx.follow_ups.extend(DEDUP)
    return dataclasses.asdict(report)


def _published(result, ctx):
    if result['status'] == 'published':
        ctx.follow_ups.extend(DEDUP)
    return result


@operation('batch_resume', '核对原文件后按状态继续处理并发布批次')
def _batch_resume(conn, params: BatchParams, ctx: Context):
    return _published(batches.resume(conn, params.batch_id, actor=ctx.actor, reason=ctx.reason,
                                      progress=ctx.progress), ctx)


@operation('batch_stage', '解析、暂存并验证批次，停在发布前')
def _batch_stage(conn, params: BatchParams, ctx: Context):
    return batches.stage(conn, params.batch_id, actor=ctx.actor, reason=ctx.reason, progress=ctx.progress)


@operation('batch_publish', '发布已验证的批次')
def _batch_publish(conn, params: BatchParams, ctx: Context):
    return _published(batches.publish(conn, params.batch_id, actor=ctx.actor, reason=ctx.reason,
                                       progress=ctx.progress), ctx)
