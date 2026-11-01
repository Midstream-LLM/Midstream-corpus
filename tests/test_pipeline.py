"""端到端测试，需要 MIDSTREAM_TEST_DSN 指向一个专用空库。"""

import json
import multiprocessing
import os
import random
import runpy
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from midstream import batches, dedup, export, ingest, jobs, management, query, reprocess
from midstream.adapters import ADAPTERS
from midstream.rules import RULES

DSN = os.environ.get("MIDSTREAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 MIDSTREAM_TEST_DSN")
SCHEMA = Path(__file__).parents[1] / "migrations/versions/0001_initial.sql"


def row(key, text, chapter="史记‧卷一", book="史记", era="汉"):
    return json.dumps(
        {"id": key, "source": book, "author": "司马迁", "era": era, "category": "史",
         "chapter": chapter, "content": text},
        ensure_ascii=False,
    )


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        for statement in SCHEMA.read_text(encoding="utf-8").split(";\n"):
            if statement.strip():
                connection.execute(statement)
        for statement in SCHEMA.with_name("0002_fingerprint.sql").read_text(encoding="utf-8").split(";\n"):
            if statement.strip():
                connection.execute(statement)
        for statement in SCHEMA.with_name("0003_dup_span.sql").read_text(encoding="utf-8").split(";\n"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(SCHEMA.with_name("0004_staging_hash.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0005_release_char_count.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0006_incremental_dedup.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0007_manual_batch.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0008_import_plan.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0009_release_selection.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0010_jobs.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0011_dataset_recipes.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0012_dataset_exports.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0013_token_stats.sql").read_text(encoding="utf-8"))
        connection.commit()
        yield connection


def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_import_uniform_samples(tmp_path):
    from midstream import importing
    path=write(tmp_path/'uniform.jsonl',[row(str(i),'甲'+str(i),chapter='样本'+str(i)) for i in range(200)])
    result=importing.preview('ccc_corpus',[str(path)])
    keys={int(item['source_key']) for item in result['samples']}
    assert 0 in keys and 199 in keys and any(60<i<140 for i in keys)
    assert len(keys)<=9 and result['fill_rates']
    data=tmp_path/'uniform.json'
    data.write_text(json.dumps([{'id':i,'body':'甲'+str(i),'title':'题'+str(i)} for i in range(100)],ensure_ascii=False))
    mapped=importing.preview('sample-json',[str(data)],{'format':'json','text_field':'body','id_field':'id','fields':{'title':'title'}})
    assert any(item['metadata']['title']=='题99' for item in mapped['samples'])
    assert any(item['metadata']['title']=='题0' for item in mapped['samples'])
    for encoding in ('utf-16','gb18030'):
        lines=tmp_path/(encoding+'.jsonl')
        lines.write_text('\n'.join(json.dumps({'id':i,'body':'甲文','title':'题'+str(i)},ensure_ascii=False) for i in range(100)),encoding=encoding)
        result=importing.preview('sample-lines',[str(lines)],{'format':'jsonl','encoding':encoding,'text_field':'body','id_field':'id','fields':{'title':'title'}})
        assert {'题0','题99'} <= {item['metadata']['title'] for item in result['samples']}


def test_batch_two_layer_preview_matches_direct_publish(conn,tmp_path,monkeypatch):
    from copy import copy
    from midstream import importing
    first=write(tmp_path/'baseline.jsonl',[row('same','甲文'),row('changed','乙文')])
    ingest.ingest(conn,ADAPTERS['ccc_corpus'],[first])
    incoming=write(tmp_path/'incoming.jsonl',[row('same','甲文'),row('changed','乙文改'),row('new','甲\r\n新文'),'{坏行'])
    preview=importing.preview('ccc_corpus',[str(incoming)])
    batch=importing.register(conn,'ccc_corpus',[str(incoming)],preview['objects'],preview['parser_version'],'test','登记')
    batch_id=batch['batch_id']
    before=conn.execute('SELECT version_id,text_hash,is_current FROM document_version ORDER BY version_id').fetchall()
    assert batches.stage(conn,batch_id,actor='test',reason='暂存')['awaiting_confirmation']
    with psycopg.connect(DSN) as readonly:
        readonly.execute('SET TRANSACTION READ ONLY')
        report=batches.preview(readonly,batch_id)
        assert {key:value['documents'] for key,value in report['counts'].items()}=={'new':1,'updated':1,'unchanged':1,'failed':1}
        assert report['counts']['failed']['chars'] is None and report['exact_match_documents']==1
        records=batches.records(readonly,batch_id,limit=2)
        assert records['next_cursor'] and len(records['items'])==2
        new=batches.records(readonly,batch_id,status='new')['items'][0]
        difference=batches.record_diff(readonly,batch_id,new['record_id'])
        assert difference['before']=='甲\r\n新文' and difference['after']=='甲\n新文'
    assert conn.execute('SELECT version_id,text_hash,is_current FROM document_version ORDER BY version_id').fetchall()==before
    published=batches.publish(conn,batch_id,actor='test',reason='确认')
    assert (published['new_documents'],published['new_versions'],published['unchanged'])==(1,4,1)
    assert batches.publish(conn,batch_id,actor='test',reason='重试')['status']=='already_published'
    other=copy(ADAPTERS['ccc_corpus']);other.name='direct-preview-fixture'
    ingest.ingest(conn,other,[first]);direct=ingest.ingest(conn,other,[incoming])
    assert (direct.new_documents,direct.new_versions,direct.unchanged)==(1,4,1)
    def current(source):
        return conn.execute('SELECT d.source_key,v.text_hash,v.char_count FROM document_version v JOIN document d USING(doc_id)'
                            ' JOIN source s USING(source_id) WHERE v.is_current AND s.name=%s ORDER BY source_key',(source,)).fetchall()
    assert current('ccc_corpus')==current(other.name)
    assert conn.execute('SELECT count(*) FROM staging_text WHERE batch_id=%s',(batch_id,)).fetchone()[0]==0


def test_staged_interruption_resume_and_abandon(conn,tmp_path,monkeypatch):
    path=write(tmp_path/'stage-interrupt.jsonl',[row('recover','恢复正文')])
    batch=ingest.register_batch(conn,ADAPTERS['ccc_corpus'],[path])
    validate=ingest.validate
    def interrupted(conn,batch):
        validate(conn,batch)
        raise KeyboardInterrupt()
    monkeypatch.setattr(ingest,'validate',interrupted)
    with pytest.raises(KeyboardInterrupt):
        batches.stage(conn,batch,actor='test',reason='暂存中断')
    assert batches.inspect(conn,batch)['status']=='validated'
    monkeypatch.setattr(ingest,'validate',validate)
    assert batches.stage(conn,batch,actor='test',reason='恢复')['awaiting_confirmation']
    assert batches.preview(conn,batch)['counts']['new']['documents']==1
    batches.abandon(conn,batch,actor='test',reason='放弃')
    assert not batches.inspect(conn,batch)['has_partition']
    assert conn.execute('SELECT count(*) FROM staging_text').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM document_version').fetchone()[0]==0
    with pytest.raises(management.ManagementError):
        batches.publish(conn,batch,actor='test',reason='不可发布')


def test_dataset_workbench_actions(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream import api, config, exporters
    seed_metadata(conn, 20)
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path)
    release = export.create_release(conn, export.Recipe.load({'name': 'workbench', 'seed': 's', 'splits': {'train': 1}}))
    result = exporters.export_release(conn, release)
    before = export.get_release(conn, release)
    app = api.create_app()
    app.dependency_overrides[api.connection] = lambda: conn
    app.dependency_overrides[api.write_connection] = lambda: conn
    with TestClient(app) as client, psycopg.connect(DSN, autocommit=True) as control:
        payload = {'actor': 'tester', 'reason': '工作台验收', 'hash_files': True}
        assert client.post(f'/api/v1/datasets/{release}/verify', json=payload).status_code == 403
        job = client.post(f'/api/v1/datasets/{release}/verify', json=payload, headers={'X-Midstream-Action': '1'}).json()
        Path(result['files'][0]).unlink()
        jobs._run_next(conn, control)
        assert jobs.show(control, job['job_id'])['status'] == 'failed'
        saved = client.post(f'/api/v1/datasets/{release}/training', json={
            'actor': 'tester', 'reason': '训练依据', 'run_name': 'workbench-train'}, headers={'X-Midstream-Action': '1'})
        assert saved.status_code == 200
        registered = client.get(f'/api/v1/datasets/{release}/training').json()
        assert json.loads(registered[0]['note']) == {'actor': 'tester', 'reason': '训练依据'}
        with pytest.raises(export.TrainingLeak):
            export.plan_release(conn, export.Recipe.load({'name': 'leak', 'seed': 's', 'splits': {'val': 1}}))
        assert export.get_release(conn, release) == before


def synthetic_tokenizer(tmp_path, *, chars=False, wide=False):
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors
    vocab = {'[UNK]': 0, '[BOS]': 1, '[EOS]': 2, '甲': 3, '乙': 4, '丙': 5}
    if wide:
        vocab.update({f't{i}': i for i in range(6, 65537)})
        vocab['甲'], vocab['t65536'] = 65536, 3
    backend = Tokenizer(models.WordLevel(vocab, unk_token='[UNK]'))
    backend.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    if chars:
        backend.normalizer = normalizers.BertNormalizer(clean_text=False, handle_chinese_chars=True,
                                                        strip_accents=False, lowercase=False)
    backend.post_processor = processors.TemplateProcessing(
        single='[BOS] $A [EOS]', special_tokens=[('[BOS]', 1), ('[EOS]', 2)])
    backend.enable_padding(length=12, pad_id=0, pad_token='[UNK]')
    backend.enable_truncation(max_length=3)
    path = tmp_path / ('wide' if wide else 'chars' if chars else 'words') / 'tokenizer.json'
    path.parent.mkdir(parents=True)
    backend.save(str(path))
    return path


def token_release(conn, tmp_path, texts):
    records = [json.loads(row(str(i), text, book=f'合成书{i}')) for i, text in enumerate(texts)]
    records[-1]['category'] = None
    path = write(tmp_path / 'token-input.jsonl', [json.dumps(record, ensure_ascii=False) for record in records])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    other = conn.execute("INSERT INTO source (name) VALUES ('tokens-other') RETURNING source_id").fetchone()[0]
    conn.execute('UPDATE document SET source_id=%s WHERE source_key=%s', (other, str(len(texts)-1)))
    conn.commit()
    recipe = export.Recipe.load({'name': 'token-release', 'seed': 's', 'splits': {'train': .8, 'val': .1, 'test': .1}})
    return export.create_release(conn, recipe), recipe


def test_token_stats_job_exact_cache_and_frozen_release(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tokenizers import Tokenizer as Backend
    from midstream import api, token_stats, tokenizing
    texts = ['甲 乙 甲', '乙', '甲 乙 甲']
    release, _ = token_release(conn, tmp_path, texts)
    path = synthetic_tokenizer(tmp_path)
    backend = Backend.from_file(str(path))
    backend.no_padding()
    backend.no_truncation()
    direct = [len(backend.encode(text, add_special_tokens=False).ids) for text in texts]
    assert direct == [3, 1, 3]
    app = api.create_app()
    app.dependency_overrides[api.connection] = lambda: conn
    app.dependency_overrides[api.write_connection] = lambda: conn
    payload = {'tokenizer': str(path), 'actor': 'tester', 'reason': '合成统计'}
    with TestClient(app) as client, psycopg.connect(DSN, autocommit=True) as control:
        assert client.post(f'/api/v1/datasets/{release}/token-stats', json=payload).status_code == 403
        queued = client.post(f'/api/v1/datasets/{release}/token-stats', json=payload,
                             headers={'X-Midstream-Action': '1'}).json()
        jobs._run_next(conn, control)
        result = jobs.show(control, queued['job_id'])
        assert result['status'] == 'succeeded', result['error']
        result = result['result']
        assert result['total'] == sum(direct) == 7
        assert sum(result['by_split'].values()) == 7
        assert {item['source']: item['tokens'] for item in result['by_source']} == {'ccc_corpus': 4, 'tokens-other': 3}
        assert {'category': None, 'tokens': 3} in result['by_category']
        assert result['percentiles'] == {'p50': 3, 'p90': 3, 'p95': 3, 'p99': 3}
        assert result['cache'] == {'computed_texts': 2, 'cached_texts': 0}
        frozen = client.get(f'/api/v1/datasets/{release}/token-stats').json()

        def no_body(*args):
            raise AssertionError('重跑命中缓存，不应解压正文')

        with monkeypatch.context() as patch:
            patch.setattr(token_stats.textcodec, 'decompress', no_body)
            repeated = token_stats.compute(conn, release, tokenizing.load(path))
        assert repeated['cache'] == {'computed_texts': 0, 'cached_texts': 2}
        assert repeated['created_at'] == frozen[0]['created_at']
        reprocess.reprocess(conn, reprocess.Rule('tokens-update', None,
                             lambda doc: reprocess.Change(text='丙 甲 乙 甲')))
        assert client.get(f'/api/v1/datasets/{release}/token-stats').json() == frozen


def test_token_estimates_and_independent_tokenizer_records(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream import api, token_stats, tokenizing
    release, recipe = token_release(conn, tmp_path, ['甲乙', '甲乙丙', '甲乙'])
    path = synthetic_tokenizer(tmp_path)
    tokenizer = tokenizing.load(path)
    conn.commit()
    app = api.create_app()
    app.dependency_overrides[api.connection] = lambda: conn

    def preview():
        with monkeypatch.context() as patch:
            patch.setattr(token_stats.textcodec, 'decompress', lambda *args: pytest.fail('试算不应解压正文'))
            with TestClient(app) as client:
                response = client.post('/api/v1/datasets/preview', json={'recipe': recipe.raw, 'tokenizer': str(path)})
                assert response.status_code == 200
                return response.json()['token_stats']

    unknown = preview()
    assert unknown['status'] == 'unknown' and unknown['coverage'] == 0
    assert unknown['exact'] is None and unknown['estimated'] is None and unknown['total'] is None
    partial = export.create_release(conn, export.Recipe.load({
        **recipe.raw, 'name': 'partial-tokens', 'include': [], 'documents': {'add': [1]}}))
    token_stats.compute(conn, partial, tokenizer)
    estimated = preview()
    assert estimated['status'] == 'estimated' and estimated['coverage'] == pytest.approx(2 / 3)
    assert estimated['exact'] == 2 and estimated['estimated'] == 1.5 and estimated['total'] == 3.5
    full = token_stats.compute(conn, release, tokenizer)
    exact = preview()
    assert exact['status'] == 'exact' and exact['estimated'] == 0 and exact['exact'] == full['total'] == 3
    second = tokenizing.load(synthetic_tokenizer(tmp_path, chars=True))
    other = token_stats.compute(conn, release, second)
    assert other['total'] == 7 and second.fingerprint != tokenizer.fingerprint
    assert len(token_stats.list_stats(conn, release)) == 2
    assert conn.execute('SELECT count(*) FROM text_token_count').fetchone()[0] == 4


def test_token_shards_match_statistics_and_dtype(conn, tmp_path, monkeypatch):
    import numpy as np
    from midstream import config, token_stats, tokenizing
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path / 'outputs')
    texts = ['甲 乙 甲', '乙', '甲 乙 甲']
    release, _ = token_release(conn, tmp_path, texts)
    before = export.get_release(conn, release)
    with psycopg.connect(DSN, autocommit=True) as control:
        for wide in (False, True):
            path = synthetic_tokenizer(tmp_path, wide=wide)
            job = jobs.submit(conn, 'tokenize', {'release_id': release, 'tokenizer': str(path), 'shard_tokens': 4},
                              'tester', '分片一致性')
            jobs._run_next(conn, control)
            completed = jobs.show(control, job['job_id'])
            assert completed['status'] == 'succeeded', completed['error']
            result = completed['result']
            manifest = json.loads(Path(result['manifest']).read_text())
            root = Path(result['manifest']).parent
            shard_info = [json.loads(line) for line in (root / 'tokens/shard_info.jsonl').read_text().splitlines()]
            arrays = {item['shard']: np.load(item['path'], mmap_mode='r', allow_pickle=False) for item in shard_info}
            assert all(array.dtype == np.dtype('<u4' if wide else '<u2') for array in arrays.values())
            tokenizer = tokenizing.load(path)
            direct = tokenizer.encode_batch(texts)
            records = [json.loads(line) for line in (root / 'manifest/docs_tokens.jsonl').read_text().splitlines()]
            docs = dict(conn.execute('SELECT doc_id,source_key FROM document'))
            for record in records:
                actual = arrays[record['shard']][record['offset']:record['offset'] + record['tokens']].tolist()
                assert actual == direct[int(docs[record['doc_id']])]
            if wide:
                assert any(65536 in array for array in arrays.values())
            stats = next(item for item in token_stats.list_stats(conn, release)
                         if item['tokenizer_fingerprint'] == tokenizer.fingerprint)
            assert sum(map(len, arrays.values())) == sum(item['tokens'] for item in records) == stats['total'] == manifest['total_tokens'] == 7
            assert manifest['add_special_tokens'] is False and manifest['truncation'] is False
            assert manifest['content_digest'] == before['content_digest']
            assert export.verify_release(conn, release, hash_files=True)
    assert export.get_release(conn, release) == before


def test_token_cancel_keeps_cache_and_no_completed_shards(conn, tmp_path, monkeypatch):
    from midstream import config, token_stats
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path / 'outputs')
    release, _ = token_release(conn, tmp_path, [f'甲 {i}' for i in range(100)])
    path = synthetic_tokenizer(tmp_path)
    with psycopg.connect(DSN, autocommit=True) as control:
        job = jobs.submit(conn, 'token_stats', {'release_id': release, 'tokenizer': str(path)}, 'test', '分批取消')
        original = jobs.HANDLERS['token_stats']

        def cancelled(conn, params, actor, reason, progress):
            def boundary(current, total, message):
                if current and '统计缺失' in message:
                    jobs.cancel(control, job['job_id'], 'test', '保留首批缓存')
                progress(current, total, message)
            return original(conn, params, actor, reason, boundary)

        monkeypatch.setitem(jobs.HANDLERS, 'token_stats', cancelled)
        jobs._run_next(conn, control)
        assert jobs.show(control, job['job_id'])['status'] == 'cancelled'
        assert conn.execute('SELECT count(*) FROM text_token_count').fetchone()[0] == 64
        assert token_stats.list_stats(conn, release) == []
        monkeypatch.setitem(jobs.HANDLERS, 'token_stats', original)
        retried = jobs.retry(conn, job['job_id'], 'test', '补齐计数')
        jobs._run_next(conn, control)
        assert jobs.show(control, retried['job_id'])['result']['cache'] == {'computed_texts': 36, 'cached_texts': 64}
        token_job = jobs.submit(conn, 'tokenize', {'release_id': release, 'tokenizer': str(path), 'shard_tokens': 20},
                                'test', '分片取消')
        encoder = jobs.HANDLERS['tokenize']

        def cancelled_export(conn, params, actor, reason, progress):
            def boundary(current, total, message):
                if current and '编码并写出' in message:
                    jobs.cancel(control, token_job['job_id'], 'test', '清理未完成分片')
                progress(current, total, message)
            return encoder(conn, params, actor, reason, boundary)

        monkeypatch.setitem(jobs.HANDLERS, 'tokenize', cancelled_export)
        jobs._run_next(conn, control)
        assert jobs.show(control, token_job['job_id'])['status'] == 'cancelled'
        assert conn.execute('SELECT count(*) FROM export_artifact').fetchone()[0] == 0
        assert not list((config.EXPORT_ROOT / 'token-release').iterdir())
        assert token_stats.list_stats(conn, release)[0]['total'] == 200
        monkeypatch.setitem(jobs.HANDLERS, 'tokenize', encoder)
        retried = jobs.retry(conn, token_job['job_id'], 'test', '重新写分片')
        jobs._run_next(conn, control)
        assert jobs.show(control, retried['job_id'])['result']['total_tokens'] == 200


def seed_metadata(conn, count, group_size=1):
    from midstream import textcodec
    sources = [conn.execute('INSERT INTO source (name) VALUES (%s) RETURNING source_id',
                            (name,)).fetchone()[0] for name in ('meta-a', 'meta-b')]
    hashes = []
    for length in (1000, 10):
        text = '测' * length
        digest = textcodec.text_hash(text)
        hashes.append(digest)
        conn.execute('INSERT INTO text_blob (text_hash, codec, char_count, byte_count, content)'
                     " VALUES (%s, 'zstd', %s, %s, %s)",
                     (digest, length, length * 3, textcodec.compress(text)))
    run = conn.execute("INSERT INTO processing_run (kind, rule_version) VALUES ('parse', 'synthetic')"
                       ' RETURNING run_id').fetchone()[0]
    conn.execute('INSERT INTO document (source_id, source_key)'
                 ' SELECT (%s::bigint[])[1 + i %% 2], i::text FROM generate_series(1,%s) g(i)',
                 (sources, count))
    conn.execute(
        'INSERT INTO document_version (doc_id, version_no, text_hash, run_id, is_current,'
        ' quality_status, char_count, title, authors, category, genre, composed_years, composed_label)'
        " SELECT doc_id, 1, (%s::bytea[])[1 + source_key::int %% 2], %s, true, 'ok',"
        " CASE WHEN source_key::int %% 2 = 0 THEN 1000 ELSE 10 END, source_key,"
        " ARRAY[CASE WHEN source_key::int %% 2 = 0 THEN '甲作者' ELSE '乙作者' END],"
        " CASE WHEN source_key::int %% 4 < 2 THEN '史' ELSE '集' END,"
        " CASE WHEN source_key::int %% 2 = 0 THEN '文' ELSE '诗' END,"
        " CASE WHEN source_key::int %% 10 > 0 THEN int4range(901,950,'[]') END, NULL FROM document",
        (hashes, run))
    if group_size > 1:
        conn.execute("INSERT INTO work (canonical_title) SELECT i::text FROM generate_series(1,%s) g(i)",
                     ((count + group_size - 1) // group_size,))
        conn.execute('INSERT INTO document_work (doc_id, work_id, relation, basis, status)'
                     " SELECT doc_id, (doc_id-1)/%s+1, 'whole', 'source', 'confirmed' FROM document",
                     (group_size,))
    conn.commit()


def test_dataset_selection_preview_api(conn, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream import api, textcodec
    seed_metadata(conn, 100)
    body = {'name': 'selection-api', 'seed': 'm2', 'splits': {'train': 1},
            'include': [{'authors': ['甲作者'], 'genres': ['文'], 'categories': ['史']},
                        {'author': '乙作者', 'genre': '诗', 'category': '集'}],
            'exclude': [{'title_prefix': '1'}], 'documents': {'add': [1], 'remove': [2]}}
    expected = [item.doc_id for item in export._select(conn, export.Recipe.load(body))]
    assert 1 in expected and 2 not in expected and 10 not in expected and 11 not in expected
    assert export._select(conn, export.Recipe.load({**body, 'include': [{'sources': ['meta-a', 'meta-b']}],
                                                  'exclude': [], 'documents': {}}))
    conn.commit()
    with psycopg.connect(DSN) as readonly:
        readonly.execute('SET TRANSACTION READ ONLY')
        def no_body(*args):
            raise AssertionError('试算不能解压正文')
        monkeypatch.setattr(textcodec, 'decompress', no_body)
        report = export.plan_release(readonly, export.Recipe.load(body)).report
        assert report['candidates'] == len(expected)
        assert readonly.execute('SELECT count(*) FROM dataset_release').fetchone()[0] == 0
    app = api.create_app()
    app.dependency_overrides[api.connection] = lambda: conn
    app.dependency_overrides[api.write_connection] = lambda: conn
    headers = {'X-Midstream-Action': '1'}
    operation = {'recipe': body, 'actor': 'test', 'reason': '合成选用'}
    with TestClient(app) as client, psycopg.connect(DSN, autocommit=True) as control:
        assert client.post('/api/v1/datasets/preview', json={'recipe': body}).json() == report
        facets = client.get('/api/v1/datasets/facets', params={'recipe': json.dumps(body)}).json()
        assert sum(x['documents'] for x in facets['sources']) == len(expected)
        assert client.post('/api/v1/datasets', json=operation).status_code == 403
        job = client.post('/api/v1/datasets', json=operation, headers=headers).json()
        jobs._run_next(conn, control)
        release = jobs.show(control, job['job_id'])['result']['release_id']
        assert client.get(f'/api/v1/datasets/{release}').json()['char_count'] == report['chars']
        assert client.get('/api/v1/datasets').json()['items'][0]['release_id'] == release
        assert client.post('/api/v1/dataset-recipes', json=operation, headers=headers).status_code == 200
        assert client.get('/api/v1/dataset-recipes/selection-api').json()['body'] == body
        changed = {**operation, 'recipe': {**body, 'seed': 'new'}}
        assert client.put('/api/v1/dataset-recipes/selection-api', json=changed, headers=headers).status_code == 200
        assert client.get('/api/v1/datasets/' + str(release)).json()['recipe']['seed'] == 'm2'
        assert client.request('DELETE', '/api/v1/dataset-recipes/selection-api', headers=headers,
                              json={'actor': 'test', 'reason': '删草稿'}).status_code == 200
        assert client.get('/api/v1/dataset-recipes').json() == []


def test_dataset_balanced_reserved_and_deterministic(conn, monkeypatch):
    seed_metadata(conn, 2000, group_size=5)
    body = {'name': 'balanced', 'seed': 'm2', 'splits': {'train': .9, 'val': .05, 'test': .05}}
    first = export.plan_release(conn, export.Recipe.load(body))
    for split, expected in body['splits'].items():
        assert abs(first.report['splits'][split]['ratio'] - expected) <= .01
    release = export.write_release(conn, first)
    selected = export._select
    monkeypatch.setattr(export, '_select', lambda conn, recipe: list(reversed(selected(conn, recipe))))
    assert export.plan_release(conn, export.Recipe.load(body)).members == first.members
    groups = export._group_keys(conn, list(range(1, 2001)))
    splits = {version: split for _, version, split, _ in first.members}
    for doc_id, group in groups.items():
        assert splits[doc_id] == splits[group]
    test_doc = next(version for version, split in splits.items() if split == 'test')
    reserved = {**body, 'name': 'reserved', 'seed': 'another', 'split_by': 'documents',
                'stratify': ['source', 'category'],
                'limits': {'val': {'max_chars': first.report['splits']['val']['chars']}},
                'reserved': {'val': {'releases': [{'release_id': release, 'split': 'val'}]},
                             'test': {'doc_ids': [test_doc]}}}
    plan = export.plan_release(conn, export.Recipe.load(reserved))
    next_splits = {version: split for _, version, split, _ in plan.members}
    assert all(next_splits[doc] == 'val' for doc, split in splits.items() if split == 'val')
    assert next_splits[test_doc] == 'test'
    assert plan.report['splits']['val']['chars'] <= reserved['limits']['val']['max_chars']
    assert abs(plan.report['splits']['train']['document_ratio'] - .9) <= .01


def test_dataset_reserved_training_leak_and_group_conflict(conn):
    seed_metadata(conn, 100, group_size=5)
    body = {'name': 'reserved-safety', 'seed': 's', 'splits': {'train': .9, 'val': .05, 'test': .05}}
    with pytest.raises(export.ExportError, match='同一作品'):
        export.plan_release(conn, export.Recipe.load({**body, 'reserved': {
            'val': {'doc_ids': [1]}, 'test': {'doc_ids': [2]}}}))
    used = export.create_release(conn, export.Recipe.load({**body, 'name': 'trained', 'splits': {'train': 1},
                                                          'include': [], 'documents': {'add': [1]}}))
    export.record_training(conn, 'trained', 'fixture-training')
    with pytest.raises(export.TrainingLeak):
        export.plan_release(conn, export.Recipe.load({**body,
            'documents': {'remove': [1]}, 'reserved': {'val': {'doc_ids': [1]}}}))
    run = conn.execute("INSERT INTO processing_run (kind, rule_version) VALUES ('dedup', 'synthetic') RETURNING run_id").fetchone()[0]
    cluster = conn.execute("INSERT INTO dup_cluster (run_id, level) VALUES (%s, 'document') RETURNING cluster_id", (run,)).fetchone()[0]
    conn.execute("INSERT INTO dup_cluster_member (cluster_id, version_id, kind) VALUES (%s,1,'exact'),(%s,6,'exact')",
                 (cluster, cluster))
    conn.commit()
    with pytest.raises(export.TrainingLeak):
        export.plan_release(conn, export.Recipe.load({**body, 'splits': {'val': 1},
            'include': [], 'documents': {'add': [6]}}))
    assert export.verify_release(conn, used)


def test_dataset_large_groups_warn_without_splitting(conn):
    seed_metadata(conn, 100, group_size=80)
    plan = export.plan_release(conn, export.Recipe.load({
        'name': 'huge-groups', 'seed': 's', 'splits': {'train': .9, 'val': .05, 'test': .05}}))
    assert {split for _, _, split, _ in plan.members} == {'train'}
    assert any('超大组' in message for message in plan.report['warnings'])
    assert any('百分点' in message for message in plan.report['warnings'])


def read_export(result):
    import io
    import pickle
    import zstandard
    manifest = json.loads(Path(result['manifest']).read_text())
    rows = []
    for file in manifest['files']:
        path = Path(result['manifest']).parent / file['file']
        if manifest['format'] == 'parquet':
            rows.extend(pq.read_table(path).to_pylist())
        elif manifest['format'] == 'pkl':
            with path.open('rb') as handle:
                rows.extend(pickle.load(handle))
        elif manifest['compression'] == 'zst':
            with path.open('rb') as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                rows.extend(json.loads(line) for line in io.TextIOWrapper(reader))
        else:
            rows.extend(json.loads(line) for line in path.read_text().splitlines())
    return sorted(rows, key=lambda item: (item['ordinal'], item.get('sample_index', 0)))


def test_multiformat_export_job_roundtrip(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream import api, config, exporters
    seed_metadata(conn, 20)
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path)
    recipe = export.Recipe.load({'name': 'formats', 'seed': 's', 'splits': {'train': .8, 'val': .2}})
    release = export.create_release(conn, recipe)
    before = export.get_release(conn, release)
    app = api.create_app()
    app.dependency_overrides[api.connection] = lambda: conn
    app.dependency_overrides[api.write_connection] = lambda: conn
    headers = {'X-Midstream-Action': '1'}
    outputs = []
    fields = exporters.DEFAULT_FIELDS + ['authors', 'era', 'category', 'genre', 'source']
    with TestClient(app) as client, psycopg.connect(DSN, autocommit=True) as control:
        for format, compression in [('parquet', None), ('jsonl', None), ('pkl', None), ('jsonl', 'zst')]:
            payload = {'actor': 'tester', 'reason': '格式一致性',
                       'options': {'format': format, 'fields': fields, 'shard_mb': .003, 'compression': compression}}
            job = client.post(f'/api/v1/datasets/{release}/exports', json=payload, headers=headers).json()
            jobs._run_next(conn, control)
            completed = jobs.show(control, job['job_id'])
            assert completed['status'] == 'succeeded', completed['error']
            result = completed['result']
            manifest = json.loads(Path(result['manifest']).read_text())
            assert manifest['recipe'] == recipe.raw and manifest['content_digest'] == before['content_digest']
            assert sum(file['rows'] for file in manifest['files']) == 20
            outputs.append(read_export(result))
        assert all(rows == outputs[0] for rows in outputs)
        assert outputs[0][0]['era'] is None
        repeated = exporters.export_release(conn, release, exporters.Options(fields=fields, shard_mb=.003))
        assert read_export(repeated) == outputs[0]
        assert len(client.get(f'/api/v1/datasets/{release}/exports').json()) == 5
        assert export.get_release(conn, release) == before
        assert export.verify_release(conn, release, hash_files=True)
        # 同字节数的文本变化：默认检查结构，--hash 才检测内容哈希变化。
        json_export = next(item for item in exporters.list_exports(conn, release)
                           if item['format'] == 'jsonl' and item['options']['compression'] is None)
        path = Path(json_export['files'][0]['path'])
        path.write_bytes(path.read_bytes().replace('测'.encode(), '改'.encode(), 1))
        assert export.verify_release(conn, release)
        assert not export.verify_release(conn, release, hash_files=True)
        path.unlink()
        assert not export.verify_release(conn, release)


def test_sample_exports_reconstruct_text(conn, tmp_path, monkeypatch):
    from midstream import config, exporters
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path / 'outputs')
    texts = ['甲段落。乙句！\n\n第三段\n末段。' * 8 + '𠀋😀　尾', '无标点长文' * 30]
    path = write(tmp_path / 'samples.jsonl', [row(str(i), text, book=f'书{i}') for i, text in enumerate(texts)])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    release = export.create_release(conn, export.Recipe.load({'name': 'samples', 'seed': 's', 'splits': {'train': 1}}))
    originals = {item['ordinal']: item['text'] for item in read_export(exporters.export_release(conn, release))}
    for overlap in (0, 7):
        options = exporters.Options(format='jsonl', unit='sample', max_chars=31, overlap_chars=overlap, shard_mb=.001)
        result = exporters.export_release(conn, release, options)
        samples = read_export(result)
        assert samples == read_export(exporters.export_release(conn, release, options))
        restored, previous = {}, {}
        for sample in samples:
            ordinal = sample['ordinal']
            start, end = sample['start_char'], sample['end_char']
            assert end - start <= 31
            assert sample['text'] == originals[ordinal][start:end]
            if sample['sample_index'] == 0:
                restored[ordinal] = sample['text']
            else:
                assert previous[ordinal] - start == overlap
                restored[ordinal] += sample['text'][overlap:]
            previous[ordinal] = end
        assert restored == originals
        assert json.loads(Path(result['manifest']).read_text())['overlap_chars'] == overlap


def test_export_interruption_keeps_no_completed_artifacts(conn, tmp_path, monkeypatch):
    from midstream import config, exporters
    seed_metadata(conn, 20)
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path)
    release = export.create_release(conn, export.Recipe.load({'name': 'interrupted', 'seed': 's', 'splits': {'train': 1}}))
    write_shard = exporters._write_shard

    def interrupted(*args):
        write_shard(*args)
        raise KeyboardInterrupt()

    monkeypatch.setattr(exporters, '_write_shard', interrupted)
    with pytest.raises(KeyboardInterrupt):
        exporters.export_release(conn, release, exporters.Options(shard_mb=.001))
    assert conn.execute('SELECT count(*) FROM export_artifact').fetchone()[0] == 0
    assert exporters.list_exports(conn, release)[0]['status'] == 'failed'
    assert not list((tmp_path / 'interrupted').iterdir())
    monkeypatch.setattr(exporters, '_write_shard', write_shard)
    result = exporters.export_release(conn, release)
    assert len(read_export(result)) == 20
    assert export.verify_release(conn, release, hash_files=True)


def test_export_memory_stays_bounded(conn, tmp_path, monkeypatch):
    import tracemalloc
    from midstream import config, exporters
    seed_metadata(conn, 10000)
    monkeypatch.setattr(config, 'EXPORT_ROOT', tmp_path)
    small = export.create_release(conn, export.Recipe.load({
        'name': 'small-memory', 'seed': 's', 'splits': {'train': 1}, 'include': [],
        'documents': {'add': list(range(1, 1001))}}))
    large = export.create_release(conn, export.Recipe.load({
        'name': 'large-memory', 'seed': 's', 'splits': {'train': 1}}))
    peaks = []
    for release in (small, large):
        tracemalloc.start()
        result = exporters.export_release(conn, release, exporters.Options(format='jsonl', shard_mb=.25))
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        assert Path(result['manifest']).is_file()
    assert peaks[1] < peaks[0] * 2 + 2 * 1024 ** 2


@pytest.mark.skipif(os.environ.get('MIDSTREAM_PLAN_BENCHMARK') != '1', reason='百万行性能验收单独运行一次')
def test_dataset_plan_million_metadata(conn):
    import resource
    import sys
    from time import perf_counter
    seed_metadata(conn, 1_000_000, group_size=10)
    conn.execute('SET TRANSACTION READ ONLY')
    started = perf_counter()
    plan = export.plan_release(conn, export.Recipe.load({
        'name': 'million', 'seed': 'scale', 'splits': {'train': .9, 'val': .05, 'test': .05},
        'stratify': ['source', 'category']}))
    elapsed = perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if sys.platform == 'darwin' else 1024)
    assert plan.report['documents'] == 1_000_000
    assert all(abs(plan.report['splits'][name]['ratio'] - ratio) <= .01
               for name, ratio in (('train', .9), ('val', .05), ('test', .05)))
    print(f'PLAN_BENCHMARK rows=1000000 seconds={elapsed:.3f} peak_rss_mib={peak:.1f}')


def test_jobs_submit_complete(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection, write_connection

    path = write(tmp_path / 'job.jsonl', [row('job#1', '任务正文。')])
    batch_id = ingest.register_batch(conn, ADAPTERS['ccc_corpus'], [path])
    app = create_app()
    app.dependency_overrides[connection] = lambda: conn
    app.dependency_overrides[write_connection] = lambda: conn
    headers = {'X-Midstream-Action': '1'}
    with TestClient(app) as client, psycopg.connect(DSN, autocommit=True) as control:
        payload = {'kind': 'batch_resume', 'params': {'batch_id': batch_id},
                   'actor': 'tester', 'reason': '任务发布'}
        # 浏览器写保护覆盖任务提交、取消和重试。
        assert client.post('/api/v1/jobs', json=payload).status_code == 403
        response = client.post('/api/v1/jobs', json=payload, headers=headers)
        assert response.status_code == 200
        job_id = response.json()['job_id']
        assert response.json()['status'] == 'queued'
        operation = {'actor': 'tester', 'reason': '管理任务'}
        for action in ('cancel', 'retry'):
            assert client.post(f'/api/v1/jobs/{job_id}/{action}', json=operation).status_code == 403
        assert client.post('/api/v1/jobs', json={**payload, 'kind': 'arbitrary'}, headers=headers).status_code == 422
        handler = jobs.HANDLERS['batch_resume']

        def observed(conn, params, actor, reason, progress):
            running = jobs.show(control, job_id)
            assert running['status'] == 'running' and running['heartbeat_at']
            return handler(conn, params, actor, reason, progress)

        monkeypatch.setitem(jobs.HANDLERS, 'batch_resume', observed)
        assert jobs._run_next(conn, control)
        completed = client.get(f'/api/v1/jobs/{job_id}').json()
        assert completed['status'] == 'succeeded'
        assert completed['result']['status'] == 'published'
        assert completed['progress']['current'] == completed['progress']['total']
        assert completed['events'][-1]['message'] == '执行完成'
        assert any('验证暂存' in e['message'] for e in completed['events'])
        assert client.get('/api/v1/jobs').json()['items'][0]['job_id'] == job_id
        assert conn.execute('SELECT count(*) FROM document_version').fetchone()[0] == 2


def test_jobs_worker_interrupted_retry(conn):
    import subprocess
    import sys
    from time import monotonic, sleep

    job_id = jobs.submit(conn, 'dedup', {'full': True}, 'tester', '中断恢复')['job_id']
    conn.commit()
    # 阻塞查重阶段，使真实 worker 保持 running；心跳仍应独立提交。
    conn.execute('SELECT pg_advisory_xact_lock(762031)')
    env = {**os.environ, 'MIDSTREAM_DSN': DSN}
    command = [sys.executable, '-m', 'midstream.cli', 'worker']

    def wait_status(control, target, status):
        deadline = monotonic() + 15
        while monotonic() < deadline:
            job = jobs.show(control, target)
            if job['status'] == status:
                return job
            sleep(0.1)
        pytest.fail(f'任务 {target} 未到达 {status}：{job}')

    process = subprocess.Popen(command, env=env)
    try:
        with psycopg.connect(DSN, autocommit=True) as control:
            running = wait_status(control, job_id, 'running')
            second = subprocess.run(command, env=env, capture_output=True, timeout=10)
            assert second.returncode == 2
            sleep(5.2)
            assert jobs.show(control, job_id)['heartbeat_at'] > running['heartbeat_at']
            process.kill()
            process.wait(timeout=10)
            conn.rollback()
            control.execute("UPDATE job SET heartbeat_at = now() - interval '61 seconds' WHERE job_id = %s", (job_id,))
            process = subprocess.Popen(command, env=env)
            failed = wait_status(control, job_id, 'failed')
            assert failed['error'] == 'interrupted'
            new = jobs.retry(conn, job_id, 'tester', '重试中断任务')
            assert new['job_id'] != job_id and new['params'] == {'full': True}
            assert wait_status(control, new['job_id'], 'succeeded')['result']['run_id']
            assert jobs.show(control, job_id)['status'] == 'failed'
    finally:
        process.kill()
        process.wait(timeout=10)
        conn.rollback()


def test_jobs_cancel_between_stages(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection, write_connection

    app = create_app()
    app.dependency_overrides[connection] = lambda: conn
    app.dependency_overrides[write_connection] = lambda: conn
    headers = {'X-Midstream-Action': '1'}
    operation = {'actor': 'tester', 'reason': '取消测试'}
    with TestClient(app) as client, psycopg.connect(DSN, autocommit=True) as control:
        queued = jobs.submit(conn, 'dedup_spans', {}, 'tester', '排队取消')['job_id']
        assert client.post(f'/api/v1/jobs/{queued}/cancel', json=operation, headers=headers).json()['status'] == 'cancelled'
        assert not jobs._run_next(conn, control)
        path = write(tmp_path / 'cancel.jsonl', [row('cancel#1', '取消前已暂存。')])
        batch_id = ingest.register_batch(conn, ADAPTERS['ccc_corpus'], [path])
        job_id = jobs.submit(conn, 'batch_resume', {'batch_id': batch_id}, 'tester', '阶段取消')['job_id']
        validate = ingest.validate

        def cancel_after_validate(conn, batch_id):
            status = validate(conn, batch_id)
            jobs.cancel(control, job_id, 'tester', '验证后停止')
            return status

        monkeypatch.setattr(ingest, 'validate', cancel_after_validate)
        jobs._run_next(conn, control)
        assert jobs.show(control, job_id)['status'] == 'cancelled'
        assert batches.inspect(conn, batch_id)['status'] == 'validated'
        assert conn.execute('SELECT count(*) FROM document_version').fetchone()[0] == 0
        retried = client.post(f'/api/v1/jobs/{job_id}/retry', json=operation, headers=headers).json()
        jobs._run_next(conn, control)
        assert jobs.show(control, retried['job_id'])['result']['status'] == 'published'
        assert conn.execute('SELECT count(*) FROM source_record').fetchone()[0] == 1


def test_jobs_batch_failure_no_republish(conn, tmp_path, monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as control:
        bad = write(tmp_path / 'bad-job.jsonl', [row('same', '甲'), row('same', '乙')])
        batch_id = ingest.register_batch(conn, ADAPTERS['ccc_corpus'], [bad])
        job = jobs.submit(conn, 'batch_resume', {'batch_id': batch_id}, 'tester', '暂存失败')
        jobs._run_next(conn, control)
        assert jobs.show(control, job['job_id'])['status'] == 'failed'
        assert batches.inspect(conn, batch_id)['status'] == 'failed'
        assert conn.execute('SELECT count(*) FROM source_record WHERE batch_id = %s', (batch_id,)).fetchone()[0] == 0
        path = write(tmp_path / 'publish-job.jsonl', [row('published', '发布后查重失败。')])
        batch_id = ingest.register_batch(conn, ADAPTERS['ccc_corpus'], [path])
        job = jobs.submit(conn, 'batch_resume', {'batch_id': batch_id}, 'tester', '发布后失败')

        def fail(conn, **kwargs):
            raise RuntimeError('dedup failed')

        monkeypatch.setattr(dedup, 'dedup', fail)
        jobs._run_next(conn, control)
        assert jobs.show(control, job['job_id'])['error'] == 'dedup failed'
        assert batches.inspect(conn, batch_id)['status'] == 'published'
        versions = conn.execute('SELECT version_id FROM document_version ORDER BY version_id').fetchall()
        retried = jobs.retry(conn, job['job_id'], 'tester', '不重复发布')
        jobs._run_next(conn, control)
        assert jobs.show(control, retried['job_id'])['result']['status'] == 'already_published'
        assert conn.execute('SELECT version_id FROM document_version ORDER BY version_id').fetchall() == versions


def test_jobs_dedup_cancel_rolls_back(conn, tmp_path, monkeypatch):
    path = write(tmp_path / 'dedup-cancel.jsonl', [row('rollback', '查重事务取消。')])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    conn.execute('DELETE FROM text_fingerprint')
    conn.commit()
    job_id = jobs.submit(conn, 'dedup', {'full': True}, 'tester', '查重取消')['job_id']
    handler = jobs.HANDLERS['dedup']
    with psycopg.connect(DSN, autocommit=True) as control:
        def cancelled(conn, params, actor, reason, progress):
            def boundary(current, total, message):
                if current == 4:
                    jobs.cancel(control, job_id, 'tester', '结果写入前取消')
                progress(current, total, message)
            return handler(conn, params, actor, reason, boundary)

        monkeypatch.setitem(jobs.HANDLERS, 'dedup', cancelled)
        jobs._run_next(conn, control)
        assert jobs.show(control, job_id)['status'] == 'cancelled'
        assert conn.execute('SELECT count(*) FROM text_fingerprint').fetchone()[0] == 0


def test_ingest_query_update_export(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    first = write(tmp_path / "a.jsonl", [
        row("a#1", "黄帝者，少典之子\r\n姓公孙"),
        row("a#2", "帝颛顼高阳者"),
        row("b#1", "项籍者，下相人也", chapter="汉书‧卷一", book="汉书"),
        "{坏行",
    ])
    report = ingest.ingest(conn, adapter, [first])
    assert (report.records_ok, report.records_failed) == (3, 1)
    assert report.new_documents == 3
    assert report.new_versions == 6
    assert conn.execute("SELECT count(*) FROM staging_text").fetchone()[0] == 0

    # 同内容文件不能再发布一次
    with pytest.raises(ingest.AlreadyImported):
        ingest.ingest(conn, adapter, [first])

    hits = query.search(conn, query.Filters(title_prefix="史记", year_from=100, year_to=150))
    assert [hit["year_from"] for hit in hits] == [-202, -202]
    assert query.search(conn, query.Filters(year_from=1000, year_to=1100)) == []
    document = query.get_text(conn, hits[0]["doc_id"])
    assert document["version_no"] == 2
    assert document["text"] == "黄帝者，少典之子\n姓公孙"
    assert query.get_text(conn, hits[0]["doc_id"], 1)["text"] == "黄帝者，少典之子\r\n姓公孙"

    recipe = export.Recipe.load({
        "name": "t1", "seed": "s", "splits": {"train": 1.0},
        "filters": {"sources": ["ccc_corpus"]},
    })
    release = export.create_release(conn, recipe)
    assert export.verify_release(conn, release)
    files = export.write_parquet(conn, release, tmp_path / "t1")
    table = pq.read_table(files[0])
    assert table.num_rows == 3
    assert sorted(table.column("ordinal").to_pylist()) == [0, 1, 2]
    stats = export.release_stats(conn, release)
    assert stats["char_count"] == sum(len(text) for text in table.column("text").to_pylist())
    assert stats["split_char_counts"] == {"train": stats["char_count"], "val": 0, "test": 0}

    # 同一来源的改动产生新版本，未变记录不动，已发布数据集不受影响
    second = write(tmp_path / "b.jsonl", [
        row("a#1", "黄帝者，少典之子，改"),
        row("a#2", "帝颛顼高阳者"),
        row("b#1", "项籍者，下相人也", chapter="汉书‧卷一", book="汉书"),
    ])
    report = ingest.ingest(conn, adapter, [second])
    assert report.unchanged == 2 and report.new_versions == 2 and report.new_documents == 0
    assert query.get_text(conn, hits[0]["doc_id"])["version_no"] == 4
    assert conn.execute(
        "SELECT count(*) FROM document_version WHERE is_current"
    ).fetchone()[0] == 3
    assert export.verify_release(conn, release)
    assert export.release_stats(conn, release) == stats

    # 同一作品的文档必须落在同一侧
    splits = conn.execute(
        """SELECT w.work_id, count(DISTINCT m.split)
           FROM dataset_member m
           JOIN document_version v USING (version_id)
           JOIN document_work w USING (doc_id)
           GROUP BY w.work_id"""
    ).fetchall()
    assert all(count == 1 for _, count in splits)


@pytest.mark.parametrize("splits", [
    {"train": 1.0}, {"val": 1.0}, {"test": 1.0},
    {"train": 0.4, "val": 0.3, "test": 0.3},
])
def test_release_char_counts(conn, tmp_path, splits):
    # 补充平面字、组合字符、全角空格和换行均按 Unicode 码点计数；重复成员分别计。
    texts = ["甲　乙，\n𠀋😀e\u0301", "甲　乙，\n𠀋😀e\u0301"]
    texts.extend(f"第{i}篇：古文。" for i in range(20))
    path = write(tmp_path / "chars.jsonl", [
        row(f"chars#{i}", text, book=f"书{i}") for i, text in enumerate(texts)
    ])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    release = export.create_release(conn, export.Recipe.load({
        "name": "chars", "seed": "chars", "splits": splits,
        "filters": {"quality": ["ok", "flagged"]},
    }))
    files = export.write_parquet(conn, release, tmp_path / "export")
    exported = []
    expected = {"train": 0, "val": 0, "test": 0}
    for path in files:
        values = pq.read_table(path).column("text").to_pylist()
        exported.extend(values)
        expected[path.stem.split('-')[0]] += sum(map(len, values))
    assert sorted(exported) == sorted(texts)
    assert all(expected[split] > 0 for split in splits)
    assert export.release_stats(conn, release) == {
        "release_id": release, "name": "chars",
        "char_count": sum(map(len, texts)), "split_char_counts": expected,
        "selection": {"candidates": len(texts), "dropped_documents": 0, "dropped_chars": 0},
    }
    assert export.verify_release(conn, release)
    with pytest.raises(export.ExportError, match="不存在"):
        export.release_stats(conn, release + 1)

    # 总数及划分和仍一致的统计错误也必须被 verify 发现。
    conn.execute(
        "UPDATE dataset_release SET char_count = char_count + 1,"
        " train_char_count = train_char_count + 1 WHERE release_id = %s", (release,),
    )
    assert not export.verify_release(conn, release)


def test_release_char_count_migration(conn, tmp_path):
    path = write(tmp_path / "old.jsonl", [row("old#1", "古文，\n𠀋"), row("old#2", "古文，\n𠀋")])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    releases = [export.create_release(conn, export.Recipe.load({
        "name": split, "seed": "old", "splits": {split: 1.0},
    })) for split in ("train", "val", "test")]
    before = [export.release_stats(conn, release) for release in releases]
    conn.commit()
    # 当前正文长度变化后，迁移仍须按旧数据集的固定版本回填。
    reprocess.reprocess(conn, reprocess.Rule(
        "chars-test-v1", None, lambda doc: reprocess.Change(text=doc["text"] + "新增"),
    ), dry_run=False)
    revision = runpy.run_path(str(SCHEMA.with_name("0005_release_char_count.py")))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(DSN))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision["downgrade"]()
                connection.exec_driver_sql(
                    "INSERT INTO dataset_release (name, recipe, content_digest)"
                    " VALUES ('empty', '{}', %s)", (export._digest([]),),
                )
                revision["upgrade"]()
    finally:
        engine.dispose()
    assert [export.release_stats(conn, release) for release in releases] == before
    assert all(export.verify_release(conn, release) for release in releases)
    empty = conn.execute("SELECT release_id FROM dataset_release WHERE name = 'empty'").fetchone()[0]
    assert export.release_stats(conn, empty)["split_char_counts"] == {"train": 0, "val": 0, "test": 0}
    assert export.release_stats(conn, empty)["char_count"] == 0
    assert export.verify_release(conn, empty)
    for update in ("char_count = -1", "char_count = char_count + 1",
                   "char_count = 0, train_char_count = -1, val_char_count = 1, test_char_count = 0"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(f"UPDATE dataset_release SET {update} WHERE release_id = %s", (empty,))


def test_training_history_blocks_held_out_work(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    path = write(tmp_path / "a.jsonl", [row("a#1", "甲文"), row("a#2", "乙文")])
    ingest.ingest(conn, adapter, [path])
    used = export.create_release(conn, export.Recipe.load({
        "name": "used", "seed": "s", "splits": {"train": 1.0},
    }))
    export.record_training(conn, "used", "run-1")
    assert used
    with pytest.raises(export.TrainingLeak):
        export.create_release(conn, export.Recipe.load({
            "name": "held", "seed": "s", "splits": {"val": 1.0},
        }))
    conn.rollback()


def test_failed_stage_leaves_no_partition(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    path = write(tmp_path / "a.jsonl", [row("a#1", "甲"), row("a#1", "乙")])
    with pytest.raises(psycopg.errors.UniqueViolation):
        ingest.ingest(conn, adapter, [path])
    conn.rollback()
    assert conn.execute(
        "SELECT status FROM source_batch ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 0


def test_manual_document_and_work_review(conn, tmp_path):
    path = write(tmp_path / "review.jsonl", [row("review#1", "甲乙丙")])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    doc_id, old_id, old_no = conn.execute(
        "SELECT doc_id, version_id, version_no FROM document_version WHERE is_current"
    ).fetchone()
    old_meta = conn.execute(
        "SELECT title, quality_status, quality_flags, field_basis, text_hash"
        " FROM document_version WHERE version_id = %s", (old_id,),
    ).fetchone()
    # 保留一个非当前派生版本；后续人工修正仍应以当前版为父版、取最大版本号。
    conn.execute(
        "INSERT INTO document_version"
        " (doc_id, version_no, parent_version_id, text_hash, run_id, is_current,"
        " quality_status, quality_flags, char_count, field_basis, extra)"
        " SELECT doc_id, version_no + 1, version_id, text_hash, run_id, false,"
        " quality_status, quality_flags, char_count, field_basis, extra"
        " FROM document_version WHERE version_id = %s", (old_id,),
    )
    conn.commit()
    result = management.review_document(
        conn, doc_id, old_id,
        {"title": "人工核定标题", "quality_status": "flagged", "quality_flags": ["人工待核"],
         "composed_years": "[618,908)"},
        actor="test-operator", reason="合成测试人工核对",
    )
    assert result["status"] == "applied" and result["version_no"] == old_no + 2
    assert result["changed_fields"] == [
        "composed_years", "quality_flags", "quality_status", "title",
    ]
    assert conn.execute(
        "SELECT title, quality_status, quality_flags, field_basis, text_hash"
        " FROM document_version WHERE version_id = %s", (old_id,),
    ).fetchone() == old_meta
    current = conn.execute(
        "SELECT parent_version_id, title, quality_status, quality_flags, field_basis, text_hash"
        " FROM document_version WHERE version_id = %s", (result["version_id"],),
    ).fetchone()
    assert current[0] == old_id and current[1:4] == (
        "人工核定标题", "flagged", ["人工待核"],
    )
    assert all(current[4][field] == "human" for field in result["changed_fields"])
    assert current[5] == old_meta[4]
    assert str(conn.execute(
        "SELECT composed_years FROM document_version WHERE version_id = %s",
        (result["version_id"],),
    ).fetchone()[0]) == "[618, 908)"
    with pytest.raises(management.ManagementError) as exc:
        management.review_document(
            conn, doc_id, old_id, {"title": "又改"}, actor="test", reason="冲突",
        )
    assert exc.value.code == "conflict"
    with pytest.raises(management.ManagementError) as exc:
        management.review_document(
            conn, doc_id, result["version_id"], {"title": "人工核定标题"},
            actor="test", reason="无变化",
        )
    assert exc.value.code == "no_change"
    with pytest.raises(management.ManagementError) as exc:
        management.review_document(
            conn, doc_id, result["version_id"], {"foo": "x"},
            actor="test", reason="非法字段",
        )
    assert exc.value.code == "invalid_input"
    work_id = conn.execute(
        "INSERT INTO work (canonical_title) VALUES ('待核作品') RETURNING work_id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO document_work (doc_id, work_id, relation, basis, status)"
        " VALUES (%s, %s, 'whole', 'rule', 'candidate')", (doc_id, work_id),
    )
    conn.commit()
    assert management.work_links(conn, doc_id, status="candidate") == [{
        "work_id": work_id, "canonical_title": "待核作品", "relation": "whole",
        "basis": "rule", "status": "candidate",
    }]
    decision = management.review_work(
        conn, doc_id, work_id, "whole", "confirmed",
        actor="test-operator", reason="依据源目录核对",
    )
    assert decision["decision"] == "confirmed"
    assert conn.execute(
        "SELECT basis, status FROM document_work WHERE doc_id = %s AND work_id = %s",
        (doc_id, work_id),
    ).fetchone() == ("rule", "confirmed")
    assert management.work_links(conn, doc_id, status="candidate") == []
    with pytest.raises(management.ManagementError) as exc:
        management.review_work(
            conn, doc_id, work_id, "whole", "rejected",
            actor="test", reason="状态冲突",
        )
    assert exc.value.code == "conflict"
    assert [event["action"] for event in management.events(conn, doc_id=doc_id)] == [
        "work_review", "document_review",
    ]


def test_batch_resume_and_abandon(conn, tmp_path, monkeypatch):
    adapter = ADAPTERS["ccc_corpus"]
    path = write(tmp_path / "resume.jsonl", [row("resume#1", "甲乙丙")])
    batch_id = ingest.register_batch(conn, adapter, [path])
    assert batches.inspect(conn, batch_id)["status"] == "received"
    # 原路径、大小、哈希任一变化都不能继续旧批次。
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    assert exc.value.code == "input_changed"
    assert batches.inspect(conn, batch_id)["status"] == "received"
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n", encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("甲", "乙"), encoding="utf-8")
    assert path.stat().st_size == len(original.encode("utf-8"))
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    assert exc.value.code == "input_changed"
    path.write_text(original, encoding="utf-8")
    conn.execute(
        "UPDATE source_batch SET parser_version = 'obsolete-parser' WHERE batch_id = %s",
        (batch_id,),
    )
    conn.commit()
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    assert exc.value.code == "not_recoverable"
    conn.execute(
        "UPDATE source_batch SET parser_version = %s WHERE batch_id = %s",
        (adapter.parser_version, batch_id),
    )
    conn.commit()
    # 发布后查重失败不会让恢复操作再次发布。
    real_dedup = batches.dedup.dedup
    monkeypatch.setattr(batches.dedup, "dedup", lambda _conn: (_ for _ in ()).throw(RuntimeError("dedup failed")))
    with pytest.raises(RuntimeError, match="dedup failed"):
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    monkeypatch.setattr(batches.dedup, "dedup", real_dedup)
    assert batches.inspect(conn, batch_id)["status"] == "published"
    count = conn.execute("SELECT count(*) FROM document_version").fetchone()[0]
    assert batches.resume(conn, batch_id, actor="test", reason="重试")["status"] == "already_published"
    assert conn.execute("SELECT count(*) FROM document_version").fetchone()[0] == count
    assert len(management.events(conn, batch_id=batch_id)) == 1

    bad = write(tmp_path / "failed.jsonl", [row("bad#1", "甲"), row("bad#1", "乙")])
    bad_id = ingest.register_batch(conn, adapter, [bad])
    with pytest.raises(psycopg.errors.UniqueViolation):
        batches.resume(conn, bad_id, actor="test", reason="尝试恢复")
    assert batches.inspect(conn, bad_id)["status"] == "failed"
    abandoned = batches.abandon(conn, bad_id, actor="test", reason="输入有重复键")
    assert abandoned["status"] == "abandoned"
    assert not batches.inspect(conn, bad_id)["has_partition"]
    assert batches.abandon(conn, bad_id, actor="test", reason="重复请求")["status"] == "already_abandoned"
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, bad_id, actor="test", reason="不可恢复")
    assert exc.value.code == "not_recoverable"
    with pytest.raises(management.ManagementError) as exc:
        batches.abandon(conn, batch_id, actor="test", reason="不可放弃")
    assert exc.value.code == "not_recoverable"


def test_batch_staged_validated_and_needs_review(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    good = write(tmp_path / "staged.jsonl", [row("staged#1", "甲乙丙")])
    staged_id = ingest.register_batch(conn, adapter, [good])
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        ingest.stage(conn, staged_id, adapter, pool)
    assert batches.inspect(conn, staged_id)["has_partition"]
    staged_view = batches.inspect(conn, staged_id)
    assert staged_view["staged"] == {"decoded": 1, "cleaned": 1}
    assert staged_view["validation_ready"] and staged_view["failure_samples"] == []
    assert batches.resume(conn, staged_id, actor="test", reason="恢复暂存批次")["status"] == "published"
    assert not batches.inspect(conn, staged_id)["has_partition"]

    validated = write(tmp_path / "validated.jsonl", [row("validated#1", "丁戊己")])
    validated_id = ingest.register_batch(conn, adapter, [validated])
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        ingest.stage(conn, validated_id, adapter, pool)
    assert ingest.validate(conn, validated_id) == "validated"
    assert batches.resume(conn, validated_id, actor="test", reason="恢复已验证批次")["status"] == "published"

    invalid = write(tmp_path / "review.jsonl", ["{坏行"])
    review_id = ingest.register_batch(conn, adapter, [invalid])
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        ingest.stage(conn, review_id, adapter, pool)
    assert ingest.validate(conn, review_id) == "needs_review"
    review_view = batches.inspect(conn, review_id)
    assert not review_view["validation_ready"]
    assert len(review_view["failure_samples"]) == 1
    assert batches.resume(conn, review_id, actor="test", reason="再次检查")["status"] == "needs_review"
    before = conn.execute(
        "SELECT count(*) FROM source_record WHERE batch_id = %s", (review_id,),
    ).fetchone()[0]
    assert before == 1
    assert batches.abandon(conn, review_id, actor="test", reason="无有效记录")["status"] == "abandoned"
    assert not batches.inspect(conn, review_id)["has_partition"]
    assert conn.execute(
        "SELECT count(*) FROM source_record WHERE batch_id = %s", (review_id,),
    ).fetchone()[0] == before


def test_reprocess(conn, tmp_path):
    path = write(tmp_path / "wanli.jsonl", [json.dumps({
        "id": "m1", "title": "神宗顯皇帝實錄 卷九",
        "raw_text": "朝埨朝埨:廣本抱本埨作。明史諸王表與館本同。永寧府",
    }, ensure_ascii=False)])
    ingest.ingest(conn, ADAPTERS["wanli"], [path])
    # 模拟旧质量规则发布的版本。
    conn.execute(
        "UPDATE document_version SET quality_status = 'ok', quality_flags = '{}' WHERE is_current"
    )
    conn.commit()

    def current():
        return conn.execute(
            "SELECT doc_id, version_id, version_no, parent_version_id, text_hash"
            " FROM document_version WHERE is_current"
        ).fetchone()

    def counts():
        return conn.execute(
            "SELECT (SELECT count(*) FROM document_version),"
            " (SELECT count(*) FROM processing_run), (SELECT count(*) FROM text_blob)"
        ).fetchone()

    original = current()
    before_counts = counts()
    quality = RULES["quality-v2"]
    preview = reprocess.reprocess(conn, quality, dry_run=True)
    assert preview["changed"] == 1
    assert counts() == before_counts and current() == original
    assert reprocess.reprocess(conn, quality)["changed"] == 1
    metadata_only = current()
    assert metadata_only[2] == original[2] + 1
    assert metadata_only[3] == original[1]
    assert metadata_only[4] == original[4]
    assert reprocess.reprocess(conn, quality)["changed"] == 0

    collation = RULES["wanli-collation-v1"]
    before_counts = counts()
    preview = reprocess.reprocess(conn, collation, dry_run=True)
    assert preview == reprocess.reprocess(conn, collation, dry_run=True)
    assert preview["changed"] == 1
    assert preview["samples"][0]["after"] == "朝埨永寧府"
    assert counts() == before_counts and current() == metadata_only
    assert reprocess.reprocess(conn, collation)["changed"] == 1
    updated = current()
    assert updated[2] == metadata_only[2] + 1
    assert updated[3] == metadata_only[1]
    assert updated[4] != metadata_only[4]
    document = query.get_text(conn, updated[0])
    assert document["text"] == "朝埨永寧府"
    assert document["quality_status"] == "ok" and document["quality_flags"] == []
    assert reprocess.reprocess(conn, collation)["changed"] == 0


def test_document_dedup(conn, tmp_path):
    rng = random.Random(762)
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    base = "齐" + "".join(rng.choices(alphabet, k=1000))
    texts = [base, "齊，" + base[1:] + "！", base[:500] + "山川" + base[502:],
             "".join(rng.choices(alphabet, k=1000)), "齊，國！", "齐国"]
    path = write(tmp_path / "dedup.jsonl", [row(f"d#{i}", text) for i, text in enumerate(texts)])
    first = ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path]).dedup
    # 本例只检查重复簇对导出的影响。
    conn.execute("DELETE FROM document_work")
    conn.commit()
    assert first["fingerprinted"] == 6
    assert first["clusters"] == 2 and first["members"] == 5
    members = conn.execute(
        """SELECT d.source_key, m.kind, v.is_current FROM dup_cluster_member m
           JOIN dup_cluster c USING (cluster_id) JOIN document_version v USING (version_id)
           JOIN document d USING (doc_id) WHERE c.run_id = %s ORDER BY d.source_key""",
        (first["run_id"],),
    ).fetchall()
    assert members == [("d#0", "exact", True), ("d#1", "exact", True),
                       ("d#2", "near_candidate", True), ("d#4", "exact", True),
                       ("d#5", "exact", True)]
    second = dedup.dedup(conn)
    assert second["fingerprinted"] == 0
    assert second["reused_snapshot"] and second["run_id"] == first["run_id"]
    assert second["clusters"] == first["clusters"]
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    before = export._group_keys(conn, list(docs.values()))
    assert before[docs["d#0"]] == before[docs["d#1"]] == before[docs["d#2"]]
    assert before[docs["d#4"]] == before[docs["d#5"]]
    replacements = {doc_id: "".join(rng.choices(alphabet, k=1000)) for doc_id in docs.values()}
    reprocess.reprocess(conn, reprocess.Rule(
        "dedup-test-v1", None, lambda doc: reprocess.Change(text=replacements[doc["doc_id"]])
    ))
    third = dedup.dedup(conn)
    assert third["clusters"] == 0 and third["fingerprinted"] == 6
    assert export._group_keys(conn, list(docs.values())) == {doc_id: doc_id for doc_id in docs.values()}
    release = export.create_release(conn, export.Recipe.load({
        "name": "dedup-latest", "seed": "s", "splits": {"train": 0.5, "val": 0.5},
    }))
    assert export.verify_release(conn, release)
    assert conn.execute(
        "SELECT count(*) FROM dup_cluster WHERE run_id = %s", (first["run_id"],)
    ).fetchone()[0] == 0


def test_export_selection_keeps_one_per_cluster(conn, tmp_path):
    rng = random.Random(763)
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    def text():
        return "".join(rng.choices(alphabet, k=1000))
    base = text()
    path = write(tmp_path / "selection.jsonl", [
        row("short", base, chapter="甲书‧卷一", book="甲书"),
        row("long", base + "！", chapter="乙书‧卷一", book="乙书"),
        row("solo", text(), chapter="丙书‧卷一", book="丙书"),
    ])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    conn.execute("INSERT INTO source (name) VALUES ('second')")
    second = conn.execute("SELECT source_id FROM source WHERE name = 'second'").fetchone()[0]
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    conn.execute("UPDATE document SET source_id = %s WHERE doc_id = %s", (second, docs["short"]))
    conn.commit()

    def release(name, selection):
        recipe = {"name": name, "seed": "s", "splits": {"train": 1.0}, "selection": selection}
        release_id = export.create_release(conn, export.Recipe.load(recipe))
        members = {doc for doc, in conn.execute(
            "SELECT v.doc_id FROM dataset_member m JOIN document_version v USING (version_id)"
            " WHERE m.release_id = %s", (release_id,))}
        return release_id, members

    _, everything = release("all", {})
    assert everything == set(docs.values())
    longest, members = release("longest", {"dedup": "document"})
    assert members == {docs["long"], docs["solo"]}
    assert export.release_stats(conn, longest)["selection"] == {
        "candidates": 3, "dropped_documents": 1, "dropped_chars": 1000}
    assert export.verify_release(conn, longest)
    _, members = release("preferred", {"dedup": "document", "source_priority": ["second"]})
    assert members == {docs["short"], docs["solo"]}
    with pytest.raises(export.ExportError):
        export.Recipe.load({"name": "bad", "seed": "s", "splits": {"train": 1.0},
                            "selection": {"dedup": "span"}})
    reprocess.reprocess(conn, reprocess.Rule(
        "selection-test-v1", None, lambda doc: reprocess.Change(text=text())))
    with pytest.raises(export.ExportError, match="已过期"):
        release("stale", {"dedup": "document"})


def test_export_selection_splits_with_dropped_bridge(conn, tmp_path):
    """被舍弃的文档仍要连接作品和重复簇，否则两侧会共享文本。"""
    rng = random.Random(764)
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    base = "".join(rng.choices(alphabet, k=1000))
    other = "".join(rng.choices(alphabet, k=1000))
    path = write(tmp_path / "bridge.jsonl", [
        row("kept", base + "！", chapter="甲书‧卷一", book="甲书"),
        row("bridge", base, chapter="乙书‧卷一", book="乙书"),
        row("work", other, chapter="乙书‧卷二", book="乙书"),
    ])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    groups = export._group_keys(conn, list(docs.values()))
    assert groups[docs["kept"]] == groups[docs["work"]]
    release = export.create_release(conn, export.Recipe.load({
        "name": "bridge", "seed": "s", "splits": {"train": 0.5, "val": 0.5},
        "selection": {"dedup": "document"}}))
    splits = conn.execute(
        "SELECT v.doc_id, m.split FROM dataset_member m JOIN document_version v USING (version_id)"
        " WHERE m.release_id = %s", (release,)).fetchall()
    assert {doc for doc, _ in splits} == {docs["kept"], docs["work"]}
    assert len({split for _, split in splits}) == 1


def test_span_dedup_pipeline(conn, tmp_path):
    rng = random.Random(762)
    shared = "，".join("".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=16)) for _ in range(8))
    texts = ["甲" * 91 + shared + "甲" * 79,
             "乙" * 37 + shared + "乙" * 43,
             "乙" * 37 + shared + "乙" * 43,
             "丙" * 150]
    path = write(tmp_path / "spans.jsonl", [row(f"s#{i}", text) for i, text in enumerate(texts)])
    report = ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    assert report.dedup_spans["rule_version"] == "span-v1"
    assert report.dedup_spans["representatives"] == 3
    assert report.dedup_spans["spans"] == 1
    versions = dict(conn.execute(
        "SELECT d.source_key, v.version_id FROM document d"
        " JOIN document_version v USING (doc_id) WHERE v.is_current"
    ))
    source, target = sorted((versions["s#0"], min(versions["s#1"], versions["s#2"])))
    starts = {versions["s#0"]: 91, versions["s#1"]: 37, versions["s#2"]: 37}

    def snapshot():
        return conn.execute(
            "SELECT run_id, version_id, start_char, end_char, source_version_id, source_start_char"
            " FROM dup_span ORDER BY version_id, start_char"
        ).fetchall()

    assert snapshot() == [(report.dedup_spans["run_id"], target, starts[target],
                           starts[target] + len(shared), source, starts[source])]
    first_run = report.dedup_spans["run_id"]
    second = dedup.dedup_spans(conn)
    assert second["compare_fingerprinted"] == 0 and second["spans"] == 1
    assert snapshot()[0][0] == second["run_id"] == first_run
    assert second["reused_snapshot"]
    assert conn.execute(
        "SELECT finished_at IS NOT NULL, config->>'spans' FROM processing_run WHERE run_id = %s",
        (second["run_id"],),
    ).fetchone() == (True, "1")
    # 文档查重独立运行，不覆盖段落快照。
    dedup.dedup(conn)
    assert snapshot()[0][0] == second["run_id"]
    reprocess.reprocess(conn, reprocess.Rule(
        "span-test-v1", None, lambda doc: reprocess.Change(text=str(doc["doc_id"]) * 40)
    ))
    assert dedup.dedup_spans(conn)["spans"] == 0
    assert snapshot() == []


def test_incremental_dedup_matches_full_and_preserves_unchanged_clusters(conn, tmp_path):
    rng = random.Random(762)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    a = "".join(rng.choices(alphabet, k=1200))
    b = "".join(rng.choices(alphabet, k=100)) + a[100:]
    c = b[:100] + "".join(rng.choices(alphabet, k=100)) + b[200:]
    unrelated = "".join(rng.choices(alphabet, k=1200))
    texts = [a, b, c, "甲乙", "甲乙", unrelated]

    def ingest_rows(name, rows):
        return ingest.ingest(conn, ADAPTERS["ccc_corpus"], [write(tmp_path / name, rows)])

    def snapshot():
        return conn.execute(
            "SELECT c.cluster_id, c.run_id, m.version_id, m.kind, m.xmin::text"
            " FROM dup_cluster_member m JOIN dup_cluster c USING (cluster_id)"
            " ORDER BY c.cluster_id, m.version_id"
        ).fetchall()

    def membership():
        clusters = {}
        for cid, _, version, kind, _ in snapshot():
            clusters.setdefault(cid, []).append((version, kind))
        return sorted(clusters.values())

    def assert_full_equivalent():
        before = snapshot()
        full = dedup.dedup(conn, full=True)
        assert snapshot() == before
        assert full["cached_pairs"] == 0
        assert full["clusters_changed"] == full["members_written"] == full["members_deleted"] == 0

    first = ingest_rows("initial.jsonl", [row(f"i#{i}", text) for i, text in enumerate(texts)])
    assert sorted(map(len, membership())) == [2, 3]
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    conn.execute("DELETE FROM document_work")
    conn.commit()
    # 最近一次运行是段落查重，导出仍须采用文档簇。
    assert first.dedup_spans["run_id"] > first.dedup["run_id"]
    groups = export._group_keys(conn, list(docs.values()))
    assert groups[docs["i#0"]] == groups[docs["i#1"]] == groups[docs["i#2"]]
    old_rows = snapshot()
    second = ingest_rows("increment.jsonl", [row("new", unrelated)]).dedup
    assert second["cached_pairs"] > 0 and second["computed_pairs"] == 0
    assert second["clusters_changed"] == 1 and second["members_written"] == 2
    assert set(old_rows).issubset(snapshot())  # 簇 ID、run_id、成员物理版本均不变。
    assert_full_equivalent()

    # 删除连接 A、C 的桥接文本 B，必须拆开旧簇。
    replacement = "".join(rng.choices(alphabet, k=1200))
    reprocess.reprocess(conn, reprocess.Rule(
        "replace-bridge", None,
        lambda doc: reprocess.Change(text=replacement) if doc["doc_id"] == docs["i#1"] else None,
    ))
    report = dedup.dedup(conn)
    assert report["members_deleted"] == 3
    assert sorted(map(len, membership())) == [2, 2]
    assert_full_equivalent()
    assert conn.execute(
        "SELECT count(*) FROM dup_cluster_member m JOIN document_version v USING (version_id)"
        " WHERE NOT v.is_current"
    ).fetchone()[0] == 0

    # 元数据改动会换 version_id，分数仍按比较正文复用。
    reprocess.reprocess(conn, reprocess.Rule(
        "metadata-only", None,
        lambda doc: reprocess.Change(metadata={"title": "修订标题"})
        if doc["doc_id"] == docs["i#0"] else None,
    ))
    report = dedup.dedup(conn)
    assert not report["reused_snapshot"] and report["computed_pairs"] == 0
    assert_full_equivalent()

    # UNLOGGED 缓存可清空；强制重算无需依赖缓存，也不重写未变簇。
    before = snapshot()
    conn.execute("TRUNCATE dedup_pair, text_fingerprint")
    conn.commit()
    rebuilt = dedup.dedup(conn, full=True)
    assert rebuilt["fingerprinted"] > 0 and rebuilt["computed_pairs"] > 0
    assert snapshot() == before
    # 移除全部当前版本，空集合也必须替换旧结果。
    conn.execute("UPDATE document_version SET is_current = false WHERE is_current")
    conn.commit()
    assert dedup.dedup(conn)["clusters"] == 0
    assert snapshot() == []
    assert dedup.dedup_spans(conn)["spans"] == 0


def test_dedup_failure_keeps_previous_snapshot(conn, tmp_path, monkeypatch):
    path = write(tmp_path / "a.jsonl", [row("a", "甲" * 100), row("b", "甲" * 100)])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    before = conn.execute("SELECT * FROM dedup_snapshot ORDER BY level").fetchall()
    members = conn.execute("SELECT * FROM dup_cluster_member ORDER BY cluster_id, version_id").fetchall()
    pairs = conn.execute("SELECT count(*) FROM dedup_pair").fetchone()[0]
    reprocess.reprocess(conn, reprocess.Rule(
        "fail-test", None,
        lambda doc: reprocess.Change(text="天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏" * 4 + str(doc["doc_id"])),
    ))
    original = dedup._write_clusters

    def fail(*args):
        original(*args)
        raise RuntimeError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(dedup, "_write_clusters", fail)
        with pytest.raises(RuntimeError, match="simulated"):
            dedup.dedup(conn)
    assert conn.execute("SELECT * FROM dedup_snapshot ORDER BY level").fetchall() == before
    assert conn.execute("SELECT * FROM dup_cluster_member ORDER BY cluster_id, version_id").fetchall() == members
    assert conn.execute("SELECT count(*) FROM dedup_pair").fetchone()[0] == pairs
    recovered = dedup.dedup(conn)
    assert not recovered["reused_snapshot"] and recovered["computed_pairs"] == 1


def test_dedup_migration_and_parameter_changes(conn, tmp_path, monkeypatch):
    rng = random.Random(762)
    base = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=1000))
    path = write(tmp_path / "versions.jsonl", [row("a", base), row("b", base[:-1] + "甲")])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    before = conn.execute("SELECT cluster_id, run_id FROM dup_cluster").fetchall()
    conn.commit()
    revision = runpy.run_path(str(SCHEMA.with_name("0006_incremental_dedup.py")))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(DSN))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision["downgrade"]()
                revision["upgrade"]()
    finally:
        engine.dispose()
    assert conn.execute("SELECT cluster_id, run_id FROM dup_cluster").fetchall() == before
    restored = dedup.dedup(conn)
    assert restored["computed_pairs"] == 1 and restored["clusters_changed"] == 0
    assert restored["members_written"] == 0
    with monkeypatch.context() as patch:
        patch.setattr(dedup, "THRESHOLD", 1.0)
        strict = dedup.dedup(conn)
        assert not strict["reused_snapshot"] and strict["clusters"] == 0
        assert strict["cached_pairs"] == 1 and strict["computed_pairs"] == 0
    assert dedup.dedup(conn)["clusters"] == 1
    with monkeypatch.context() as patch:
        patch.setattr(dedup, "RULE_VERSION", "compare-test-v2")
        changed_rule = dedup.dedup(conn)
        assert changed_rule["fingerprinted"] == 2 and changed_rule["computed_pairs"] == 1
        assert changed_rule["cached_pairs"] == 0


def test_web_api_real_versions(conn, tmp_path):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection
    path = write(tmp_path / 'web.jsonl', [row('web#1', '甲篇正文\r\n　第二段，保留标点。', chapter='测试文献·甲篇'), row('web#2', '乙篇正文。', chapter='测试文献·乙篇')])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    app = create_app()
    app.dependency_overrides[connection] = lambda: conn
    with TestClient(app) as client:
        assert client.get('/api/v1/sources').json() == [{'name':'ccc_corpus'}]
        first = client.get('/api/v1/documents?limit=1').json()
        doc = first['items'][0]['doc_id']
        assert first['next_cursor'] == doc
        assert client.get(f'/api/v1/documents?after={doc}&limit=1').json()['next_cursor'] is None
        versions = client.get(f'/api/v1/documents/{doc}/versions').json()
        assert [v['version_no'] for v in versions] == [2,1]
        assert versions[0]['is_current']
        assert '\r\n' in client.get(f'/api/v1/documents/{doc}?version=1').json()['text']
        assert '\r' not in client.get(f'/api/v1/documents/{doc}?version=2').json()['text']


def test_web_metadata_stats_review_and_batches(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection, write_connection
    from midstream import textcodec
    path = write(tmp_path / 'meta-web.jsonl', [row('web-review', '甲篇正文。')])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    conn.execute("INSERT INTO source (name) VALUES ('empty_fixture')")
    conn.commit()
    release = export.create_release(conn, export.Recipe.load({
        'name':'web-stats-fixture','seed':'s','splits':{'train':1.0}}))
    app = create_app()
    app.dependency_overrides[connection] = lambda: conn
    app.dependency_overrides[write_connection] = lambda: conn
    client = TestClient(app)
    headers = {'X-Midstream-Action':'1','Origin':'http://testserver'}
    doc = client.get('/api/v1/documents').json()['items'][0]['doc_id']
    def no_text(*args):
        raise AssertionError('Metadata requests must not decompress text')
    with monkeypatch.context() as patch:
        patch.setattr(textcodec, 'decompress', no_text)
        metadata = client.get(f'/api/v1/documents/{doc}/metadata').json()
    assert 'text' not in metadata and metadata['source'] == 'ccc_corpus'
    assert metadata['document']['source_key'] == 'web-review'
    assert metadata['source_records'][0]['path'] == str(path)
    assert metadata['processing']['batch_id'] is not None
    stats = client.get('/api/v1/stats').json()
    assert stats['current'] == {'documents':1,'characters':len('甲篇正文。'),'quarantined':0}
    assert next(s for s in stats['sources'] if s['source']=='empty_fixture')['versions'] == 0
    assert stats['releases'][0]['release_id'] == release
    payload = {'actor':'fixture reviewer','reason':'correct title','expected_version_id':metadata['version_id'],
               'metadata':{'title':'已核对标题','quality_status':'flagged','quality_flags':['manual_check']}}
    saved = client.post(f'/api/v1/documents/{doc}/review',json=payload,headers=headers)
    assert saved.status_code == 200 and saved.json()['version_no'] == 3
    assert client.post(f'/api/v1/documents/{doc}/review',json=payload,headers=headers).status_code == 409
    old = client.get(f'/api/v1/documents/{doc}/metadata?version=2').json()
    current = client.get(f'/api/v1/documents/{doc}/metadata').json()
    assert old['title'] == metadata['title'] and current['title'] == '已核对标题'
    assert current['field_basis']['title'] == 'human'
    assert export.release_stats(conn,release)['char_count'] == stats['releases'][0]['char_count']
    invalid = {**payload,'expected_version_id':current['version_id'],'metadata':{'composed_years':'bad range'}}
    assert client.post(f'/api/v1/documents/{doc}/review',json=invalid,headers=headers).status_code == 422
    assert client.get(f'/api/v1/documents/{doc}/metadata').json()['version_id'] == current['version_id']
    work = client.get(f'/api/v1/documents/{doc}/works').json()[0]
    decision = {k:work[k] for k in ('work_id','relation')}
    decision.update(decision='rejected',expected_status=work['status'],actor='fixture reviewer',reason='wrong work')
    assert client.post(f'/api/v1/documents/{doc}/works/review',json=decision,headers=headers).status_code == 200
    events = client.get(f'/api/v1/events?doc_id={doc}').json()
    assert len(events) == 2 and events[0]['action']=='work_review'
    new = write(tmp_path / 'resume-web.jsonl',[row('new-web','乙篇正文。')])
    batch = ingest.register_batch(conn,ADAPTERS['ccc_corpus'],[new])
    assert client.get(f'/api/v1/batches/{batch}').json()['status']=='received'
    operation={'actor':'fixture reviewer','reason':'continue checked source'}
    resumed=client.post(f'/api/v1/batches/{batch}/resume',json=operation,headers=headers)
    assert resumed.status_code==200 and resumed.json()['status']=='published'
    assert client.post(f'/api/v1/batches/{batch}/resume',json=operation,headers=headers).json()['status']=='already_published'
    assert client.post(f'/api/v1/batches/{batch}/abandon',json=operation,headers=headers).status_code==409
    unused=write(tmp_path/'abandon-web.jsonl',[row('abandon-web','丙篇正文。')])
    abandoned=ingest.register_batch(conn,ADAPTERS['ccc_corpus'],[unused])
    assert client.post(f'/api/v1/batches/{abandoned}/abandon',json=operation,headers=headers).json()['status']=='abandoned'
    assert unused.exists()
    listing=client.get('/api/v1/batches?include_published=true&limit=1').json()
    assert listing['items'][0]['batch_id']==abandoned and listing['next_cursor']==abandoned
    older=client.get(f'/api/v1/batches?include_published=true&before={abandoned}&limit=1').json()
    assert older['items'][0]['batch_id']==batch


def test_web_year_granularities_and_unknowns(conn, tmp_path):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection
    from midstream import management
    path=write(tmp_path/'year-web.jsonl',[row(f'year-{i}', f'合成文献第{i}篇。') for i in range(5)])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    current=conn.execute('SELECT doc_id,version_id FROM document_version WHERE is_current ORDER BY doc_id').fetchall()
    ranges=['[-50,51)','[601,701)',None,'[-100,-99)','[-101,-100)']
    for i,((doc,version),years) in enumerate(zip(current,ranges)):
        management.review_document(conn,doc,version,{'composed_years':years,'composed_label':f'合成时代{i}'},actor='fixture',reason='year fixture')
    app=create_app()
    app.dependency_overrides[connection]=lambda:conn
    c=TestClient(app)
    stats=c.get('/api/v1/stats/years?granularity=century').json()
    assert stats['unknown_years']==1 and stats['known_years']==4
    counts={v['label']:v['documents'] for v in stats['items']}
    assert counts['公元前2世纪']==1 and counts['公元前1世纪']==2
    assert counts['1世纪']==1 and counts['7世纪']==1
    assert sum(counts.values())==5
    selected=c.get('/api/v1/documents?year_from=601&year_to=700').json()['items']
    assert [v['doc_id'] for v in selected]==[current[1][0]]
    assert len(c.get('/api/v1/documents?year_from=601&year_to=700&include_unknown_years=true').json()['items'])==2
    unknown=c.get('/api/v1/documents?only_unknown_years=true').json()['items']
    assert [v['doc_id'] for v in unknown]==[current[2][0]]
    assert len(c.get('/api/v1/documents?era=合成时代0').json()['items'])==1
    labels=c.get('/api/v1/stats/years?granularity=dynasty').json()
    assert sum(v['documents'] for v in labels['items'])==5 and not labels['overlapping']
    assert len(c.get('/api/v1/years/options').json()['labels'])==5
    years=c.get('/api/v1/stats/years?granularity=year').json()
    assert years['windowed'] and years['year_from']==601 and years['year_to']==700
    exact=c.get('/api/v1/stats/years?granularity=year&year_from=-101&year_to=-100').json()
    assert [v['documents'] for v in exact['items']]==[1,1]
    for url in ('documents?year_from=0','documents?year_from=1&only_unknown_years=true',
                'stats/years?granularity=year&year_from=-1000&year_to=1000',
                'stats/years?granularity=dynasty&year_from=1'):
        assert c.get('/api/v1/'+url).status_code==422


def test_mapped_import_plan_resume(conn, tmp_path):
    from midstream import importing
    path = write(tmp_path / 'mapped.jsonl', [json.dumps({'id': '1', 'body': '天地玄黄', 'name': '测试'})])
    plan = {'format': 'jsonl', 'text_field': 'body', 'id_field': 'id', 'fields': {'title': 'name'}}
    preview = importing.preview('mapped_fixture', [str(path)], plan)
    registered = importing.register(conn, 'mapped_fixture', [str(path)], preview['objects'],
        preview['parser_version'], 'tester', 'fixture import', plan)
    batch_id = registered['batch_id']
    assert conn.execute('SELECT import_plan FROM source_batch WHERE batch_id=%s', (batch_id,)).fetchone()[0]['text_field'] == 'body'
    result = batches.resume(conn, batch_id, actor='tester', reason='fixture publish')
    assert result['status'] == 'published'
    hits = query.search(conn, query.Filters(sources=['mapped_fixture']))
    assert hits[0]['title'] == '测试'
    assert query.get_text(conn, hits[0]['doc_id'])['text'] == '天地玄黄'
