"""Server-side file browsing and cancellable, read-only import discovery."""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import psycopg

from .. import config
from ..corpus.management import ManagementError
from .detection import detect_one

_lock = threading.RLock()
_scans = {}
MAX_FILES = 10000


def browse(name=None, offset=0, limit=200):
    path = Path(name) if name else (config.JISHUI_RAW or Path.home())
    if not path.is_absolute():
        raise ManagementError('invalid_input', '请选择绝对路径')
    try:
        path = path.resolve(strict=True)
        entries = []
        with os.scandir(path) as children:
            for child in children:
                if child.name.startswith('.') or child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False) or child.is_file(follow_symlinks=False):
                    entries.append({'name': child.name, 'path': child.path,
                                    'directory': child.is_dir(follow_symlinks=False),
                                    'size_bytes': child.stat(follow_symlinks=False).st_size})
        entries.sort(key=lambda x: (not x['directory'], x['name'].casefold()))
    except OSError as exc:
        raise ManagementError('invalid_input', '目录不存在或无法读取') from exc
    return {'path': str(path), 'parent': str(path.parent), 'total': len(entries),
            'items': entries[offset:offset + limit],
            'next_offset': offset + limit if offset + limit < len(entries) else None}


def fields_of(record, prefix=''):
    result = {}
    if isinstance(record, dict):
        for key, value in record.items():
            name = f'{prefix}.{key}' if prefix else key
            if isinstance(value, dict):
                result.update(fields_of(value, name))
            else:
                result[name] = {'type': type(value).__name__,
                                'example': json.dumps(value, ensure_ascii=False, default=str)[:180]}
    return result


def inspect_file(path, conn, stop):
    before = path.stat()
    result = detect_one(path)
    sample = result.pop('_sample')
    fields = fields_of(sample['record'])
    result.update(size_bytes=before.st_size, fields=fields, encoding=sample['encoding'],
                  sample=sample['sample'][:1500])
    signature = {'format': result['format'], 'encoding': sample['encoding'],
                 'fields': {k: v['type'] for k, v in fields.items()}}
    result['signature'] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    if result['format'] == 'unsupported':
        return dict(result, import_status='unsupported', previous_batches=[])
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1 << 20):
            if stop.is_set():
                raise InterruptedError()
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('文件在扫描时发生变化，请重新扫描')
    sha = digest.digest()
    rows = conn.execute(
        """SELECT b.batch_id, b.status, s.name, o.sha256 = %s AS same
           FROM raw_object o JOIN source_batch b USING (batch_id)
           JOIN source s USING (source_id)
           WHERE b.status <> 'abandoned' AND (o.path = %s OR o.sha256 = %s)
           ORDER BY b.batch_id DESC""", (sha, str(path), sha)).fetchall()
    same = [r for r in rows if r[3]]
    result.update(sha256=sha.hex(),
                  import_status=('imported' if any(r[1] == 'published' for r in same)
                                 else 'pending' if same else 'changed' if rows else 'new'),
                  previous_batches=[{'batch_id': r[0], 'status': r[1], 'source': r[2]} for r in rows])
    return result


def _snapshot(scan, offset=0):
    return {k: v for k, v in scan.items() if k not in ('stop', 'items')} | {
        'items': scan['items'][offset:offset + 200],
        'next_offset': min(offset + 200, len(scan['items'])),
        'found': len(scan['items']),
    }


def start(paths, recursive=True):
    roots = []
    for name in paths:
        path = Path(name)
        if not path.is_absolute() or path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ManagementError('invalid_input', '请选择可读取的文件或文件夹')
        roots.append(path.resolve())
    with _lock:
        for key, old in list(_scans.items()):
            if old['status'] != 'running' and time.time() - old['created_at'] > 86400:
                del _scans[key]
        if any(s['status'] == 'running' for s in _scans.values()):
            raise ManagementError('conflict', '已有扫描正在进行，请等待或取消后重试')
        if len(_scans) >= 10:
            del _scans[min(_scans, key=lambda key: _scans[key]['created_at'])]
        scan_id = uuid.uuid4().hex
        scan = dict(scan_id=scan_id, status='running', current='', items=[], errors=[],
                    created_at=time.time(), stop=threading.Event(), paths=[str(p) for p in roots])
        _scans[scan_id] = scan
    threading.Thread(target=_run, args=(scan, roots, recursive), daemon=True).start()
    return _snapshot(scan)


def _run(scan, roots, recursive):
    visited = set()
    seen_hashes = {}
    stop = scan['stop']
    try:
        with psycopg.connect(config.dsn(), autocommit=True) as conn:
            conn.execute('SET default_transaction_read_only = on')
            stack = list(reversed(roots))
            while stack and not stop.is_set():
                path = stack.pop()
                if path in visited or path.is_symlink():
                    continue
                visited.add(path)
                if path.is_dir():
                    try:
                        with os.scandir(path) as entries:
                            children = [Path(e.path) for e in entries if not e.name.startswith('.')
                                        and not e.is_symlink()
                                        and (e.is_file(follow_symlinks=False)
                                             or (recursive and e.is_dir(follow_symlinks=False)))]
                        stack.extend(sorted(children, reverse=True))
                    except OSError:
                        with _lock:
                            scan['errors'].append(f'无法读取目录：{path}')
                    continue
                if not path.is_file():
                    continue
                if len(scan['items']) >= MAX_FILES:
                    with _lock:
                        scan['errors'].append(f'本次达到 {MAX_FILES} 个文件上限，请缩小选择范围后继续扫描')
                    break
                with _lock:
                    scan['current'] = str(path)
                try:
                    item = inspect_file(path, conn, stop)
                    sha = item.get('sha256')
                    if sha and sha in seen_hashes and item['import_status'] == 'new':
                        item.update(import_status='duplicate', duplicate_of=seen_hashes[sha])
                    if sha:
                        seen_hashes[sha] = str(path)
                except InterruptedError:
                    break
                except (OSError, ValueError, TypeError) as exc:
                    item = {'path': str(path), 'import_status': 'error', 'note': str(exc),
                            'format': 'unknown', 'signature': '', 'fields': {}, 'plan': None,
                            'source': None, 'known': False, 'previous_batches': []}
                with _lock:
                    scan['items'].append(item)
        with _lock:
            scan['status'] = 'cancelled' if stop.is_set() else 'completed'
    except Exception:
        with _lock:
            scan['status'] = 'failed'
            scan['errors'].append('扫描失败，请检查数据库连接与服务器文件权限')
    finally:
        with _lock:
            scan['current'] = ''


def get(scan_id, offset=0, cancel=False):
    with _lock:
        scan = _scans.get(scan_id)
        if scan is None:
            raise ManagementError('not_found', '扫描结果已过期或服务已重启，请重新扫描')
        if cancel:
            scan['stop'].set()
        return _snapshot(scan, offset)
