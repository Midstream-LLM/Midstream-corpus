"""声明式字段映射；模型只生成配置，不生成可执行代码。"""
import hashlib
import json
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from .base import ParsedRecord


class MappingPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['txt', 'json', 'jsonl', 'parquet', 'lines']
    encoding: Literal['utf-8-sig', 'utf-16', 'gb18030'] = 'utf-8-sig'
    text_field: str = ''
    id_field: str = ''
    fields: dict[str, str] = Field(default_factory=dict)

    def checked(self):
        allowed = {'title', 'authors', 'composed_label', 'category'}
        if set(self.fields) - allowed:
            raise ValueError('只允许映射标题、作者、时代标签和分类；许可证需另行核实')
        if self.format not in ('txt', 'lines') and not self.text_field:
            raise ValueError('结构化数据必须指定正文字段')
        return self


def value(record, key):
    current = record
    for part in key.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def records(path, plan):
    if plan.format == 'parquet':
        for batch in pq.ParquetFile(path).iter_batches(batch_size=128):
            yield from batch.to_pylist()
    elif plan.format == 'json':
        # 当前 JSON 数组读取有明确上限；较大文件应转换为 JSONL。
        if path.stat().st_size > 64 << 20:
            raise ValueError('JSON 文件超过 64 MiB，请转换成 JSONL')
        with path.open(encoding=plan.encoding) as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            yield data
        elif isinstance(data, list):
            yield from data
        else:
            raise ValueError('JSON 须为对象或对象数组')
    elif plan.format in ('jsonl', 'lines'):
        with path.open(encoding=plan.encoding) as handle:
            for line in handle:
                if not line.strip():
                    continue
                if plan.format == 'lines':
                    yield {'text': line.rstrip('\r\n')}
                else:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        yield None
    else:
        yield {'text': path.read_text(encoding=plan.encoding)}


class MappedAdapter:
    description = '经确认的声明式导入方案'
    license_note = 'needs-review'

    def __init__(self, name, plan):
        self.name = name
        self.plan = MappingPlan.model_validate(plan).checked()
        digest = hashlib.sha256(json.dumps(self.plan.model_dump(), sort_keys=True).encode()).hexdigest()[:16]
        self.parser_version = 'mapped-v1-' + digest

    def default_paths(self):
        return []

    def iter_chunks(self, path):
        chunk = []
        for index, record in enumerate(records(path, self.plan), 1):
            chunk.append((str(path), index, record))
            if len(chunk) == 128:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    def parse_chunk(self, chunk):
        result = []
        for path, index, record in chunk:
            # 文件路径命名空间避免不同分片的行号或局部 ID 冲突。
            key = value(record, self.plan.id_field) if self.plan.id_field else index
            if key is None or isinstance(key, (dict, list, bool)):
                key = f'invalid-id:{index}'
            key = json.dumps([path, str(key)], ensure_ascii=False)
            locator = {'path': path, 'record': index}
            if not isinstance(record, dict):
                result.append(ParsedRecord(key, locator, {}, error='记录不是 JSON 对象'))
                continue
            text = value(record, self.plan.text_field) if self.plan.format not in ('txt', 'lines') else record['text']
            if isinstance(text, list) and all(isinstance(v, str) for v in text):
                text = '\n'.join(text)
            meta = {k: value(record, v) for k, v in self.plan.fields.items()}
            if isinstance(meta.get('authors'), str):
                meta['authors'] = [meta['authors']]
            if self.plan.format == 'txt':
                meta.setdefault('title', Path(path).stem)
            if any(v is not None and not isinstance(v, str) for k,v in meta.items() if k != 'authors') or (
                meta.get('authors') is not None and (not isinstance(meta['authors'], list) or
                any(not isinstance(a, str) for a in meta['authors']))):
                result.append(ParsedRecord(key, locator, record, error='元数据字段类型不符'))
                continue
            record = json.loads(json.dumps(record, default=str))
            meta = json.loads(json.dumps(meta, default=str))
            meta['field_basis'] = {k: 'source' for k,v in meta.items() if v is not None}
            raw_fields = dict(record)
            body_key = self.plan.text_field if self.plan.format not in ('txt', 'lines') else 'text'
            # 不把正文重复放进未压缩的元数据。嵌套原字段按原样保留。
            if '.' not in body_key:
                raw_fields.pop(body_key, None)
            result.append(ParsedRecord(key, locator, raw_fields, text=text if isinstance(text, str) else None,
                error=None if isinstance(text, str) and text else '正文为空或类型不符', metadata=meta))
        return result

    def parse(self, path):
        for chunk in self.iter_chunks(path):
            yield from self.parse_chunk(chunk)
