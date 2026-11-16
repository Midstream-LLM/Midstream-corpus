import json
import pytest
from fastapi.testclient import TestClient
from midstream import api
from midstream.ingest import importing, pipeline as ingest
from midstream.corpus.management import ManagementError


def test_preview_and_changed_input(tmp_path):
    path = tmp_path / 'sample.jsonl'
    path.write_text(json.dumps({'id': 'a', 'content': '甲乙', 'source': '测试'}) + '\n')
    result = importing.preview('ccc_corpus', [str(path)])
    assert result['samples'][0]['text_preview'] == '甲乙'
    assert result['samples'][0]['metadata']['title'] == '测试'
    path.write_text('changed')
    with pytest.raises(ManagementError, match='重新预览'):
        importing.register(None, 'ccc_corpus', [str(path)], result['objects'],
                           result['parser_version'], 'tester', 'test')
    for names in ([str(path), str(path)], ['relative.jsonl'], [str(tmp_path)]):
        with pytest.raises(ManagementError):
            importing.inputs('ccc_corpus', names)


def test_register_audit_transaction_and_duplicate(monkeypatch, tmp_path):
    path = tmp_path / 'sample.jsonl'
    path.write_text('{}')
    _, _, objects = importing.inputs('ccc_corpus', [str(path)])
    calls = []
    class Conn:
        def commit(self): calls.append('commit')
    def register(conn, adapter, paths, *, commit):
        assert commit is False
        calls.append('register')
        return 42
    monkeypatch.setattr(ingest, 'register_batch', register)
    monkeypatch.setattr(importing, '_event', lambda *args: calls.append('audit'))
    args = (Conn(), 'ccc_corpus', [str(path)], objects, 'ccc-corpus-v2', 'tester', 'test')
    assert importing.register(*args)['batch_id'] == 42
    assert calls == ['register', 'audit', 'commit']
    def duplicate(*args, **kwargs): raise ingest.AlreadyImported('already imported')
    monkeypatch.setattr(ingest, 'register_batch', duplicate)
    with pytest.raises(ManagementError) as e:
        importing.register(*args)
    assert e.value.code == 'conflict'


def test_import_http_boundary(tmp_path):
    c = TestClient(api.create_app())
    assert len(c.get('/api/v1/import/adapters').json()) == 2
    body = {'source': 'ccc_corpus', 'paths': [str(tmp_path / 'missing')]}
    assert c.post('/api/v1/import/preview', json=body).status_code == 403
    headers = {'X-Midstream-Action': '1'}
    assert c.post('/api/v1/import/preview', json=body, headers=headers).status_code == 422
    body['paths'] = []
    assert c.post('/api/v1/import/preview', json=body, headers=headers).status_code == 422


def test_known_detection_never_calls_llm(tmp_path, monkeypatch):
    from midstream.ingest import detection as import_detection
    path = tmp_path / 'ccc.jsonl'
    path.write_text(json.dumps({'id': 'a', 'content': '甲乙', 'source': '测试', 'chapter': '一'}) + '\n')
    def forbidden(*args): raise AssertionError('known format must not call LLM')
    monkeypatch.setattr(import_detection.LLMServices, 'from_config', forbidden)
    result = import_detection.detect([str(path)], True)['items'][0]
    assert result['adapter'] == 'ccc_corpus'
    assert '_sample' not in result


def test_mapped_formats_and_metadata(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from midstream.ingest.adapters.mapped import MappedAdapter
    for fmt in ('json', 'jsonl', 'parquet'):
        path = tmp_path / ('input.' + fmt)
        row = {'body': ['甲', '乙'], 'name': '标题', 'writer': '作者'}
        if fmt == 'parquet': pq.write_table(pa.Table.from_pylist([row]), path)
        else: path.write_text(json.dumps([row] if fmt == 'json' else row))
        adapter = MappedAdapter('fixture', {'format': fmt, 'text_field': 'body', 'fields': {'title': 'name', 'authors': 'writer'}})
        record = next(adapter.parse(path))
        assert record.text == '甲\n乙'
        assert record.metadata['authors'] == ['作者']
        assert 'body' not in record.raw_fields
    path = tmp_path / 'text.txt'
    path.write_text('甲乙', encoding='utf-16')
    adapter = MappedAdapter('fixture', {'format': 'txt', 'encoding': 'utf-16'})
    assert next(adapter.parse(path)).text == '甲乙'


def test_unknown_detection_llm_sample_and_null(tmp_path, monkeypatch):
    from midstream.ingest import detection as import_detection
    path = tmp_path / 'unknown.jsonl'
    path.write_text(json.dumps({'body': '甲' * 10000}) + '\n')
    class Service:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def identify_import(self, sample, **kwargs):
            assert set(sample) == {'format', 'encoding', 'sample', 'file_name', 'draft'}
            assert len(sample['sample']) <= 6000
            return {'plan': None, 'note': 'cannot map', 'method': 'llm', 'model': 'fixture'}
    monkeypatch.setattr(import_detection.LLMServices, 'from_config', Service)
    result = import_detection.detect([str(path)], True)['items'][0]
    assert result['plan'] is None and result['method'] == 'llm'


def test_plan_rules_and_detection_ladder(tmp_path):
    import json
    from midstream.ingest import detection
    from midstream.ingest.adapters.mapped import MappedAdapter
    book = tmp_path / '明_小说_某甲_某书.txt'
    book.write_text('Title: 书\n*** START ***\n正文\n*** END ***\n尾')
    plan = dict(format='txt', constants={'license': 'cc0-1.0'}, trim_start=r'\*\*\* START \*\*\*\n', trim_end=r'\n\*\*\* END',
                path_pattern=r'/(?P<composed_label>[^_/]+)_(?P<category>[^_/]+)_(?P<authors>[^_/]+)_(?P<title>[^/]+)\.txt$')
    record = next(MappedAdapter('x', plan).parse(book))
    assert record.text == '正文' and record.metadata['authors'] == ['某甲']
    assert record.metadata['field_basis'] == {'license': 'import_plan', 'title': 'path', 'composed_label': 'path',
                                              'category': 'path', 'authors': 'path'}
    nested = tmp_path / 'n.json'
    nested.write_text(json.dumps({'title': '集', 'content': [{'chapter': '篇', 'paragraphs': ['甲', '乙']}]}))
    plan = dict(format='json', text_field='paragraphs|content', fields={'title': 'chapter|title', 'work_title': '_parent.title'})
    record = next(MappedAdapter('x', plan).parse(nested))
    assert (record.text, record.metadata['title'], record.metadata['work_title']) == ('甲\n乙', '篇', '集')
    # 未知来源先得到规则草稿；确认过的方案下次直接复用。
    found = detection.detect_one(book)
    assert not found['known'] and found['method'] == 'inferred' and 'composed_label' in found['plan']['path_pattern']
    saved = dict(found['plan'], path_pattern=plan and r'/(?P<title>[^/]+)\.txt$')
    found = detection.detect_one(book, [('mine', 7, saved, [])])
    assert found['known'] and found['source'] == 'mine' and found['method'] == 'learned'
