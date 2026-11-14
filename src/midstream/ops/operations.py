"""运维操作：备份与备份校验。"""

from pydantic import Field

from ..operations import Context, Params, operation
from . import backup


class BackupParams(Params):
    scheduled: bool = False


class BackupVerifyParams(Params):
    backup_id: int = Field(gt=0)


@operation('backup', '备份数据库到 OSS')
def _backup(conn,params: BackupParams,ctx: Context):
    return backup.run(conn,ctx.actor,ctx.reason,progress=ctx.progress)


@operation('backup_verify', '校验 OSS 备份可恢复')
def _backup_verify(conn,params: BackupVerifyParams,ctx: Context):
    return backup.verify(conn,params.backup_id,progress=ctx.progress)
