"""备份配置与密钥；默认无定时任务，接口从不返回凭据。"""
import fcntl
import os
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel,ConfigDict,Field,ValidationError,model_validator

from . import config
from .llm.settings import SettingsError,_toml

ENV={'access_key_id':'OSS_ACCESS_KEY_ID','access_key_secret':'OSS_ACCESS_KEY_SECRET','security_token':'OSS_SECURITY_TOKEN'}


class BackupConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    endpoint:str=''
    bucket:str=''
    prefix:str='midstream'
    schedule:str='none'
    keep:int=Field(default=3,ge=1)
    include_config:bool=False

    @model_validator(mode='after')
    def valid(self):
        if self.schedule not in ('none','daily','weekly'):
            raise ValueError('备份周期必须为 none/daily/weekly')
        if self.endpoint:
            url=urlsplit(self.endpoint)
            if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
                raise ValueError('OSS endpoint 必须为不含凭据的 HTTP(S) 地址')
        if self.schedule!='none' and (not self.endpoint or not self.bucket):
            raise ValueError('启用定时备份前需要 endpoint 和 bucket')
        return self


def path():
    return config.HOME/'backup.toml'


def revision():
    if not path().exists():
        return 'missing'
    stat=path().stat()
    return f'{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}'


def read():
    if not path().exists():
        return BackupConfig(),{}
    if path().stat().st_mode & 0o077:
        raise SettingsError('backup.toml 权限须为 0600')
    try:
        raw=tomllib.loads(path().read_text(encoding='utf-8'))
        if set(raw)-{'backup','credentials'}:
            raise ValueError()
        cfg=BackupConfig.model_validate(raw.get('backup',{}))
        keys=raw.get('credentials',{})
        if set(keys)-ENV.keys() or any(not isinstance(v,str) for v in keys.values()):
            raise ValueError()
        return cfg,keys
    except (ValueError,TypeError):
        raise SettingsError('备份配置文件格式无效') from None


def credentials(local):
    return {key:os.environ.get(env) or local.get(key,'') for key,env in ENV.items()}


def get():
    cfg,local=read()
    return {'config':cfg.model_dump(),'revision':revision(),'credentials_status':{
        key:{'configured':bool(os.environ.get(env) or local.get(key)),
             'source':'environment' if os.environ.get(env) else 'local' if local.get(key) else 'missing',
             'local_saved':bool(local.get(key))} for key,env in ENV.items()}}


def save(data,keys,clear_keys,expected):
    try:
        cfg=BackupConfig.model_validate(data)
    except ValidationError:
        raise SettingsError('备份配置格式无效') from None
    if (set(keys)|set(clear_keys))-ENV.keys() or set(keys)&set(clear_keys) or any(not v.strip() for v in keys.values()):
        raise SettingsError('凭据字段无效，留空表示保留')
    path().parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(path())+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if revision()!=expected:
            raise SettingsError('备份配置已变化，请重新加载',409)
        _,local=read()
        local={k:v for k,v in local.items() if k not in clear_keys}
        local.update(keys)
        if cfg.schedule!='none' and not all(credentials(local)[key] for key in ('access_key_id','access_key_secret')):
            raise SettingsError('启用定时备份前需要配置凭据')
        content='[backup]\n'+'\n'.join(k+' = '+_toml(v) for k,v in cfg.model_dump().items())
        content+='\n\n[credentials]\n'+'\n'.join(k+' = '+_toml(v) for k,v in local.items())+'\n'
        temp_fd,temp=tempfile.mkstemp(prefix='.backup-',dir=path().parent)
        try:
            with os.fdopen(temp_fd,'w',encoding='utf-8') as handle:
                handle.write(content);handle.flush();os.fsync(handle.fileno())
            os.replace(temp,path())
        finally:
            if Path(temp).exists():
                Path(temp).unlink()
    return get()
