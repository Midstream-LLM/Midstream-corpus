"""有限样本识别。

顺序：内置解析器 → 已确认过的方案 → 内置来源方案 → 规则推断 → 模型建议。
前三级算已识别；后两级只是草稿，须人工确认。确认后的方案随批次保存，下次自动复用。
"""
import json
import re
from pathlib import Path

import pyarrow.parquet as pq

from .adapters.mapped import MappingPlan
from .profiles import PROFILES, SKIP, SYNONYMS
from ..corpus.eras import ERA_YEARS
from ..corpus.management import ManagementError
from ..llm.services import LLMServices


def sample(path):
    with path.open('rb') as handle:
        head = handle.read(32768)
    if head.startswith(b'PAR1'):
        fmt, encoding = 'parquet', 'utf-8-sig'
        batch = next(pq.ParquetFile(path).iter_batches(batch_size=1), None)
        record = batch.to_pylist()[0] if batch is not None else {}
    elif (head.startswith((b'%PDF', b'PK\x03\x04', b'\x1f\x8b', b'\x89PNG', b'\xff\xd8\xff'))
          or (b'\x00' in head and not head.startswith((b'\xff\xfe', b'\xfe\xff')))) or path.suffix.lower() in ('.pdf', '.zip', '.tgz', '.gz', '.bin'):
        return {'format': 'unsupported', 'encoding': None, 'record': {},
                'sample': '需先提取正文；不向模型发送二进制文件'}
    else:
        encoding = 'utf-16' if head.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        try:
            text = head.decode(encoding, errors='strict')
        except UnicodeDecodeError:
            # 截断的 UTF-8 尾部不能被误判为 GB 编码。
            try:
                text = head.decode(encoding, errors='ignore')
                if len(text.encode(encoding)) < len(head) - 8:
                    raise ValueError()
            except ValueError:
                encoding = 'gb18030'
                text = head.decode(encoding, errors='replace')
        fmt = {'json': 'json', 'jsonl': 'jsonl', 'src': 'lines', 'tgt': 'lines'}.get(path.suffix.lower().lstrip('.'), 'txt')
        record = {}
        if fmt in ('json', 'jsonl'):
            try:
                start = text.lstrip()
                if fmt == 'json' and start.startswith('['):
                    start = start[1:].lstrip()
                record, _ = json.JSONDecoder().raw_decode(start)
            except ValueError:
                # 第一条记录超过取样窗口（如整份文件是一个对象）时读全文件。
                if fmt == 'json' and path.stat().st_size <= 64 << 20:
                    with path.open(encoding=encoding) as handle:
                        whole = json.load(handle)
                    record = whole[0] if isinstance(whole, list) and whole else whole
        return {'format': fmt, 'encoding': encoding, 'record': record,
                'sample': text[:6000]}
    # Parquet 字段保留，样本限制长度；不发送路径或完整文件。
    return {'format': fmt, 'encoding': encoding, 'record': record,
            'sample': json.dumps(record, ensure_ascii=False, default=str)[:6000]}


def skipped(path):
    name = path.as_posix()
    return next((note for pattern, note in SKIP if re.search(pattern, name)), None)


def learned_plans(conn):
    """各来源最近一次经人工确认的方案，以及当时文件所在的目录。"""
    return conn.execute(
        """SELECT DISTINCT ON (s.name) s.name, b.batch_id, b.import_plan,
                  (SELECT array_agg(DISTINCT regexp_replace(o.path, '/[^/]*$', ''))
                   FROM raw_object o WHERE o.batch_id = b.batch_id)
           FROM source_batch b JOIN source s USING (source_id)
           WHERE b.import_plan IS NOT NULL AND b.status <> 'abandoned'
           ORDER BY s.name, b.batch_id DESC""").fetchall()


def _fits(plan, path, fmt, encoding, fields, directories=()):
    if plan['format'] != fmt or plan['encoding'] != encoding:
        return False
    pattern = plan.get('path_pattern')
    if pattern and not re.search(pattern, path.as_posix()):
        return False
    if fmt in ('txt', 'lines'):
        return bool(pattern) or str(path.parent) in directories
    used = [plan['text_field'], plan['id_field'], *plan['fields'].values()]
    return all(any(option.split('.')[0] in fields | {'_parent'} for option in item.split('|')) for item in used if item)


def infer(path, fmt, encoding, record):
    """按字段名和取值给未知来源起草方案；不确定的字段留空。"""
    plan = dict(format=fmt, encoding=encoding)
    if fmt == 'txt':
        parts = path.stem.split('_')
        if len(parts) > 1 and any(part in ERA_YEARS for part in parts):
            era = next(i for i, part in enumerate(parts) if part in ERA_YEARS)
            groups = ['(?P<composed_label>[^_/]+)' if i == era else '(?P<title>[^/]+)' if i == len(parts) - 1
                      else '[^_/]+' for i in range(len(parts))]
            plan['path_pattern'] = '/' + '_'.join(groups) + re.escape(path.suffix) + '$'
        return plan
    if fmt == 'lines' or not isinstance(record, dict):
        return plan if fmt == 'lines' else None
    def size(item):
        return sum(map(len, item)) if isinstance(item, list) else len(item)
    texts = {k: size(v) for k, v in record.items()
             if isinstance(v, str) or (isinstance(v, list) and v and all(isinstance(i, str) for i in v))}
    named = [k for k in SYNONYMS['text'] if k in texts]
    if not texts:
        return None
    plan['text_field'] = named[0] if named else max(texts, key=texts.get)
    plan['id_field'] = next((k for k in SYNONYMS['id'] if k in record), '')
    plan['fields'] = {}
    for target in ('title', 'authors', 'composed_label', 'category', 'work_title'):
        found = next((k for k in SYNONYMS[target] if k in record and k != plan['text_field']
                      and isinstance(record[k], (str, list))), None)
        if found:
            plan['fields'][target] = found
    return plan


def detect_one(path, learned=()):
    note = skipped(path)
    if note:
        return {'path': str(path), 'known': True, 'source': None, 'adapter': None, 'plan': None,
                'format': 'skipped', 'note': note, 'method': 'rule',
                '_sample': {'format': 'skipped', 'encoding': None, 'record': {}, 'sample': ''}}
    data = sample(path)
    fmt, record, encoding = data['format'], data['record'], data['encoding']
    fields = set(record) if isinstance(record, dict) else set()
    name = path.as_posix()
    source, adapter, plan, note, method = None, None, None, '', 'rule'
    if fmt == 'jsonl' and {'id', 'raw_text', 'wanli_year'} <= fields:
        source = adapter = 'wanli'
    elif fmt == 'jsonl' and {'id', 'content', 'source', 'chapter'} <= fields:
        source = adapter = 'ccc_corpus'
    elif fmt != 'unsupported':
        for known, batch_id, saved, directories in learned:
            if _fits(saved, path, fmt, encoding, fields, directories or ()):
                source, plan, method = known, saved, 'learned'
                note = f'沿用批次 #{batch_id} 确认过的方案。'
                break
        for profile in () if source else PROFILES:
            if (re.search(profile['root'], name) and profile.get('require', set()) <= fields
                    and (fmt in ('txt', 'lines') or not profile.get('any') or profile['any'] & fields)
                    and (fmt in ('txt', 'lines')) == ('text_field' not in profile.get('plan', {}))):
                draft = dict(profile.get('plan', {}), format=fmt, encoding=encoding)
                if 'id_field' in draft and draft['id_field'] not in fields:
                    draft['id_field'] = ''
                source, plan, note = profile['source'], draft, profile.get('note', '')
                break
        if source is None:
            plan, method = infer(path, fmt, encoding, record), 'inferred'
            note = '按字段名和文件名推断的草稿，请核对后确认。' if plan else ''
    if plan is not None:
        plan = MappingPlan.model_validate(plan).checked().model_dump()
    return {'path': str(path), 'known': source is not None, 'source': source,
            'adapter': adapter, 'plan': plan, 'format': fmt, 'note': note, 'method': method,
            '_sample': data}


def detect(names, use_llm=False, profile=None, conn=None):
    results = []
    learned = learned_plans(conn) if conn is not None else ()
    for name in names:
        path = Path(name)
        if not path.is_absolute() or not path.is_file():
            raise ManagementError('invalid_input', '识别需要后端机器上的普通文件绝对路径')
        try:
            path = path.resolve()
            result = detect_one(path, learned)
            data = result.pop('_sample')
            if not result['known'] and use_llm and data['format'] != 'unsupported':
                # 只发送文件名、字段和有限样本，不发送目录和完整文件。
                request = {k: data[k] for k in ('format', 'encoding', 'sample')}
                request.update(file_name=path.name, draft=result['plan'])
                with LLMServices.from_config() as service:
                    suggestion = service.identify_import(request, profile=profile)
                result.update(suggestion)
                if result['plan'] is not None:
                    result['plan'] = MappingPlan.model_validate(result['plan']).checked().model_dump()
            results.append(result)
        except (OSError, ValueError, TypeError) as exc:
            raise ManagementError('invalid_input', '无法识别文件或识别结果无效，请检查格式和字段') from exc
    return {'items': results}
