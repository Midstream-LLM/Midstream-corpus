"""交付操作：文本导出、校验、token 统计与 token 分片。"""

from pydantic import Field

from ..datasets.operations import ReleaseParams
from ..operations import Context, operation
from . import token_stats, tokenizing
from ..datasets.recipe import ExportError
from .export import Options, export_release, verify_release
from .tokens import write_tokens


class DatasetExportParams(ReleaseParams):
    options: Options = Field(default_factory=Options)


class DatasetVerifyParams(ReleaseParams):
    hash_files: bool = False


class TokenStatsParams(ReleaseParams):
    tokenizer: str = Field(min_length=1, pattern=r'\S')


class TokenizeParams(TokenStatsParams):
    shard_tokens: int = Field(default=200_000_000, gt=0)


@operation('dataset_export', '把固定发布导出为 Parquet、JSONL 或 PKL')
def _dataset_export(conn, params: DatasetExportParams, ctx: Context):
    return export_release(conn, params.release_id, params.options, progress=ctx.progress)


@operation('token_stats', '按分词器统计数据集 token 数')
def _token_stats(conn, params: TokenStatsParams, ctx: Context):
    return token_stats.compute(conn, params.release_id, tokenizing.load(params.tokenizer), progress=ctx.progress)


@operation('tokenize', '统计 token 并写出 NPY token 分片')
def _tokenize(conn, params: TokenizeParams, ctx: Context):
    tokenizer = tokenizing.load(params.tokenizer)
    token_stats.compute(conn, params.release_id, tokenizer, progress=ctx.progress)
    return write_tokens(conn, params.release_id, tokenizer,
                                  shard_tokens=params.shard_tokens, progress=ctx.progress)


@operation('dataset_verify', '校验固定发布与导出文件')
def _dataset_verify(conn, params: DatasetVerifyParams, ctx: Context):
    ctx.progress(0, 1, '校验固定发布与导出文件')
    if not verify_release(conn, params.release_id, hash_files=params.hash_files):
        raise ExportError('校验未通过')
    return {'release_id': params.release_id, 'valid': True}
