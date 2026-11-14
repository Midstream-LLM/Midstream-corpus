import json
import threading
import time
from uuid import uuid4
from pathlib import Path

import pytest

from midstream.ingest import discovery, importing
from midstream.api.ingest import ImportStart
from midstream.corpus.management import ManagementError


class Rows:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchall(self):
        return self.rows


class ReadConnection:
    def __init__(self, rows=()):
        self.rows = rows

    def execute(self, sql, args=None):
        return Rows(self.rows)


def test_browse_paging_and_no_symlink_traversal(tmp_path):
    (tmp_path / 'folder').mkdir()
    (tmp_path / 'file.txt').write_text('text')
    (tmp_path / '.hidden').write_text('hidden')
    (tmp_path / 'link').symlink_to(tmp_path / 'folder')
    page = discovery.browse(str(tmp_path), limit=1)
    assert page['total'] == 2
    assert page['items'][0]['directory']
    assert page['next_offset'] == 1
    assert discovery.browse(str(tmp_path), offset=1)['items'][0]['name'] == 'file.txt'
    with pytest.raises(ManagementError):
        discovery.start([str(tmp_path / 'link')])
    with pytest.raises(ManagementError):
        discovery.browse('relative')


def test_scan_fingerprints_nested_fields_and_cancellation(tmp_path):
    path = tmp_path / 'one.jsonl'
    path.write_text(json.dumps({'meta': {'title': '甲'}, 'body': ['乙', '丙']}))
    stop = threading.Event()
    for rows, expected in [
        ([], 'new'),
        ([(1, 'published', 'old', False)], 'changed'),
        ([(2, 'received', 'old', True)], 'pending'),
        ([(3, 'published', 'old', True)], 'imported'),
    ]:
        item = discovery.inspect_file(path, ReadConnection(rows), stop)
        assert item['import_status'] == expected
        assert set(item['fields']) == {'meta.title', 'body'}
        assert len(item['sha256']) == 64
    stop.set()
    with pytest.raises(InterruptedError):
        discovery.inspect_file(path, ReadConnection(), stop)


def test_recursive_scanning_group_inputs_and_partial_results(tmp_path, monkeypatch):
    (tmp_path / 'a.txt').write_text('甲')
    child = tmp_path / 'child'
    child.mkdir()
    (child / 'b.txt').write_text('甲')
    (child / 'c.txt').write_text('乙')
    (child / 'loop').symlink_to(tmp_path)

    class Conn(ReadConnection):
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(discovery.psycopg, 'connect', lambda *a, **kw: Conn())
    for recursive, expected in [(False, 1), (True, 3)]:
        scan = discovery.start([str(tmp_path)], recursive)
        for _ in range(100):
            result = discovery.get(scan['scan_id'])
            if result['status'] != 'running':
                break
            time.sleep(.01)
        assert result['status'] == 'completed'
        assert result['found'] == expected
        if recursive:
            assert [i['import_status'] for i in result['items']].count('duplicate') == 1
        assert discovery.get(scan['scan_id'], 1)['items'] == result['items'][1:]


def test_atomic_start_retry_and_changed_payload(monkeypatch):
    from midstream import jobs
    calls = []
    saved = {}
    class One:
        def __init__(self, row=None): self.row = row
        def fetchone(self): return self.row
    class Conn:
        def execute(self, sql, params):
            if sql.startswith('SELECT after_state'):
                return One((saved['state'],) if 'state' in saved else None)
            if sql.startswith('UPDATE management_event'):
                saved['state'] = params[0].obj
            return One()
        def commit(self): calls.append('commit')
    def register(*args, **kw):
        assert kw['commit'] is False
        calls.append('register')
        return {'batch_id': 42}
    monkeypatch.setattr(importing, 'register', register)
    monkeypatch.setattr(jobs, '_insert', lambda *args: calls.append('queue') or 8)
    body = ImportStart(source='test', paths=['/tmp/test.txt'],
                       objects=[{'path': '/tmp/test.txt'}], parser_version='v1',
                       actor='operator', reason='selected files', request_token=uuid4(),
                       approval={'method': 'llm', 'model': 'fixture', 'requested_by': 'caller'})
    assert importing.start_import(Conn(), body) == {'batch_id': 42, 'job_id': 8}
    assert importing.start_import(Conn(), body) == {'batch_id': 42, 'job_id': 8}
    assert calls == ['register', 'queue', 'commit']
    assert saved['state']['approval']['requested_by'] == 'caller'
    assert saved['state']['approved_by'] == 'operator'
    with pytest.raises(ManagementError, match='重新预览'):
        importing.start_import(Conn(), body.model_copy(update={'source': 'changed'}))


def test_discovery_http_requires_same_origin_action_header(tmp_path):
    from fastapi.testclient import TestClient
    from midstream.api import create_app
    client = TestClient(create_app())
    assert client.post('/api/v1/import/browse', json={'path': str(tmp_path)}).status_code == 403
    headers = {'X-Midstream-Action': '1'}
    assert client.post('/api/v1/import/browse', json={'path': str(tmp_path)}, headers=headers).status_code == 200
    assert client.post('/api/v1/import/scans', json={'paths': []}, headers=headers).status_code == 422
    assert client.post('/api/v1/import/scans', json={'paths': [str(tmp_path)]},
                       headers=headers | {'Origin': 'https://other.example'}).status_code == 403


def test_cancel_keeps_completed_files(tmp_path, monkeypatch):
    (tmp_path / 'a.txt').write_text('甲')
    (tmp_path / 'b.txt').write_text('乙')
    waiting = threading.Event()
    original = discovery.inspect_file
    def inspect(path, conn, stop):
        if path.name == 'b.txt':
            waiting.set()
            assert stop.wait(2), 'cancel was not delivered'
            raise InterruptedError()
        return original(path, conn, stop)
    class Conn(ReadConnection):
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(discovery.psycopg, 'connect', lambda *a, **kw: Conn())
    monkeypatch.setattr(discovery, 'inspect_file', inspect)
    scan = discovery.start([str(tmp_path)])
    assert waiting.wait(2)
    discovery.get(scan['scan_id'], cancel=True)
    for _ in range(100):
        result = discovery.get(scan['scan_id'])
        if result['status'] != 'running':
            break
        time.sleep(.01)
    assert result['status'] == 'cancelled'
    assert [Path(i['path']).name for i in result['items']] == ['a.txt']


def test_start_queue_failure_does_not_commit(monkeypatch):
    from midstream import jobs
    committed = []
    class Conn:
        def execute(self, *args): return self
        def fetchone(self): return None
        def commit(self): committed.append(True)
    monkeypatch.setattr(importing, 'register', lambda *a, **kw: {'batch_id': 12})
    def fail(*args): raise ValueError('queue unavailable')
    monkeypatch.setattr(jobs, '_insert', fail)
    body = ImportStart(source='test', paths=['/tmp/test.txt'], objects=[{}],
                       parser_version='v1', actor='operator', reason='selected files',
                       request_token=uuid4())
    with pytest.raises(ValueError, match='queue unavailable'):
        importing.start_import(Conn(), body)
    assert not committed
