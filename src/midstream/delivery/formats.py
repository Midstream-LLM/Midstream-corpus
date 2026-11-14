"""文本格式的统一接口和注册表。"""

import hashlib
import io
import json
import pickle
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

from .samples import SAMPLE_FIELDS


class Format(Protocol):
    suffix: str

    def write(self, stream, rows, options) -> None: ...

    def count(self, path) -> int: ...


class _HashWriter(io.RawIOBase):
    """只在最终字节写出时计算一次 sha256，不重新打开产物计算。"""

    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def writable(self):
        return True

    def tell(self):
        return self.size

    def write(self, data):
        size = self.handle.write(data)
        self.digest.update(memoryview(data)[:size])
        self.size += size
        return size

    def flush(self):
        if not self.handle.closed:
            self.handle.flush()


def _schema(fields):
    integers = {'ordinal', 'version_id', 'doc_id', *SAMPLE_FIELDS}
    return pa.schema([(name, pa.int64() if name in integers else
                       pa.list_(pa.string()) if name == 'authors' else pa.string()) for name in fields])


class Parquet:
    suffix = 'parquet'

    def write(self, stream, rows, options):
        pq.write_table(pa.Table.from_pylist(rows, schema=_schema(rows[0].keys())), stream,
                       compression='zstd')

    def count(self, path):
        return pq.ParquetFile(path).metadata.num_rows


class Pickle:
    suffix = 'pkl'

    def write(self, stream, rows, options):
        pickle.dump(rows, stream, protocol=5)

    def count(self, path):
        with path.open('rb') as handle:
            return len(pickle.load(handle))


class Jsonl:
    suffix = 'jsonl'

    def write(self, stream, rows, options):
        def write_json(output):
            for row in rows:
                output.write((json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
        if options.compression:
            with zstandard.ZstdCompressor(level=9).stream_writer(stream, closefd=False) as compressed:
                write_json(compressed)
        else:
            write_json(stream)

    def count(self, path):
        with path.open('rb') as handle:
            if path.suffix == '.zst':
                with zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                    return sum(1 for _ in io.TextIOWrapper(reader, encoding='utf-8'))
            return sum(1 for _ in handle)


FORMATS: dict[str, Format] = {'jsonl': Jsonl(), 'parquet': Parquet(), 'pkl': Pickle()}
