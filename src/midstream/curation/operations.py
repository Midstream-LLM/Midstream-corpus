"""整理操作：按规则生成新版本。"""

from pydantic import field_validator

from ..operations import Context, Params, operation
from . import reprocess
from .rules import RULES


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
