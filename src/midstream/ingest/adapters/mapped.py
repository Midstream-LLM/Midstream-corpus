"""声明式字段映射；模型只生成配置，不生成可执行代码。"""
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from .base import ParsedRecord


TARGETS = {'title', 'authors', 'composed_label', 'category', 'work_title'}
CONSTANTS = {'license', 'composed_label', 'category', 'unit_type', 'work_title'}
HEAD_CHARS = 4000


class MappingPlan(BaseModel):
    """字段值是记录内的路径：嵌套用点分隔，`a|b` 取第一个非空项，`_parent.x` 取上层记录的字段。"""
    model_config = ConfigDict(extra='forbid')
    format: Literal['txt', 'json', 'jsonl', 'parquet', 'lines']
    encoding: Literal['utf-8-sig', 'utf-16', 'gb18030'] = 'utf-8-sig'
    text_field: str = ''
    id_field: str = ''
    fields: dict[str, str] = Field(default_factory=dict)
    # 以下各项默认关闭；只有非默认值计入解析器版本。
    constants: dict[str, str] = Field(default_factory=dict)   # 整批相同、经人工确认的值
    path_pattern: str = ''                # 对文件路径搜索的正则，命名组即字段名；year 组写入年份
    head_patterns: list[str] = Field(default_factory=list)    # 对正文开头搜索的正则，命名组同上
    trim_start: str = ''                  # 正文从第一次匹配之后开始
    trim_end: str = ''                    # 正文到其后第一次匹配之前结束

    def checked(self):
        if set(self.fields) - TARGETS:
            raise ValueError('只允许映射标题、作者、时代标签、分类和所属作品')
        if set(self.constants) - CONSTANTS:
            raise ValueError('常量只允许许可证、时代标签、分类、单元类型和所属作品')
        if self.format not in ('txt', 'lines') and not self.text_field:
            raise ValueError('结构化数据必须指定正文字段')
        try:
            for pattern in (self.path_pattern, self.trim_start, self.trim_end, *self.head_patterns):
                re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'正则无效：{exc}') from exc
        return self

    def identity(self):
        data = self.model_dump()
        for key in ('constants', 'path_pattern', 'head_patterns', 'trim_start', 'trim_end'):
            if not data[key]:
                del data[key]
        return data


def value(record, key):
    for path in key.split('|'):
        current = record
        for part in path.split('.'):
            current = current.get(part) if isinstance(current, dict) else None
        if current not in (None, '', []):
            return current
    return None


def leaves(record, text_field, trail=()):
    """正文字段是对象数组时逐层展开；子记录通过 `_parent` 引用上层字段。"""
    body = value(record, text_field) if isinstance(record, dict) and text_field else None
    if isinstance(body, list) and body and all(isinstance(v, dict) for v in body):
        parent = {k: v for k, v in record.items() if v is not body}
        for index, child in enumerate(body, 1):
            yield from leaves({**child, '_parent': parent}, text_field, trail + (index,))
    else:
        yield trail, record


def matched(pattern, text):
    """命名组的值；year/year_end 转成年份区间，未知组名归入 extra。"""
    found = re.search(pattern, text, re.M) if pattern else None
    meta, extra = {}, {}
    for name, item in (found.groupdict() if found else {}).items():
        if not item:
            continue
        item = item.strip()
        if name == 'authors':
            meta[name] = [item]
        elif name in TARGETS:
            meta[name] = item
        elif name not in ('year', 'year_end'):
            extra[name] = item
    if found and found.groupdict().get('year'):
        year = int(found['year'])
        meta['composed_years'] = [year, int(found.groupdict().get('year_end') or year)]
    return meta, extra


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
        digest = hashlib.sha256(json.dumps(self.plan.identity(), sort_keys=True).encode()).hexdigest()[:16]
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
        return [parsed for path, index, top in chunk
                for trail, record in leaves(top, self.plan.text_field)
                for parsed in [self._parse(path, '.'.join(map(str, (index, *trail))), record)]]

    def _parse(self, path, index, record):
        plan, plain = self.plan, self.plan.format in ('txt', 'lines')
        # 文件路径命名空间避免不同分片的行号或局部 ID 冲突。
        key = value(record, plan.id_field) if plan.id_field and isinstance(record, dict) else index
        if key is None or isinstance(key, (dict, list, bool)):
            key = f'invalid-id:{index}'
        key = json.dumps([path, str(key)], ensure_ascii=False)
        locator = {'path': path, 'record': index}
        if not isinstance(record, dict):
            return ParsedRecord(key, locator, {}, error='记录不是 JSON 对象')
        text = record['text'] if plain else value(record, plan.text_field)
        if isinstance(text, list) and all(isinstance(v, str) for v in text):
            text = '\n'.join(text)
        if not isinstance(text, str):
            text = None
        meta = {k: 'import_plan' for k in plan.constants}
        basis, extra = dict(meta), {}
        meta.update(plan.constants)
        if plan.format == 'txt':
            meta['title'], basis['title'] = Path(path).stem, 'path'
        for label, patterns, subject in (('path', [plan.path_pattern], Path(path).as_posix()),
                                         ('text', plan.head_patterns, (text or '')[:HEAD_CHARS])):
            for pattern in patterns:
                found, more = matched(pattern, subject)
                meta.update(found)
                basis.update(dict.fromkeys(found, label))
                extra.update(more)
        for target, source in plan.fields.items():
            item = value(record, source)
            if item is not None:
                meta[target], basis[target] = item, 'source'
        if isinstance(meta.get('authors'), str):
            meta['authors'] = [meta['authors']]
        if any(not isinstance(v, str) for k, v in meta.items() if k not in ('authors', 'composed_years')) or (
                'authors' in meta and (not isinstance(meta['authors'], list)
                                       or any(not isinstance(a, str) for a in meta['authors']))):
            return ParsedRecord(key, locator, record, error='元数据字段类型不符')
        if text and plan.trim_start and (found := re.search(plan.trim_start, text)):
            text = text[found.end():]
        if text and plan.trim_end and (found := re.search(plan.trim_end, text)):
            text = text[:found.start()]
        if 'work_title' in meta:
            meta.setdefault('work_relation', 'part')
        if extra:
            meta['extra'] = extra
        meta['field_basis'] = basis
        raw_fields = json.loads(json.dumps(record, default=str))
        body_key = 'text' if plain else plan.text_field
        # 不把正文重复放进未压缩的元数据。嵌套原字段按原样保留。
        for name in body_key.split('|'):
            if '.' not in name:
                raw_fields.pop(name, None)
        return ParsedRecord(key, locator, raw_fields, text=text,
                            error=None if text else '正文为空或类型不符', metadata=meta)

    def parse(self, path):
        for chunk in self.iter_chunks(path):
            yield from self.parse_chunk(chunk)
