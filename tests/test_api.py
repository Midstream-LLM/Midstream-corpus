from fastapi.testclient import TestClient
from midstream import api


def client(monkeypatch):
    app = api.create_app()
    app.dependency_overrides[api.connection] = lambda: None
    return TestClient(app)


def test_search_pagination_and_validation(monkeypatch):
    def search(conn, filters, limit, after_doc):
        assert filters.sources == ['fixture']
        assert filters.title_prefix == '甲'
        assert after_doc == 10 and limit == 3
        return [{'doc_id': 11}, {'doc_id': 12}, {'doc_id': 13}]
    monkeypatch.setattr(api.query, 'search', search)
    c = client(monkeypatch)
    response = c.get('/api/v1/documents?source=fixture&title=甲&after=10&limit=2')
    assert response.json() == {'items': [{'doc_id':11},{'doc_id':12}], 'next_cursor':12}
    for query in ('limit=0','after=-1','quality=invalid','year_from=200&year_to=100'):
        assert c.get('/api/v1/documents?'+query).status_code == 422


def test_version_content_and_missing(monkeypatch):
    def get_text(conn, doc_id, version):
        if doc_id == 404:
            return None
        assert version == 2
        return {'doc_id':doc_id,'version_no':2,'text':'甲𠀋　乙\n丙','char_count':6}
    monkeypatch.setattr(api.query,'get_text',get_text)
    c=client(monkeypatch)
    data=c.get('/api/v1/documents/1?version=2&offset=1&length=3').json()
    assert data['text']=='𠀋　乙' and data['next_offset']==4
    assert c.get('/api/v1/documents/404').status_code==404
    assert c.get('/api/v1/documents/1?length=20001').status_code==422
    assert c.get('/api/unknown').status_code==404
    assert c.get('/api/v1/health').json()['api_version']=='1'
    assert c.get('/api/openapi.json').status_code==200
