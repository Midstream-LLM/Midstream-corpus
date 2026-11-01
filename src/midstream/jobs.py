"""PostgreSQL 持久队列；worker 串行执行，阶段边界协作取消。"""

from threading import Event, Thread
from time import sleep

import psycopg
from psycopg.types.json import Jsonb

from . import batches, dedup
from .management import ManagementError, _identity


def _batch_resume(conn, params, actor, reason, progress):
    return batches.resume(conn, params['batch_id'], actor=actor, reason=reason,
                          progress=progress)


def _dedup(conn, params, actor, reason, progress):
    return dedup.dedup(conn, **params, progress=progress)


def _dedup_spans(conn, params, actor, reason, progress):
    return dedup.dedup_spans(conn, **params, progress=progress)


HANDLERS = {'batch_resume': _batch_resume, 'dedup': _dedup, 'dedup_spans': _dedup_spans}


def _event(conn, job_id, message, level='info'):
    conn.execute('INSERT INTO job_event (job_id, level, message) VALUES (%s, %s, %s)',
                 (job_id, level, message))


def submit(conn, kind, params, actor, reason):
    _identity(actor, reason)
    if kind not in HANDLERS or not isinstance(params, dict):
        raise ManagementError('invalid_input', '任务类型或参数无效')
    if kind == 'batch_resume':
        valid = (set(params) == {'batch_id'} and type(params['batch_id']) is int
                 and params['batch_id'] > 0)
    else:
        valid = set(params) <= {'full'} and type(params.get('full', False)) is bool
    if not valid:
        raise ManagementError('invalid_input', '批次任务需要 batch_id；查重仅接受 full 布尔值')
    job_id = conn.execute(
        'INSERT INTO job (kind, params, actor, reason) VALUES (%s, %s, %s, %s) RETURNING job_id',
        (kind, Jsonb(params), actor, reason),
    ).fetchone()[0]
    _event(conn, job_id, f'提交：{actor}；{reason}')
    conn.commit()
    return show(conn, job_id)


def list_jobs(conn, *, limit=30, before=None):
    return [row[0] for row in conn.execute(
        'SELECT to_jsonb(j) FROM job j WHERE (%s::bigint IS NULL OR job_id < %s)'
        ' ORDER BY job_id DESC LIMIT %s', (before, before, limit),
    ).fetchall()]


def _get(conn, job_id, *, lock=False):
    row = conn.execute('SELECT to_jsonb(j) FROM job j WHERE job_id = %s'
                       + (' FOR UPDATE' if lock else ''), (job_id,)).fetchone()
    if row is None:
        raise ManagementError('not_found', f'任务 {job_id} 不存在')
    return row[0]


def show(conn, job_id):
    job = _get(conn, job_id)
    job['events'] = [row[0] for row in conn.execute(
        'SELECT to_jsonb(e) FROM job_event e WHERE job_id = %s'
        ' ORDER BY event_id DESC LIMIT 100', (job_id,),
    ).fetchall()][::-1]
    return job


def cancel(conn, job_id, actor, reason):
    _identity(actor, reason)
    job = _get(conn, job_id, lock=True)
    if job['status'] not in ('queued', 'running'):
        raise ManagementError('conflict', '任务已经结束')
    conn.execute(
        "UPDATE job SET cancel_requested = true,"
        " status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,"
        " finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END"
        " WHERE job_id = %s", (job_id,),
    )
    _event(conn, job_id, f'请求取消：{actor}；{reason}')
    conn.commit()
    return show(conn, job_id)


def retry(conn, job_id, actor, reason):
    job = _get(conn, job_id)
    if job['status'] not in ('failed', 'cancelled'):
        raise ManagementError('conflict', '只能重试失败或已取消的任务')
    result = submit(conn, job['kind'], job['params'], actor, reason)
    _event(conn, result['job_id'], f'重试原任务 #{job_id}')
    conn.commit()
    return show(conn, result['job_id'])


class Cancelled(Exception):
    pass


def _recover(conn):
    with conn.transaction():
        rows = conn.execute(
            "UPDATE job SET status = 'failed', error = 'interrupted', finished_at = now()"
            " WHERE status = 'running' AND heartbeat_at < now() - interval '60 seconds'"
            " RETURNING job_id",
        ).fetchall()
        for job_id, in rows:
            _event(conn, job_id, 'interrupted', 'error')


def _run_next(conn, control):
    with control.transaction():
        row = control.execute(
            "SELECT to_jsonb(j) FROM job j WHERE status = 'queued'"
            " ORDER BY job_id FOR UPDATE SKIP LOCKED LIMIT 1",
        ).fetchone()
        if row is None:
            return False
        job = row[0]
        job_id = job['job_id']
        control.execute("UPDATE job SET status = 'running', started_at = now(), heartbeat_at = now()"
                        " WHERE job_id = %s", (job_id,))
        _event(control, job_id, '开始执行')

    def progress(current, total, message):
        with control.transaction():
            requested = control.execute(
                'UPDATE job SET progress = %s WHERE job_id = %s RETURNING cancel_requested',
                (Jsonb({'current': current, 'total': total, 'message': message}), job_id),
            ).fetchone()[0]
            _event(control, job_id, message)
        if requested:
            raise Cancelled()

    stopped = Event()

    def heartbeat():
        while not stopped.wait(5):
            control.execute('UPDATE job SET heartbeat_at = now() WHERE job_id = %s', (job_id,))

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    result, error = None, None
    try:
        progress(0, 0, '准备执行')
        result = HANDLERS[job['kind']](conn, job['params'], job['actor'], job['reason'], progress)
        conn.commit()
        status, message = 'succeeded', '执行完成'
    except Cancelled:
        conn.rollback()
        status, message = 'cancelled', '已取消；已完成阶段保留'
    except Exception as exc:
        conn.rollback()
        status, error = 'failed', str(exc)
        message = error
    finally:
        stopped.set()
        thread.join()
    with control.transaction():
        control.execute(
            'UPDATE job SET status = %s, result = %s, error = %s, finished_at = now(),'
            ' heartbeat_at = now(), progress = CASE WHEN %s = \'succeeded\''
            " THEN jsonb_build_object('current', 1, 'total', 1, 'message', %s::text)"
            ' ELSE progress END WHERE job_id = %s',
            (status, Jsonb(result), error, status, message, job_id),
        )
        _event(control, job_id, message, 'error' if status == 'failed' else 'info')
    return True


def worker(conn):
    """手动启动；数据库级锁限制为一个 worker，退出后任务由心跳恢复。"""
    with psycopg.connect(conn.info.dsn, autocommit=True) as control:
        if not control.execute('SELECT pg_try_advisory_lock(762032)').fetchone()[0]:
            raise ManagementError('conflict', '已有 worker 正在运行')
        try:
            while True:
                _recover(control)
                if not _run_next(conn, control):
                    sleep(2)
        except KeyboardInterrupt:
            conn.rollback()
            return 0
