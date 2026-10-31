import json
import pytest
from fastapi.testclient import TestClient
from midstream import api, importing, ingest
from midstream.management import ManagementError


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
    args = (Conn(), 'ccc_corpus', [str(path)], objects, 'ccc-corpus-v1', 'tester', 'test')
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
    from midstream import import_detection
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
    from midstream.adapters.mapped import MappedAdapter
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
    from midstream import import_detection
    path = tmp_path / 'unknown.jsonl'
    path.write_text(json.dumps({'body': '甲' * 10000}) + '\n')
    class Service:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def identify_import(self, sample, **kwargs):
            assert set(sample) == {'format', 'encoding', 'sample'}
            assert len(sample['sample']) <= 6000
            return {'plan': None, 'note': 'cannot map', 'method': 'llm', 'model': 'fixture'}
    monkeypatch.setattr(import_detection.LLMServices, 'from_config', Service)
    result = import_detection.detect([str(path)], True)['items'][0]
    assert result['plan'] is None and result['method'] == 'llm'
