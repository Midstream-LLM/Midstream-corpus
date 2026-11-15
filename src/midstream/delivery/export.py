"""固定发布的流式多格式导出：分片、写出、登记产物、校验文件；每个分片有独立的内存上限。"""

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Literal

import pyarrow as pa
import numpy as np
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import config
from ..store import textcodec
from ..datasets.recipe import ExportError
from ..datasets.release import get_release, verify_members
from .formats import FORMATS, _HashWriter
from .samples import SAMPLE_FIELDS, SAMPLERS

DEFAULT_FIELDS = ['ordinal', 'version_id', 'doc_id', 'split', 'title', 'text']
FieldName = Literal['ordinal', 'version_id', 'doc_id', 'split', 'title', 'text',
                    'authors', 'era', 'category', 'genre', 'source', 'composed_years', 'edition_years',
                    'processed_steps', 'contributors', 'tags', 'tune_pattern', 'topics', 'external_ids', 'field_basis']
CURATION_FIELDS = ['composed_years', 'edition_years', 'processed_steps', 'contributors', 'tags',
                   'tune_pattern', 'topics', 'external_ids', 'field_basis']


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


def _documents(conn, release_id, *, only_cut=False):
    with conn.cursor(name='export_documents') as cursor:
        cursor.itersize = 16
        cursor.execute(
            'SELECT m.ordinal, m.version_id, v.doc_id, m.split, v.title, b.content,'
            ' v.authors, v.composed_label, v.category, v.genre, s.name,'
            " jsonb_build_object('composed_years',v.composed_years,'edition_years',v.edition_years,"
            " 'processed_steps',v.processed_steps,'contributors',v.contributors,'tags',v.tags,"
            " 'tune_pattern',v.tune_pattern,'topics',v.topics,'external_ids',v.external_ids,'field_basis',v.field_basis),"
            ' (SELECT jsonb_agg(jsonb_build_array(start_char,end_char) ORDER BY start_char)'
            ' FROM dataset_member_cut c WHERE c.release_id=m.release_id AND c.version_id=m.version_id) cuts'
            ' FROM dataset_member m JOIN document_version v USING (version_id)'
            ' JOIN text_blob b USING (text_hash) JOIN document d USING (doc_id)'
            ' JOIN source s USING (source_id) WHERE m.release_id = %s' +
            (' AND EXISTS(SELECT 1 FROM dataset_member_cut c WHERE c.release_id=m.release_id AND c.version_id=m.version_id)' if only_cut else '') +
            ' ORDER BY m.split, m.ordinal',
            (release_id,))
        keys = DEFAULT_FIELDS + ['authors', 'era', 'category', 'genre', 'source', '_curation', '_cuts']
        for row in cursor:
            document = dict(zip(keys, row))
            from ..corpus.metadata import public_metadata
            document.update(public_metadata(document.pop('_curation')))
            from ..datasets.cuts import apply
            document['text'] = apply(textcodec.decompress(bytes(document['text'])),document.pop('_cuts') or [])
            yield document


def _rows(conn, release_id, options):
    fields = list(options.fields) + (SAMPLE_FIELDS if options.unit == 'sample' else [])
    for document in _documents(conn, release_id):
        records = SAMPLERS[options.unit](document, options)
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


def _write_shard(path, rows, options):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as handle:
        writer = _HashWriter(handle)
        FORMATS[options.format].write(writer, rows, options)
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
            ext = FORMATS[options.format].suffix + ('.zst' if options.compression else '')
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


def _row_count(path, format):
    if format == 'npy':
        return int(np.load(path, mmap_mode='r', allow_pickle=False).size)
    if format == 'json':
        with path.open(encoding='utf-8') as handle:
            json.load(handle)
        return 1
    return FORMATS[format].count(path)


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


def verify_release(conn, release_id, *, hash_files=False) -> bool:
    """核对固定成员，再核对产物存在、大小、行数；仅显式要求时重算文件哈希。"""
    return verify_members(conn, release_id) and verify_files(conn, release_id, hash_files=hash_files)


def write_parquet(conn, release_id, out_dir: Path) -> list[Path]:
    """兼容库入口；每次导出仍使用独立 export_id 目录。"""
    result = export_release(conn, release_id, directory=out_dir)
    return [Path(name) for name in result['files']]
