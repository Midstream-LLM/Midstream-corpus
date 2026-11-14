"""登记前的首、中、尾抽样；只解析选中的记录。"""
import json
import codecs
from collections import deque
from dataclasses import replace
from itertools import islice
from pathlib import Path

import pyarrow.parquet as pq

from .adapters.base import JsonlAdapter, JsonlChunk


def _codec(path, encoding):
    if encoding.lower().replace('-', '') == 'utf16':
        with path.open('rb') as handle:
            return 'utf-16-le' if handle.read(2) == b'\xff\xfe' else 'utf-16-be'
    return encoding


def _line_windows(path, encoding='utf-8'):
    size = path.stat().st_size
    with path.open('rb') as handle:
        utf16 = encoding.startswith('utf-16-')
        def readline():
            data = bytearray()
            while block := handle.readline():
                data.extend(block)
                if utf16 and len(data) % 2:
                    data.extend(handle.read(1))
                if not utf16 or data.endswith('\n'.encode(encoding)):
                    break
            return bytes(data)
        for position, start in (('start', 0), ('middle', size // 2), ('end', max(0, size - 65536))):
            if utf16:
                start = max(2, start // 2 * 2)
            handle.seek(start)
            if start > (2 if utf16 else 0):
                readline()
            if position == 'end':
                tail = deque(maxlen=3)
                while line := readline():
                    tail.append((handle.tell() - len(line), line))
                for offset, data in tail:
                    yield position, offset, data
            else:
                for _ in range(3):
                    offset = handle.tell()
                    data = readline()
                    if data:
                        yield position, offset, data


def _json_values(path, encoding):
    """扫描顶层数组边界，保留首/中/尾原始片段；不解析其余记录。"""
    first, middle, tail = [], [], deque(maxlen=3)
    size, offset, index = path.stat().st_size, 0, 0
    if size > 64 << 20:
        raise ValueError('JSON 文件超过 64 MiB，请转换成 JSONL')
    with path.open(encoding=encoding) as handle:
        char = handle.read(1)
        while char and char.isspace():
            char = handle.read(1)
        if char != '[':
            raw = char + handle.read()
            return [(1, json.loads(raw), 'document')]
        depth, quoted, escaped, part = 0, False, False, []
        done = False
        while not done and (block := handle.read(65536)):
            for char in block:
                if not quoted and depth == 0 and char in ',]':
                    raw = ''.join(part)
                    if raw.strip():
                        index += 1
                        item = (index, raw)
                        if len(first) < 3:
                            first.append(item)
                        if offset >= size // 2 and len(middle) < 3:
                            middle.append(item)
                        tail.append(item)
                    offset += len(raw.encode(encoding)) + 1
                    part = []
                    if char == ']':
                        done = True
                        break
                else:
                    part.append(char)
                    if quoted:
                        if escaped:
                            escaped = False
                        elif char == '\\':
                            escaped = True
                        elif char == '"':
                            quoted = False
                    elif char == '"':
                        quoted = True
                    elif char in '[{':
                        depth += 1
                    elif char in ']}':
                        depth -= 1
    selected = {i: (raw, position) for position, items in (('start', first), ('middle', middle), ('end', tail))
                for i, raw in items}
    return [(i, json.loads(raw), position) for i, (raw, position) in sorted(selected.items())]


def _fragment(path, offset, encoding):
    with path.open('rb') as handle:
        utf16 = encoding.lower().replace('-', '') == 'utf16'
        if utf16:
            encoding = 'utf-16-le' if handle.read(2) == b'\xff\xfe' else 'utf-16-be'
            offset = max(2, offset // 2 * 2)
        handle.seek(offset)
        data = handle.read(8192)
    for skip in ((0, 2) if utf16 else range(4)) if offset else (0,):
        try:
            return codecs.getincrementaldecoder(encoding)().decode(data[skip:], final=False)
        except UnicodeDecodeError as exc:
            if exc.start:
                raise
    raise ValueError('抽样窗口不能按指定编码读取')


def sample(adapter, path: Path):
    if isinstance(adapter, JsonlAdapter):
        seen = set()
        for position, offset, data in _line_windows(path):
            if offset in seen:
                continue
            seen.add(offset)
            for record in adapter.parse_chunk(JsonlChunk(data, 1, offset)):
                yield position, replace(record, locator={'offset': offset})
        return
    plan = adapter.plan
    if plan.format == 'parquet':
        file = pq.ParquetFile(path)
        groups = sorted({0, file.num_row_groups // 2, file.num_row_groups - 1} - {-1})
        preceding = [0]
        for i in range(file.num_row_groups):
            preceding.append(preceding[-1] + file.metadata.row_group(i).num_rows)
        for group in groups:
            for batch in islice(file.iter_batches(row_groups=[group], batch_size=3), 1):
                for i, raw in enumerate(batch.to_pylist(), preceding[group] + 1):
                    yield 'row_group:' + str(group), adapter.parse_chunk([(str(path), i, raw)])[0]
    elif plan.format == 'json':
        for index, raw, position in _json_values(path, plan.encoding):
            yield position, adapter.parse_chunk([(str(path), index, raw)])[0]
    elif plan.format in ('jsonl', 'lines'):
        seen = set()
        encoding = _codec(path, plan.encoding)
        for position, offset, data in _line_windows(path, encoding):
            if offset in seen:
                continue
            seen.add(offset)
            text = data.decode(encoding)
            try:
                raw = {'text': text.rstrip('\r\n')} if plan.format == 'lines' else json.loads(text)
            except ValueError:
                raw = None
            record = adapter.parse_chunk([(str(path), offset, raw)])[0]
            yield position, replace(record, source_key=record.source_key if plan.id_field else '',
                                    locator={'path': str(path), 'offset': offset})
    else:
        # TXT 是一份文档，返回原文三个片段。
        for position, offset in (('start', 0), ('middle', path.stat().st_size // 2), ('end', max(0, path.stat().st_size - 8192))):
            raw = _fragment(path, offset, plan.encoding)
            record = adapter.parse_chunk([(str(path), 1, {'text': raw})])[0]
            yield position, replace(record, locator={'path': str(path), 'offset': offset})
