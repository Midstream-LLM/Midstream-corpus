import json
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from midstream import api
from midstream.llm import LLMClient, LLMConfig
from midstream.llm.settings import get, save, SettingsError


def config():
    return {'credentials': {'test': 'MIDSTREAM_SETTINGS_TEST_KEY'},
            'endpoints': {'chat': {'protocol': 'chat_completions', 'base_url': 'https://example.test/v1', 'credential': 'test'}},
            'profiles': {'default': {'endpoint': 'chat', 'model': 'test-model', 'parameters': {'thinking': {'type': 'disabled'}, 'temperature': 0.2, 'stop': ['甲', '乙']}}},
            'services': {'import_detection': 'default'}}


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = tmp_path / 'llm.toml'
    monkeypatch.setenv('MIDSTREAM_LLM_CONFIG', str(path))
    monkeypatch.delenv('MIDSTREAM_SETTINGS_TEST_KEY', raising=False)
    return path


def test_atomic_save_secret_roundtrip_and_conflict(path, monkeypatch):
    before = get()
    assert not before['configured']
    saved = save(config(), {'test': 'synthetic-local-secret'}, [], before['revision'])
    assert 'synthetic-local-secret' not in json.dumps(saved)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert LLMConfig.load().key('test') == 'synthetic-local-secret'
    assert LLMConfig.load().profiles['default'].parameters['stop'] == ['甲', '乙']
    assert saved['credentials_status']['test']['source'] == 'local'
    with pytest.raises(SettingsError) as exc:
        save(config(), {}, [], before['revision'])
    assert exc.value.status == 409
    saved = save(saved['config'], {}, [], saved['revision'])
    assert LLMConfig.load().key('test') == 'synthetic-local-secret'
    monkeypatch.setenv('MIDSTREAM_SETTINGS_TEST_KEY', 'synthetic-env-secret')
    assert LLMConfig.load().key('test') == 'synthetic-env-secret'
    assert get()['credentials_status']['test']['source'] == 'environment'
    save(saved['config'], {}, ['test'], saved['revision'])
    assert not LLMConfig.load().local_keys


def test_invalid_save_preserves_original_and_no_key_echo(path):
    saved = save(config(), {}, [], get()['revision'])
    original = path.read_bytes()
    invalid = config()
    invalid['profiles']['default']['endpoint'] = 'missing'
    with pytest.raises(SettingsError):
        save(invalid, {'test': 'do-not-echo'}, [], saved['revision'])
    assert path.read_bytes() == original
    c = TestClient(api.create_app())
    body = {'config':config(), 'revision':saved['revision'], 'keys':['do-not-echo']}
    response = c.post('/api/v1/llm/settings',json=body,headers={'X-Midstream-Action':'1'})
    assert response.status_code == 422
    assert 'do-not-echo' not in response.text
    assert c.post('/api/v1/llm/settings',json={'config':config(),'revision':saved['revision']}).status_code == 403


def test_saved_local_key_used_in_shared_client(path):
    save(config(), {'test':'synthetic-shared-key'}, [], get()['revision'])
    def handler(request):
        assert request.headers['authorization'] == 'Bearer synthetic-shared-key'
        return httpx.Response(200, json={'id':'test','model':'test-model','choices':[{'finish_reason':'stop','message':{'content':'ok'}}]})
    client = LLMClient(LLMConfig.load(), transport=httpx.MockTransport(handler))
    try:
        assert client.request({'messages':[{'role':'user','content':'test'}]}, profile='default').text == 'ok'
    finally: client.close()


def test_api_config_save_and_reload(path):
    c=TestClient(api.create_app())
    first=c.get('/api/v1/llm/settings').json()
    response=c.post('/api/v1/llm/settings',json={'config':config(), 'keys':{'test':'synthetic-api-key'}, 'revision':first['revision']},headers={'X-Midstream-Action':'1'})
    assert response.status_code == 200
    assert response.json()['credentials_status']['test']['configured']
    assert 'synthetic-api-key' not in response.text
    assert 'synthetic-api-key' not in c.get('/api/v1/llm/settings').text
