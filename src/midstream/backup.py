"""pg_dump/pg_restore 与 OSS 流式传输；恢复只创建新库。"""
import os
import subprocess
import tempfile
from datetime import timedelta,timezone

import oss2
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from psycopg.types.json import Jsonb

from . import backup_settings,server
from .management import ManagementError

PART_BYTES=8<<20


def _bucket(cfg,local,endpoint=None,bucket=None):
    keys=backup_settings.credentials(local)
    if not (cfg.endpoint and cfg.bucket and keys['access_key_id'] and keys['access_key_secret']):
        raise ManagementError('invalid_input','请先配置 OSS endpoint、bucket 和凭据')
    auth=oss2.StsAuth(keys['access_key_id'],keys['access_key_secret'],keys['security_token']) if keys['security_token'] else \
        oss2.Auth(keys['access_key_id'],keys['access_key_secret'])
    return oss2.Bucket(auth,endpoint or cfg.endpoint,bucket or cfg.bucket)


def test_connection():
    cfg,local=backup_settings.read()
    try:
        _bucket(cfg,local).get_bucket_info()
    except oss2.exceptions.OssError:
        raise ManagementError('backup_failed','OSS 连接失败，请检查配置和凭据') from None
    return {'connected':True}


def _command(tool,dsn=None,*options):
    env=dict(os.environ)
    command=[str(server.BIN/tool)]
    if dsn:
        values=conninfo_to_dict(dsn)
        password=values.pop('password',None)
        if password:
            env['PGPASSWORD']=password
        command+=['--dbname',make_conninfo(**values)]
    return command+list(options),env


def list_backups(conn):
    return [row[0] for row in conn.execute('SELECT to_jsonb(b) FROM backup_record b ORDER BY backup_id DESC LIMIT 100')]


def _record(conn,backup_id):
    row=conn.execute("SELECT to_jsonb(b) FROM backup_record b WHERE backup_id=%s AND status='completed'",(backup_id,)).fetchone()
    if row is None:
        raise ManagementError('not_found','备份不存在或尚未完成')
    return row[0]


def run(conn,actor,reason,*,progress=None):
    cfg,local=backup_settings.read()
    bucket=_bucket(cfg,local)
    backup_id,created=conn.execute("INSERT INTO backup_record(endpoint,bucket,object_key,database_name,status,actor,reason)"
        " VALUES(%s,%s,'',%s,'running',%s,%s) RETURNING backup_id,created_at",
        (cfg.endpoint,cfg.bucket,conn.info.dbname,actor,reason)).fetchone()
    prefix=cfg.prefix.strip('/')
    stamp=created.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    key=(prefix+'/' if prefix else '')+f'{conn.info.dbname}/backup-{backup_id}-{stamp}/database.dump'
    conn.execute('UPDATE backup_record SET object_key=%s WHERE backup_id=%s',(key,backup_id));conn.commit()
    upload=None;process=None;objects=[];size=0
    try:
        upload=bucket.init_multipart_upload(key).upload_id
        command,env=_command('pg_dump',conn.info.dsn,'-Fc','--no-owner','--no-privileges')
        with tempfile.TemporaryFile() as errors:
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,env=env)
            parts=[]
            while block:=process.stdout.read(PART_BYTES):
                if progress:
                    progress(size,0,'流式上传数据库备份')
                result=bucket.upload_part(key,upload,len(parts)+1,block)
                parts.append(oss2.models.PartInfo(len(parts)+1,result.etag))
                size+=len(block)
            process.stdout.close()
            if process.wait()!=0:
                errors.seek(0)
                raise ManagementError('backup_failed','pg_dump 失败：'+errors.read(4000).decode('utf-8',errors='replace'))
            bucket.complete_multipart_upload(key,upload,parts)
            upload=None;objects.append(key)
        if cfg.include_config:
            from .llm.config import config_path
            for path in (backup_settings.path(),config_path()):
                if path.is_file():
                    data=path.read_bytes()
                    config_key=key.rsplit('/',1)[0]+'/config/'+path.name
                    bucket.put_object(config_key,data);objects.append(config_key);size+=len(data)
        conn.execute("UPDATE backup_record SET status='completed',objects=%s,byte_count=%s,finished_at=now()"
                     ' WHERE backup_id=%s',(Jsonb(objects),size,backup_id));conn.commit()
    except BaseException as exc:
        if process is not None and process.poll() is None:
            process.kill();process.wait()
        if process is not None:
            process.stdout.close()
        failure=ManagementError('backup_failed','OSS 备份失败，请检查连接和凭据') if isinstance(exc,oss2.exceptions.OssError) else exc
        try:
            if upload is not None:
                bucket.abort_multipart_upload(key,upload)
            for name in objects:
                bucket.delete_object(name)
        except oss2.exceptions.OssError:
            failure=ManagementError('backup_failed','OSS 备份失败，临时对象清理未完成')
        conn.rollback()
        error=str(failure) or 'interrupted'
        conn.execute("UPDATE backup_record SET status='failed',error=%s,finished_at=now() WHERE backup_id=%s",(error,backup_id));conn.commit()
        raise failure from None
    # 新备份完成后才执行保留策略，清理失败不会回滚新备份。
    for old_id,old_objects in conn.execute("SELECT backup_id,objects FROM backup_record WHERE endpoint=%s AND bucket=%s"
            " AND status='completed' ORDER BY backup_id DESC OFFSET %s",(cfg.endpoint,cfg.bucket,cfg.keep)).fetchall():
        conn.execute("UPDATE backup_record SET status='deleting' WHERE backup_id=%s",(old_id,));conn.commit()
        try:
            for name in old_objects:
                bucket.delete_object(name)
        except oss2.exceptions.OssError:
            raise ManagementError('backup_failed','新备份已完成，旧备份清理失败') from None
        conn.execute("UPDATE backup_record SET status='deleted' WHERE backup_id=%s",(old_id,));conn.commit()
    return {'backup_id':backup_id,'bytes':size,'object_key':key}


def _restore_stream(record,dsn=None,*,progress=None):
    cfg,local=backup_settings.read()
    bucket=_bucket(cfg,local,record['endpoint'],record['bucket'])
    command,env=_command('pg_restore',dsn,*(['--no-owner','--no-privileges'] if dsn else ['--list']))
    downloaded=0
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=output,stderr=errors,env=env)
        try:
            with bucket.get_object(record['object_key']) as response:
                broken=False
                while block:=response.read(PART_BYTES):
                    if progress:
                        progress(downloaded,0,'下载并校验备份' if dsn is None else '恢复到新数据库')
                    if not broken:
                        try:
                            process.stdin.write(block)
                        except BrokenPipeError:
                            broken=True
                    downloaded+=len(block)
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass  # pg_restore --list 可在读完目录后先退出；下方仍检查退出码。
            if process.wait()!=0:
                errors.seek(0)
                raise ManagementError('backup_failed','pg_restore 失败：'+errors.read(4000).decode('utf-8',errors='replace'))
        except BaseException:
            if process.poll() is None:
                process.kill();process.wait()
            raise
    return downloaded


def verify(conn,backup_id,*,progress=None):
    record=_record(conn,backup_id)
    try:
        size=_restore_stream(record,progress=progress)
    except oss2.exceptions.OssError:
        raise ManagementError('backup_failed','OSS 备份下载失败') from None
    conn.execute('UPDATE backup_record SET verified_at=now() WHERE backup_id=%s',(backup_id,));conn.commit()
    return {'backup_id':backup_id,'verified':True,'downloaded_bytes':size}


def restore(conn,backup_id,database):
    if database in ('midstream',conn.info.dbname,'postgres','template0','template1') or not database.strip() or len(database.encode('utf-8'))>63:
        raise ManagementError('invalid_input','只能指定一个全新的数据库名，禁止覆盖 midstream 或现有源库')
    record=_record(conn,backup_id)
    with psycopg.connect(conn.info.dsn,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(database)))
    destination=make_conninfo(conn.info.dsn,dbname=database)
    try:
        _restore_stream(record,destination)
    except oss2.exceptions.OssError:
        raise ManagementError('backup_failed','OSS 备份下载失败；新库保留供检查') from None
    return {'backup_id':backup_id,'database':database}


def recover(conn):
    conn.execute("UPDATE backup_record SET status='failed',error='interrupted',finished_at=now() WHERE status='running'")


def schedule(conn):
    cfg,_=backup_settings.read()
    if cfg.schedule=='none':
        return
    hours=24 if cfg.schedule=='daily' else 168
    latest,now=conn.execute("SELECT max(created_at),clock_timestamp() FROM job WHERE kind='backup'").fetchone()
    if latest is not None and now-latest<timedelta(hours=hours):
        return
    if conn.execute("SELECT EXISTS(SELECT 1 FROM job WHERE kind='backup' AND status IN ('queued','running'))").fetchone()[0]:
        return
    from . import jobs
    jobs.submit(conn,'backup',{'scheduled':True},'scheduler',f'用户配置的 {cfg.schedule} 备份')
