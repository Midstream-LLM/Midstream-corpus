"""固定发布的流式多格式导出；每个分片有独立的内存上限。"""

import hashlib
import io
import json
import os
import pickle
import re
import shutil
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard
import numpy as np
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import config, textcodec
from .export import ExportError, get_release

DEFAULT_FIELDS = ['ordinal', 'version_id', 'doc_id', 'split', 'title', 'text']
FieldName = Literal['ordinal', 'version_id', 'doc_id', 'split', 'title', 'text',
                    'authors', 'era', 'category', 'genre', 'source']
SAMPLE_FIELDS = ['sample_index', 'start_char', 'end_char']
SENTENCE_END = re.compile(r'[。！？!?][」』”"]?|\.(?=\s|$)')


class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['parquet', 'jsonl', 'pkl'] = 'parquet'
    compression: Literal['zst'] | None = None
    fields: list[FieldName] = Field(default_factory=lambda: DEFAULT_FIELDS.copy(), min_length=1)
    shard_mb: float = Field(default=256, gt=0, allow_inf_nan=False)
    unit: Literal['document', 'sample'] = 'document'
    max_chars: int = Field(default=8192, gt=0)
    overlap_chars: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid(self):
        if self.overlap_chars >= self.max_chars:
            raise ValueError('overlap_chars 必须小于 max_chars')
        if self.compression and self.format != 'jsonl':
            raise ValueError('外层 zst 压缩仅用于 jsonl')
        if len(set(self.fields)) != len(self.fields):
            raise ValueError('fields 不能重复')
        return self


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


def _documents(conn, release_id):
    with conn.cursor(name='export_documents') as cursor:
        cursor.itersize = 16
        cursor.execute(
            'SELECT m.ordinal, m.version_id, v.doc_id, m.split, v.title, b.content,'
            ' v.authors, v.composed_label, v.category, v.genre, s.name'
            ' FROM dataset_member m JOIN document_version v USING (version_id)'
            ' JOIN text_blob b USING (text_hash) JOIN document d USING (doc_id)'
            ' JOIN source s USING (source_id) WHERE m.release_id = %s ORDER BY m.split, m.ordinal',
            (release_id,))
        keys = DEFAULT_FIELDS + ['authors', 'era', 'category', 'genre', 'source']
        for row in cursor:
            document = dict(zip(keys, row))
            document['text'] = textcodec.decompress(bytes(document['text']))
            yield document


def _samples(document, options):
    text = document['text']
    start, index = 0, 0
    while start < len(text):
        end = min(start + options.max_chars, len(text))
        if end < len(text):
            part = text[start:end]
            boundary = part.rfind('\n\n', options.overlap_chars + 1)
            if boundary >= 0:
                end = start + boundary + 2
            else:
                boundary = part.rfind('\n', options.overlap_chars + 1)
                if boundary >= 0:
                    end = start + boundary + 1
                else:
                    for match in SENTENCE_END.finditer(part, options.overlap_chars + 1):
                        end = start + match.end()
        yield {**document, 'text': text[start:end], 'sample_index': index,
               'start_char': start, 'end_char': end}
        if end == len(text):
            break
        start = end - options.overlap_chars
        index += 1


def _rows(conn, release_id, options):
    fields = list(options.fields) + (SAMPLE_FIELDS if options.unit == 'sample' else [])
    for document in _documents(conn, release_id):
        records = _samples(document, options) if options.unit == 'sample' else (document,)
        for record in records:
            yield document['split'], {name: record[name] for name in fields}


def _shards(rows, max_bytes):
    split, buffer, size = None, [], 0
    for next_split, row in rows:
        # UTF-8 JSON 大小作为统一分片预算；单篇/单个 sample 超预算时单独成片。
        row_size = len(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + 1
        if buffer and (next_split != split or size + row_size > max_bytes):
            yield split, buffer
            buffer, size = [], 0
        split = next_split
        buffer.append(row)
        size += row_size
    if buffer:
        yield split, buffer


def _schema(fields):
    integers = {'ordinal', 'version_id', 'doc_id', *SAMPLE_FIELDS}
    return pa.schema([(name, pa.int64() if name in integers else
                       pa.list_(pa.string()) if name == 'authors' else pa.string()) for name in fields])


def _write_shard(path, rows, options):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as handle:
        writer = _HashWriter(handle)
        if options.format == 'parquet':
            pq.write_table(pa.Table.from_pylist(rows, schema=_schema(rows[0].keys())), writer,
                           compression='zstd')
        elif options.format == 'pkl':
            pickle.dump(rows, writer, protocol=5)
        else:
            def write_json(output):
                for row in rows:
                    output.write((json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
            if options.compression:
                with zstandard.ZstdCompressor(level=9).stream_writer(writer, closefd=False) as compressed:
                    write_json(compressed)
            else:
                write_json(writer)
        writer.flush()
        digest, size = writer.digest.hexdigest(), writer.size
    os.replace(temporary, path)
    return {'file': path.name, 'rows': len(rows), 'bytes': size, 'sha256': digest}


def export_release(conn, release_id, options=None, *, directory=None, progress=None):
    options = options or Options()
    release = get_release(conn, release_id)
    settings = options.model_dump()
    export_id = conn.execute(
        'INSERT INTO dataset_export (release_id, format, options) VALUES (%s, %s, %s) RETURNING export_id',
        (release_id, options.format, Jsonb(settings))).fetchone()[0]
    conn.commit()
    base = Path(directory) if directory is not None else config.EXPORT_ROOT / release['name']
    target = base / str(export_id)
    temporary = base / f'.{export_id}.tmp'
    files, counts, current = [], {}, 0
    total = sum(item['documents'] for item in release['splits'].values())
    try:
        temporary.mkdir(parents=True)
        for split, rows in _shards(_rows(conn, release_id, options), int(options.shard_mb * 1024 ** 2)):
            if progress:
                progress(current, total if options.unit == 'document' else 0, '写出导出分片')
            index = counts.get(split, 0)
            counts[split] = index + 1
            ext = options.format + ('.zst' if options.compression else '')
            file = _write_shard(temporary / f'{split}-{index:05d}.{ext}', rows, options)
            files.append({**file, 'split': split})
            current += len(rows)
        if progress:
            progress(current, current, '保存 manifest 与导出记录')
        manifest = {'export_id': export_id, 'release_id': release_id, 'recipe': release['recipe'],
                    'content_digest': release['content_digest'], **settings,
                    'fields': list(options.fields) + (SAMPLE_FIELDS if options.unit == 'sample' else []),
                    'files': files}
        manifest_tmp = temporary / 'manifest.json.tmp'
        manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(manifest_tmp, temporary / 'manifest.json')
        os.replace(temporary, target)
        # 文件与 manifest 全部完成后才登记；writing/failed 不会出现在完成产物中。
        for file in files:
            conn.execute(
                'INSERT INTO export_artifact (release_id, export_id, path, format, sha256, split,'
                ' row_count, size_bytes, tool_info) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (release_id, export_id, str(target / file['file']), options.format,
                 bytes.fromhex(file['sha256']), file['split'], file['rows'], file['bytes'],
                 Jsonb({'pyarrow': pa.__version__} if options.format == 'parquet' else {})))
        conn.execute("UPDATE dataset_export SET status = 'completed', manifest_path = %s, finished_at = now()"
                     ' WHERE export_id = %s', (str(target / 'manifest.json'), export_id))
        conn.commit()
        return {'export_id': export_id, 'release_id': release_id, 'manifest': str(target / 'manifest.json'),
                'files': [str(target / file['file']) for file in files]}
    except BaseException as exc:
        conn.rollback()
        if temporary.exists():
            shutil.rmtree(temporary)
        conn.execute("UPDATE dataset_export SET status = 'failed', error = %s, finished_at = now() WHERE export_id = %s",
                     (str(exc), export_id))
        conn.commit()
        raise


def list_exports(conn, release_id):
    return [{**row, 'files': [item[0] for item in conn.execute(
        'SELECT to_jsonb(a) - \'sha256\' || jsonb_build_object(\'sha256\', encode(sha256, \'hex\'))'
        ' FROM export_artifact a WHERE export_id = %s ORDER BY artifact_id', (row['export_id'],))]}
            for row, in conn.execute('SELECT to_jsonb(e) FROM dataset_export e WHERE release_id = %s'
                                     ' ORDER BY export_id DESC', (release_id,)).fetchall()]


def write_tokens(conn, release_id, tokenizer, *, shard_tokens=200_000_000, progress=None):
    """按击水 NPY/文档索引布局写出；调用前已填充正文计数缓存。"""
    from . import token_stats
    release = get_release(conn, release_id)
    dtype = np.dtype('<u2' if tokenizer.vocab_size <= 65536 else '<u4')
    lengths, size = [], 0
    for record in token_stats._members(conn, release_id, tokenizer.fingerprint):
        count = record['tokens']
        if size and size + count > shard_tokens:
            lengths.append(size)
            size = 0
        size += count
    if size:
        lengths.append(size)
    options = {'unit': 'document', 'tokenizer_fingerprint': tokenizer.fingerprint,
               'tokenizer_name': tokenizer.name, 'vocab_size': tokenizer.vocab_size,
               'dtype': dtype.str, 'shard_tokens': shard_tokens, 'add_special_tokens': False,
               'padding': False, 'truncation': False}
    export_id = conn.execute(
        "INSERT INTO dataset_export (release_id,format,options) VALUES (%s,'tokens',%s) RETURNING export_id",
        (release_id, Jsonb(options))).fetchone()[0]
    conn.commit()
    base = config.EXPORT_ROOT / release['name']
    target, temporary = base / str(export_id), base / f'.{export_id}.tmp'
    files, split_counts = [], {split: {'documents': 0, 'tokens': 0} for split in ('train', 'val', 'test')}
    shard_writer, shard, offset, documents = None, -1, 0, 0

    def line(writer, value):
        writer.write((json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))

    def artifact(name, writer, rows, format):
        return {'file': name, 'rows': rows, 'bytes': writer.size,
                'sha256': writer.digest.hexdigest(), 'format': format}

    try:
        for name in ('tokens', 'manifest', 'split'):
            (temporary / name).mkdir(parents=True)
        with ExitStack() as stack:
            def index_writer(name):
                return _HashWriter(stack.enter_context((temporary / name).open('wb')))

            info = index_writer('tokens/shard_info.jsonl')
            docs = index_writer('manifest/docs_tokens.jsonl')
            splits = {split: index_writer(f'split/{split}.jsonl') for split in split_counts}

            def close_shard():
                if shard_writer is None:
                    return
                shard_writer.flush()
                shard_writer.handle.close()
                name = f'tokens/shard_{shard:05d}.npy'
                files.append({**artifact(name, shard_writer, offset, 'npy'), 'tokens': offset})
                line(info, {'shard': shard, 'path': str(target / name), 'tokens': offset})

            def items():
                for record in token_stats._members(conn, release_id, tokenizer.fingerprint, content=True):
                    content = record.pop('content')
                    if record['tokens']:
                        yield record, textcodec.decompress(bytes(content))

            total_docs = sum(value['documents'] for value in release['splits'].values())
            for batch in token_stats._text_batches(items()):
                if progress:
                    progress(documents, total_docs, '编码并写出 token 分片')
                encoded = tokenizer.encode_batch([text for _, text in batch])
                for (record, _), ids in zip(batch, encoded):
                    count = len(ids)
                    if shard_writer is None or (offset and offset + count > shard_tokens):
                        close_shard()
                        shard += 1
                        offset = 0
                        shard_writer = _HashWriter((temporary / f'tokens/shard_{shard:05d}.npy').open('wb'))
                        np.lib.format.write_array_header_1_0(shard_writer, {
                            'descr': dtype.str, 'fortran_order': False, 'shape': (lengths[shard],)})
                    shard_writer.write(np.asarray(ids, dtype=dtype).tobytes())
                    record = {**record, 'tokens': count, 'shard': shard, 'offset': offset}
                    line(docs, record)
                    line(splits[record['split']], record)
                    offset += count
                    documents += 1
                    split_counts[record['split']]['documents'] += 1
                    split_counts[record['split']]['tokens'] += count
            close_shard()
            files.append(artifact('tokens/shard_info.jsonl', info, len(lengths), 'jsonl'))
            files.append(artifact('manifest/docs_tokens.jsonl', docs, documents, 'jsonl'))
            for split, writer in splits.items():
                files.append(artifact(f'split/{split}.jsonl', writer, split_counts[split]['documents'], 'jsonl'))
            split_manifest = index_writer('split/split_manifest.json')
            split_manifest.write((json.dumps({'release_id': release_id, 'seed': release['recipe']['seed'],
                                             'method': 'fixed_dataset_member', 'splits': split_counts},
                                            ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            files.append(artifact('split/split_manifest.json', split_manifest, 1, 'json'))
        if progress:
            progress(documents, documents, '保存 token manifest 与导出记录')
        manifest = {'export_id': export_id, 'release_id': release_id, 'format': 'tokens',
                    'recipe': release['recipe'], 'content_digest': release['content_digest'], **options,
                    'total_tokens': sum(lengths), 'documents': documents,
                    'zero_token_documents': total_docs - documents, 'by_split': split_counts, 'files': files}
        manifest_tmp = temporary / 'manifest.json.tmp'
        manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(manifest_tmp, temporary / 'manifest.json')
        os.replace(temporary, target)
        for file in files:
            conn.execute('INSERT INTO export_artifact (release_id,export_id,path,format,sha256,row_count,size_bytes)'
                         ' VALUES (%s,%s,%s,%s,%s,%s,%s)',
                         (release_id, export_id, str(target / file['file']), file['format'],
                          bytes.fromhex(file['sha256']), file['rows'], file['bytes']))
        conn.execute("UPDATE dataset_export SET status='completed',manifest_path=%s,finished_at=now()"
                     ' WHERE export_id=%s', (str(target / 'manifest.json'), export_id))
        conn.commit()
        return {'release_id': release_id, 'export_id': export_id, 'tokenizer_fingerprint': tokenizer.fingerprint,
                'total_tokens': sum(lengths), 'manifest': str(target / 'manifest.json'),
                'files': [str(target / file['file']) for file in files]}
    except BaseException as exc:
        if shard_writer is not None and not shard_writer.handle.closed:
            shard_writer.handle.close()
        conn.rollback()
        if temporary.exists():
            shutil.rmtree(temporary)
        conn.execute("UPDATE dataset_export SET status='failed',error=%s,finished_at=now() WHERE export_id=%s",
                     (str(exc), export_id))
        conn.commit()
        raise


def _row_count(path, format):
    if format == 'npy':
        return int(np.load(path, mmap_mode='r', allow_pickle=False).size)
    if format == 'json':
        with path.open(encoding='utf-8') as handle:
            json.load(handle)
        return 1
    if format == 'parquet':
        return pq.ParquetFile(path).metadata.num_rows
    if format == 'pkl':
        with path.open('rb') as handle:
            return len(pickle.load(handle))
    with path.open('rb') as handle:
        if path.suffix == '.zst':
            with zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                return sum(1 for _ in io.TextIOWrapper(reader, encoding='utf-8'))
        return sum(1 for _ in handle)


def verify_files(conn, release_id, *, hash_files=False):
    rows = conn.execute(
        'SELECT a.path, a.format, a.sha256, a.row_count, a.size_bytes, a.export_id'
        ' FROM export_artifact a LEFT JOIN dataset_export e USING (export_id)'
        " WHERE a.release_id = %s AND (a.export_id IS NULL OR e.status = 'completed')", (release_id,)).fetchall()
    for name, format, expected_hash, count, size, export_id in rows:
        path = Path(name)
        if not path.is_file() or (size is not None and path.stat().st_size != size):
            return False
        actual = _row_count(path, format)
        if count is not None and actual != count:
            return False
        if export_id is None and path.stem in ('train', 'val', 'test'):
            expected = conn.execute('SELECT count(*) FROM dataset_member WHERE release_id = %s AND split = %s',
                                    (release_id, path.stem)).fetchone()[0]
            if actual != expected:
                return False
        if hash_files:
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(chunk)
            if digest.digest() != bytes(expected_hash):
                return False
    for manifest, in conn.execute("SELECT manifest_path FROM dataset_export WHERE release_id = %s AND status = 'completed'",
                                  (release_id,)):
        if not Path(manifest).is_file():
            return False
    return True
