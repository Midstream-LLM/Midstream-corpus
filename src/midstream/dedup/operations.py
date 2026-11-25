"""查重操作。"""

from pydantic import Field

from ..operations import Context, Params, operation
from . import engine as dedup


class DedupParams(Params):
    full: bool = False


@operation('dedup', '当前版本的文档级查重')
def _dedup(conn, params: DedupParams, ctx: Context):
    return dedup.dedup(conn, full=params.full, progress=ctx.progress)


@operation('dedup_spans', '当前版本的段落级查重')
def _dedup_spans(conn, params: DedupParams, ctx: Context):
    return dedup.dedup_spans(conn, full=params.full, progress=ctx.progress)


class EstimateParams(Params):
    batch_id: int = Field(gt=0)
    sample_chars: int = Field(default=5_000_000, ge=10_000, le=50_000_000)


@operation('batch_estimate', '抽样估算批次去重后的净增字数（发布前，只读）')
def _batch_estimate(conn, params: EstimateParams, ctx: Context):
    from .estimate import estimate
    return estimate(conn, params.batch_id, sample_chars=params.sample_chars, progress=ctx.progress)
