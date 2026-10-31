"""有限样本识别；已知来源走规则，其他来源可请求模型建议。"""
import json
from pathlib import Path

import pyarrow.parquet as pq

from .adapters.mapped import MappingPlan
from .management import ManagementError
from .llm.services import LLMServices


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
                pass
        return {'format': fmt, 'encoding': encoding, 'record': record,
                'sample': text[:6000]}
    # Parquet 字段保留，样本限制长度；不发送路径或完整文件。
    return {'format': fmt, 'encoding': encoding, 'record': record,
            'sample': json.dumps(record, ensure_ascii=False, default=str)[:6000]}


def detect_one(path):
    data = sample(path)
    fmt, record = data['format'], data['record']
    fields = set(record) if isinstance(record, dict) else set()
    parts = set(path.parts)
    source, adapter, plan, note = None, None, None, ''
    if fmt == 'jsonl' and {'id', 'raw_text', 'wanli_year'} <= fields:
        source = adapter = 'wanli'
    elif fmt == 'jsonl' and {'id', 'content', 'source', 'chapter'} <= fields:
        source = adapter = 'ccc_corpus'
    else:
        profiles = [
            ('ming-qing-wenji-corpus', 'mingqing', 'text'),
            ('wikisource-zh', 'wikisource', 'text'),
            ('greathangpt-classical-chinese', 'greathangpt', 'text'),
            ('chinese-poetry', 'chinese_poetry', 'paragraphs'),
            ('daizhigev20', 'daizhige', ''),
            ('new-dataset', 'team_texts', ''),
            ('gutenberg-zh', 'gutenberg', ''),
            ('erya-dataset', 'erya', ''),
        ]
        for directory, name, text_field in profiles:
            if directory not in parts:
                continue
            if fmt in ('txt', 'lines') or text_field in fields:
                source = name
                if name == 'chinese_poetry' and text_field not in fields:
                    continue
                selected = text_field if fmt not in ('txt', 'lines') else ''
                mapping = {k: v for k,v in {'title':'title', 'authors':'author',
                          'composed_label':'era', 'category':'category'}.items() if v in fields}
                plan = MappingPlan(format=fmt, encoding=data['encoding'], text_field=selected,
                                   id_field='id' if 'id' in fields else '', fields=mapping).model_dump()
                if name == 'erya':
                    note = '逐行保留原文；src/tgt 配对关系尚不支持，请勿将其当作已关联的翻译样本。'
                if name == 'gutenberg':
                    note = 'TXT 可导入；书目清单关联待补，作者等字段保持未知。'
                break
    return {'path': str(path), 'known': source is not None, 'source': source,
            'adapter': adapter, 'plan': plan, 'format': fmt, 'note': note,
            '_sample': data}


def detect(names, use_llm=False, profile=None):
    results = []
    for name in names:
        path = Path(name)
        if not path.is_absolute() or not path.is_file():
            raise ManagementError('invalid_input', '识别需要后端机器上的普通文件绝对路径')
        try:
            result = detect_one(path.resolve())
            data = result.pop('_sample')
            if not result['known'] and use_llm and data['format'] != 'unsupported':
                with LLMServices.from_config() as service:
                    suggestion = service.identify_import({k: data[k] for k in ('format', 'encoding', 'sample')}, profile=profile)
                result.update(suggestion)
                if result['plan'] is not None:
                    result['plan'] = MappingPlan.model_validate(result['plan']).checked().model_dump()
            results.append(result)
        except (OSError, ValueError, TypeError) as exc:
            raise ManagementError('invalid_input', '无法识别文件或识别结果无效，请检查格式和字段') from exc
    return {'items': results}
