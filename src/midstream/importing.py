"""Web 入库准备：复用适配器，原文件保持原位。"""
from itertools import islice
from pathlib import Path

from . import ingest
from .batches import _event
from .adapters import ADAPTERS
from .management import ManagementError, _identity


def adapters():
    return [{'name': a.name, 'description': a.description,
             'parser_version': a.parser_version, 'license_note': a.license_note}
            for a in ADAPTERS.values()]


def inputs(source, names, plan=None):
    if not source.strip():
        raise ManagementError("invalid_input", "来源名称不能为空")
    if plan is not None:
        if source in ADAPTERS:
            raise ManagementError("invalid_input", "内置来源请使用原适配器；自定义映射须使用独立来源名称")
        from .adapters.mapped import MappedAdapter
        try:
            adapter = MappedAdapter(source, plan)
        except ValueError as exc:
            raise ManagementError("invalid_input", str(exc)) from exc
    else:
        adapter = ADAPTERS.get(source)
    if adapter is None:
        raise ManagementError('invalid_input', '请选择已支持的来源格式')
    paths = []
    try:
        for name in names:
            path = Path(name)
            if not path.is_absolute():
                raise ManagementError('invalid_input', '请使用后端机器上的绝对路径')
            path = path.resolve(strict=True)
            if not path.is_file():
                raise ManagementError('invalid_input', '路径必须指向普通文件')
            if path in paths:
                raise ManagementError('invalid_input', '文件路径重复')
            paths.append(path)
        objects = [{'path': str(p), 'size_bytes': p.stat().st_size,
                    'sha256': ingest._sha256_file(p).hex()} for p in paths]
    except OSError as exc:
        raise ManagementError('invalid_input', '文件不存在或后端无法读取') from exc
    return adapter, paths, objects


def preview(source, names, plan=None):
    adapter, paths, objects = inputs(source, names, plan)
    samples = []
    try:
        for path in paths:
            for record in islice(adapter.parse(path), 3):
                samples.append({'file': str(path), 'source_key': record.source_key,
                                'metadata': record.metadata, 'error': record.error,
                                'text_preview': (record.text or '')[:500]})
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ManagementError('invalid_input', '文件不能按所选来源格式解析') from exc
    return {'source': source, 'parser_version': adapter.parser_version,
            'objects': objects, 'samples': samples}


def register(conn, source, names, expected, parser_version, actor, reason, plan=None):
    _identity(actor, reason)
    adapter, paths, objects = inputs(source, names, plan)
    if objects != expected or adapter.parser_version != parser_version:
        raise ManagementError('input_changed', '文件或解析器已变化，请重新预览')
    try:
        batch_id = ingest.register_batch(conn, adapter, paths, commit=False)
    except ingest.AlreadyImported as exc:
        raise ManagementError('conflict', str(exc)) from exc
    if plan is not None:
        from psycopg.types.json import Jsonb
        conn.execute('UPDATE source_batch SET import_plan = %s WHERE batch_id = %s',
                     (Jsonb(adapter.plan.model_dump()), batch_id))
    _event(conn, 'batch_register', batch_id, actor, reason, 'unregistered', 'received')
    conn.commit()
    return {'batch_id': batch_id, 'status': 'received'}
