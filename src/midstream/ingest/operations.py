"""入库操作：整源入库与批次处理。"""

import dataclasses
from pathlib import Path

from pydantic import Field, field_validator

from ..operations import Context, Params, operation
from . import batches, pipeline as ingest
from .adapters import ADAPTERS
from ..curation.models import CurationConfig

# 发布改变了当前版本；查重作为后续任务，不在发布事务里。
DEDUP = [('dedup', {}), ('dedup_spans', {})]


class BatchParams(Params):
    batch_id: int = Field(gt=0)


class IngestParams(Params):
    curation: CurationConfig | None = None
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
        report = ingest.ingest(conn, adapter, paths, curation=params.curation, actor=ctx.actor, reason=ctx.reason)
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


class PublishParams(BatchParams):
    # 仍有未完成入库清洗的记录时也发布；这些记录留到库内补清洗。
    allow_uncurated: bool = False


@operation('batch_publish', '发布已验证的批次')
def _batch_publish(conn, params: PublishParams, ctx: Context):
    return _published(batches.publish(conn, params.batch_id, actor=ctx.actor, reason=ctx.reason,
                                       progress=ctx.progress, allow_uncurated=params.allow_uncurated), ctx)


class OcrParams(Params):
    paths: list[str] = Field(min_length=1)
    engine: str
    # 本次最多新识别多少页；已缓存的页不计。付费引擎用它控制花费。
    max_pages: int | None = Field(default=None, gt=0)

    @field_validator('engine')
    @classmethod
    def known_engine(cls, value):
        from .ocr import ENGINES
        if value not in ENGINES:
            raise ValueError(f'未知 OCR 引擎：{value}')
        return value


@operation('ocr', '识别扫描件（PDF/图片），逐页缓存结果，供 scan 格式入库')
def _ocr(conn, params: OcrParams, ctx: Context):
    from . import ocr
    return ocr.run(params.paths, params.engine, max_pages=params.max_pages, progress=ctx.progress)
