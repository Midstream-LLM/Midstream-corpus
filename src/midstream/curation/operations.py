"""整理操作：按规则生成新版本。"""

from pydantic import Field, field_validator, model_validator

from ..operations import Context, Params, operation
from . import reprocess
from .rules import RULES
from .models import CurationConfig, ResumeOptions


class ReprocessParams(Params):
    rule: str
    apply: bool = False

    @field_validator('rule')
    @classmethod
    def known(cls, value):
        if value not in RULES:
            raise ValueError(f'未知规则：{value}')
        return value


@operation('reprocess', '按规则生成新版本；apply 为 false 时只预览')
def _reprocess(conn, params: ReprocessParams, ctx: Context):
    report = reprocess.reprocess(conn, RULES[params.rule], dry_run=not params.apply)
    if params.apply:
        # 当前版本变了，查重结果随之过期。
        ctx.follow_ups.extend([('dedup', {}), ('dedup_spans', {})])
    return report


class CurateParams(Params):
    batch_id: int | None = Field(default=None, gt=0)
    filters: dict = Field(default_factory=dict)
    doc_ids: list[int] | None = Field(default=None, min_length=1, max_length=1000)
    # 默认：给了 doc_ids 时全部处理，否则 100 条。
    limit: int | None = Field(default=None, ge=1, le=1000)
    resume_run_id: int | None = Field(default=None, gt=0)
    resume_options: ResumeOptions | None = None
    config: CurationConfig = Field(default_factory=CurationConfig)

    @model_validator(mode='after')
    def selection(self):
        from ..corpus.query import Filters
        Filters.from_dict(self.filters)
        if self.batch_id is not None and (self.filters or self.doc_ids):
            raise ValueError('暂存批次与库内筛选不能同时使用')
        if self.doc_ids and any(type(i) is not int or i <= 0 for i in self.doc_ids):
            raise ValueError('doc_ids 必须是正整数列表')
        return self


@operation('curate', '检索、清洗与标注；支持暂存批次、库内补处理和恢复运行')
def _curate(conn, params: CurateParams, ctx: Context):
    from .pipeline import run
    report = run(conn, actor=ctx.actor, reason=ctx.reason, progress=ctx.progress,
                 **{**params.model_dump(exclude={'config','resume_options'}),
                    'config': params.config, 'resume_options':params.resume_options})
    if params.batch_id is None and report['counts'].get('applied'):
        ctx.follow_ups.extend([('dedup', {}), ('dedup_spans', {})])
    return report
