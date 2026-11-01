"""按固定发布计数；缓存仅依赖正文哈希和分词器 JSON 指纹。"""

import psycopg
from psycopg.types.json import Jsonb

from . import textcodec
from .export import get_release


def _text_batches(items):
    batch, chars = [], 0
    for key, text in items:
        if batch and (len(batch) >= 64 or chars + len(text) > 1 << 20):
            yield batch
            batch, chars = [], 0
        batch.append((key, text))
        chars += len(text)
    if batch:
        yield batch


def _fill_cache(conn, release_id, tokenizer, progress):
    fingerprint = bytes.fromhex(tokenizer.fingerprint)
    computed = 0
    # 执行连接每批提交缓存；独立只读游标不会因此失效。
    with psycopg.connect(conn.info.dsn) as reader:
        reader.execute('SET TRANSACTION READ ONLY')
        with reader.cursor(name='uncounted_texts') as cursor:
            cursor.itersize = 16
            cursor.execute(
                'SELECT b.text_hash, b.content FROM (SELECT DISTINCT v.text_hash'
                ' FROM dataset_member m JOIN document_version v USING (version_id)'
                ' WHERE m.release_id = %s AND NOT EXISTS(SELECT 1 FROM dataset_member_cut cut'
                ' WHERE cut.release_id=m.release_id AND cut.version_id=m.version_id)) selected JOIN text_blob b USING (text_hash)'
                ' LEFT JOIN text_token_count c ON c.text_hash = b.text_hash AND c.tokenizer_fingerprint = %s'
                ' WHERE c.text_hash IS NULL ORDER BY b.text_hash', (release_id, fingerprint))
            items = ((digest, textcodec.decompress(bytes(content))) for digest, content in cursor)
            for batch in _text_batches(items):
                if progress:
                    progress(computed, 0, '统计缺失正文的 token 数')
                counts = tokenizer.count_batch([text for _, text in batch])
                with conn.cursor() as writer:
                    writer.executemany(
                        'INSERT INTO text_token_count (text_hash, tokenizer_fingerprint, token_count)'
                        ' VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                        [(key, fingerprint, count) for (key, _), count in zip(batch, counts)])
                conn.commit()
                computed += len(batch)
    return computed


def list_stats(conn, release_id):
    get_release(conn, release_id)
    return [row[0] for row in conn.execute(
        'SELECT to_jsonb(s) - \'tokenizer_fingerprint\' || jsonb_build_object(\'tokenizer_fingerprint\','
        " encode(tokenizer_fingerprint,'hex')) FROM release_token_stats s"
        ' WHERE release_id = %s ORDER BY created_at, tokenizer_fingerprint', (release_id,))]


def _cut_counts(conn, release_id, tokenizer, progress):
    from . import exporters
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS cut_token_count (version_id bigint PRIMARY KEY,token_count bigint)'
                 ' ON COMMIT PRESERVE ROWS')
    conn.execute('TRUNCATE cut_token_count')
    count = 0
    with psycopg.connect(conn.info.dsn) as reader:
        reader.execute('SET TRANSACTION READ ONLY')
        items = ((row['version_id'],row['text']) for row in exporters._documents(reader,release_id,only_cut=True))
        for batch in _text_batches(items):
            if progress:
                progress(count,0,'统计裁剪后正文的 token 数')
            counts = tokenizer.count_batch([text for _,text in batch])
            with conn.cursor() as writer:
                writer.executemany('INSERT INTO cut_token_count VALUES(%s,%s)',
                                   [(vid,n) for (vid,_),n in zip(batch,counts)])
            count += len(batch)
    return count


def compute(conn, release_id, tokenizer, *, progress=None):
    get_release(conn, release_id)
    computed = _fill_cache(conn, release_id, tokenizer, progress)
    cut_documents = _cut_counts(conn,release_id,tokenizer,progress)
    if progress:
        progress(computed, computed, '汇总固定发布的 token 统计')
    fingerprint = bytes.fromhex(tokenizer.fingerprint)
    by_split = {name: 0 for name in ('train', 'val', 'test')}
    by_source, by_category = [], []
    total, percentiles = 0, {}
    for dimension, split, source, category, tokens, quantiles in conn.execute(
        "WITH counts AS (SELECT m.split, s.name AS source, v.category, coalesce(ct.token_count,c.token_count) token_count"
        ' FROM dataset_member m JOIN document_version v USING (version_id)'
        ' JOIN document d USING (doc_id) JOIN source s USING (source_id)'
        ' LEFT JOIN text_token_count c ON c.text_hash = v.text_hash AND c.tokenizer_fingerprint = %s'
        ' LEFT JOIN cut_token_count ct USING(version_id)'
        ' WHERE m.release_id = %s)'
        " SELECT CASE WHEN grouping(split)=0 THEN 'split' WHEN grouping(source)=0 THEN 'source'"
        " WHEN grouping(category)=0 THEN 'category' ELSE 'total' END, split,source,category,"
        ' coalesce(sum(token_count),0), percentile_disc(ARRAY[0.5,0.9,0.95,0.99])'
        ' WITHIN GROUP (ORDER BY token_count) FROM counts'
        ' GROUP BY GROUPING SETS ((split),(source),(category),())', (fingerprint, release_id)):
        tokens = int(tokens)
        if dimension == 'split':
            by_split[split] = tokens
        elif dimension == 'source':
            by_source.append({'source': source, 'tokens': tokens})
        elif dimension == 'category':
            by_category.append({'category': category, 'tokens': tokens})
        else:
            total = tokens
            percentiles = dict(zip(('p50', 'p90', 'p95', 'p99'), quantiles or [None] * 4))
    by_source.sort(key=lambda item: item['source'])
    by_category.sort(key=lambda item: (item['category'] is not None, item['category'] or ''))
    conn.execute(
        'INSERT INTO release_token_stats (release_id,unit,tokenizer_fingerprint,tokenizer_name,'
        ' total,by_split,by_source,by_category,percentiles) VALUES (%s,\'document\',%s,%s,%s,%s,%s,%s,%s)'
        ' ON CONFLICT DO NOTHING',
        (release_id, fingerprint, tokenizer.name, total, Jsonb(by_split), Jsonb(by_source),
         Jsonb(by_category), Jsonb(percentiles)))
    conn.commit()
    unique = conn.execute('SELECT count(DISTINCT v.text_hash) FROM dataset_member m'
                          ' JOIN document_version v USING (version_id) WHERE m.release_id = %s'
                          ' AND NOT EXISTS(SELECT 1 FROM dataset_member_cut cut WHERE cut.release_id=m.release_id AND cut.version_id=m.version_id)',
                          (release_id,)).fetchone()[0]
    record = next(item for item in list_stats(conn, release_id)
                  if item['tokenizer_fingerprint'] == tokenizer.fingerprint and item['unit'] == 'document')
    cache = {'computed_texts': computed, 'cached_texts': unique - computed}
    if cut_documents:
        cache['computed_cut_documents'] = cut_documents
    return {**record, 'cache': cache}


def estimate(conn, kept, groups, assigned, tokenizer, *, cuts=None):
    cuts = cuts or {}
    chars_by_text = {item.text_hash: item.char_count for item in kept if item.version_id not in cuts}
    known = dict(conn.execute(
        'SELECT text_hash, token_count FROM text_token_count WHERE tokenizer_fingerprint = %s'
        ' AND text_hash = ANY(%s)', (bytes.fromhex(tokenizer.fingerprint), list(chars_by_text))))
    known_chars = sum(chars_by_text[key] for key in known)
    ratio = sum(known.values()) / known_chars if known_chars else None
    totals = {'all': [0, 0, 0, 0]}
    distributions = {'split': {}, 'source': {}, 'category': {}}
    for item in kept:
        values = [totals['all']]
        for dimension, key in (('split', assigned[groups[item.doc_id]]), ('source', item.source),
                               ('category', item.category)):
            values.append(distributions[dimension].setdefault(key, [0, 0, 0, 0]))
        for value in values:
            value[3] += 1
            if item.version_id not in cuts and item.text_hash in known:
                value[0] += known[item.text_hash]
                value[1] += 1
            else:
                value[2] += item.char_count

    def report(value):
        exact, covered, missing_chars, documents = value
        complete = covered == documents
        estimated = 0 if complete else missing_chars * ratio if ratio is not None else None
        return {'status': 'exact' if complete else 'estimated' if ratio is not None else 'unknown',
                'exact': exact if known or complete else None, 'estimated': estimated,
                'total': exact + estimated if estimated is not None else None,
                'coverage': covered / documents if documents else 1,
                'cached_documents': covered, 'documents': documents}

    return {**report(totals['all']), 'unit': 'document', 'tokenizer_name': tokenizer.name,
            'tokenizer_fingerprint': tokenizer.fingerprint,
            'by_split': {key: report(value) for key, value in sorted(distributions['split'].items())},
            'by_source': [{'source': key, **report(value)} for key, value in sorted(distributions['source'].items())],
            'by_category': [{'category': key, **report(value)} for key, value in sorted(
                distributions['category'].items(), key=lambda item: (item[0] is not None, item[0] or ''))]}


def _members(conn, release_id, fingerprint, *, content=False):
    with conn.cursor(name='token_members') as cursor:
        cursor.itersize = 16
        cursor.execute(
            'SELECT m.ordinal,m.version_id,v.doc_id,m.split,s.name AS source,v.category,'
            ' v.composed_label AS era,coalesce(ct.token_count,c.token_count) AS tokens,'
            ' (SELECT jsonb_agg(jsonb_build_array(start_char,end_char) ORDER BY start_char)'
            ' FROM dataset_member_cut cut WHERE cut.release_id=m.release_id AND cut.version_id=m.version_id) AS _cuts' +
            (',b.content' if content else '') +
            ' FROM dataset_member m JOIN document_version v USING (version_id)'
            ' JOIN document d USING (doc_id) JOIN source s USING (source_id)'
            ' LEFT JOIN text_token_count c ON c.text_hash = v.text_hash AND c.tokenizer_fingerprint = %s'
            ' LEFT JOIN cut_token_count ct USING(version_id)' +
            (' JOIN text_blob b ON b.text_hash = v.text_hash' if content else '') +
            ' WHERE m.release_id = %s ORDER BY m.split,m.ordinal', (bytes.fromhex(fingerprint), release_id))
        keys = [item.name for item in cursor.description]
        for row in cursor:
            yield dict(zip(keys, row))
