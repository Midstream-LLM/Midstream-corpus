"""查重操作。"""

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
