"""备份设置、备份与校验。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import jobs
from ..ops import backup, backup_settings
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


class BackupSettingsSave(BaseModel):
    model_config=ConfigDict(extra='forbid')
    config:dict
    keys:dict[str,str]=Field(default_factory=dict)
    clear_keys:list[str]=Field(default_factory=list)
    revision:str


@router.get('/api/v1/backup/settings')
def backup_settings_get():
    return backup_settings.get()


@router.post('/api/v1/backup/settings',dependencies=[Depends(browser_write)])
def backup_settings_save(body:BackupSettingsSave):
    return backup_settings.save(body.config,body.keys,body.clear_keys,body.revision)


@router.post('/api/v1/backup/test',dependencies=[Depends(browser_write)])
def backup_connection_test():
    return backup.test_connection()


@router.get('/api/v1/backups')
def backup_list(conn=Depends(connection)):
    return backup.list_backups(conn)


@router.post('/api/v1/backups',dependencies=[Depends(browser_write)])
def backup_run(body:Operation,conn=Depends(write_connection)):
    return jobs.submit(conn,'backup',{},body.actor,body.reason)


@router.post('/api/v1/backups/{backup_id}/verify',dependencies=[Depends(browser_write)])
def backup_verify(backup_id:int,body:Operation,conn=Depends(write_connection)):
    return jobs.submit(conn,'backup_verify',{'backup_id':backup_id},body.actor,body.reason)
