"""PostgreSQL 持久队列；worker 和就地执行共用 _execute，阶段边界协作取消。"""

import signal
import sys
from threading import Event, Thread
from time import sleep

import psycopg
from psycopg.types.json import Jsonb

from pydantic import ValidationError

from . import operations
from .corpus.management import ManagementError, _identity


def _event(conn, job_id, message, level='info'):
    conn.execute('INSERT INTO job_event (job_id, level, message) VALUES (%s, %s, %s)',
                 (job_id, level, message))


def _insert(conn, kind, params, actor, reason, *, running=False):
    """按注册表校验参数并写入任务表；不提交。"""
    _identity(actor, reason)
    operations.load()
    if kind not in operations.REGISTRY or not isinstance(params, dict):
        raise ManagementError('invalid_input', '任务类型或参数无效')
    try:
        model = operations.REGISTRY[kind].params.model_validate(params)
    except ValidationError as exc:
        raise ManagementError('invalid_input', f'任务参数与类型不匹配：{exc.errors()[0]["msg"]}') from exc
    params = model.model_dump(mode='json', exclude_unset=True)
    job_id = conn.execute(
        'INSERT INTO job (kind, params, actor, reason, status, started_at, heartbeat_at)'
        " VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END)"
        ' RETURNING job_id',
        (kind, Jsonb(params), actor, reason, 'running' if running else 'queued', running, running),
    ).fetchone()[0]
    _event(conn, job_id, f'提交：{actor}；{reason}')
    return job_id


def submit(conn, kind, params, actor, reason):
    job_id = _insert(conn, kind, params, actor, reason)
    conn.commit()
    return show(conn, job_id)


def _echo(current, total, message):
    print(f'[{current}/{total}] {message}' if total else message, file=sys.stderr, flush=True)


def run_inline(conn, kind, params, actor, reason):
    """写入任务表后在当前进程执行；失败时记录任务并重新抛出原异常。

    后续任务在主任务成功后按顺序接着执行，失败只记录在各自的任务上。
    """
    job_id = _insert(conn, kind, params, actor, reason, running=True)
    conn.commit()
    with psycopg.connect(conn.info.dsn, autocommit=True) as control:
        _event(control, job_id, '就地执行')
        error, follow_ups = _execute(conn, control, _get(control, job_id), inline=True)
        if error is not None:
            raise error
        result = show(control, job_id)
        result['follow_ups'] = []
        while follow_ups:
            follow_kind, follow_params = follow_ups.pop(0)
            child = _insert(control, follow_kind, follow_params, actor,
                            f'{kind} #{job_id} 的后续任务', running=True)
            _event(control, job_id, f'后续任务 #{child}：{follow_kind}')
            _, more = _execute(conn, control, _get(control, child), inline=True)
            follow_ups.extend(more)
            result['follow_ups'].append(show(control, child))
    return result


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
        control.execute("UPDATE job SET status = 'running', started_at = now(), heartbeat_at = now()"
                        " WHERE job_id = %s", (job['job_id'],))
        _event(control, job['job_id'], '开始执行')
    _execute(conn, control, job)
    return True


def _execute(conn, control, job, *, inline=False):
    """执行一条已标记为 running 的任务；worker 把后续任务写入队列，就地执行时返回给调用方。"""
    job_id = job['job_id']
    operations.load()
    op = operations.REGISTRY[job['kind']]

    def progress(current, total, message):
        with control.transaction():
            requested = control.execute(
                'UPDATE job SET progress = %s WHERE job_id = %s RETURNING cancel_requested',
                (Jsonb({'current': current, 'total': total, 'message': message}), job_id),
            ).fetchone()[0]
            _event(control, job_id, message)
        if inline:
            _echo(current, total, message)
        if requested:
            raise Cancelled()

    stopped = Event()

    def heartbeat():
        while not stopped.wait(5):
            control.execute('UPDATE job SET heartbeat_at = now() WHERE job_id = %s', (job_id,))

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    ctx = operations.Context(job['actor'], job['reason'], progress)
    result, error, interrupted = None, None, None
    try:
        progress(0, 0, '准备执行')
        result = op.run(conn, op.params.model_validate(job['params']), ctx)
        conn.commit()
        status, message = 'succeeded', '执行完成'
    except Cancelled:
        conn.rollback()
        status, message = 'cancelled', '已取消；已完成阶段保留'
    except KeyboardInterrupt as exc:
        conn.rollback()
        status, message, interrupted = 'cancelled', '已中断；已完成阶段保留', exc
    except Exception as exc:
        conn.rollback()
        status, error = 'failed', exc
        message = str(exc)
    finally:
        stopped.set()
        thread.join()
    follow_ups = ctx.follow_ups if status == 'succeeded' else []
    with control.transaction():
        if not inline:
            for kind, params in follow_ups:
                child = _insert(control, kind, params, job['actor'], f'{job["kind"]} #{job_id} 的后续任务')
                _event(control, job_id, f'后续任务 #{child}：{kind}')
        control.execute(
            'UPDATE job SET status = %s, result = %s, error = %s, finished_at = now(),'
            ' heartbeat_at = now(), progress = CASE WHEN %s = \'succeeded\''
            " THEN jsonb_build_object('current', 1, 'total', 1, 'message', %s::text)"
            ' ELSE progress END WHERE job_id = %s',
            (status, Jsonb(result), message if error else None, status, message, job_id),
        )
        _event(control, job_id, message, 'error' if status == 'failed' else 'info')
    if interrupted is not None:
        raise interrupted
    return error, follow_ups


def worker(conn):
    """数据库级锁限制为一个 worker；SIGINT 和 SIGTERM 都把当前任务记为已取消后退出。"""
    # 后台启动的进程可能继承了被忽略的 SIGINT，这里显式恢复；SIGTERM 是 serve 和 systemd 的停止信号。
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    with psycopg.connect(conn.info.dsn, autocommit=True) as control:
        if not control.execute('SELECT pg_try_advisory_lock(762032)').fetchone()[0]:
            raise ManagementError('conflict', '已有 worker 正在运行')
        from .ops import backup
        backup.recover(control)
        try:
            while True:
                _recover(control)
                backup.schedule(control, submit)
                if not _run_next(conn, control):
                    sleep(2)
        except KeyboardInterrupt:
            conn.rollback()
            return 0
