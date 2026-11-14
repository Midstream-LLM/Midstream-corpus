"""token 分片（NPY）：需要分词器并写索引文件，不套 Format，产物登记在同一张表里。"""

import json
import os
import shutil
from contextlib import ExitStack

import numpy as np
from psycopg.types.json import Jsonb

from .. import config
from ..store import textcodec
from ..datasets.release import get_release
from .formats import _HashWriter


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
                        from ..datasets.cuts import apply
                        yield record, apply(textcodec.decompress(bytes(content)),record.pop('_cuts') or [])

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
