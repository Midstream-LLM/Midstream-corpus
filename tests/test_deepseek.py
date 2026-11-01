import httpx
import pytest
from fastapi.testclient import TestClient
from midstream import api
from midstream.llm import deepseek
from midstream.llm.settings import SettingsError


def test_preset_protocol_urls():
    data=deepseek.preset()
    assert data['base_urls']['anthropic_messages']=='https://api.deepseek.com/anthropic'
    assert data['base_urls']['fim_completions'].endswith('/beta')
    assert data['models'][0]['id']=='deepseek-flash'


def test_discovery_only_official_origin_and_metadata():
    def handle(request):
        assert str(request.url)=='https://api.deepseek.com/models'
        assert request.method=='GET'
        assert request.headers['authorization']=='Bearer synthetic-key'
        return httpx.Response(200,json={'data':[{'id':'new-model','name':'New model','context_window':10000,'max_output_tokens':3000,'effort':{'supported_levels':['low','max']},'unknown':'ignored'}]})
    data=deepseek.models('', 'synthetic-key', transport=httpx.MockTransport(handle))
    assert data['models'][0]['max_output_tokens']==3000
    assert data['models'][0]['effort_levels']==['low','max']
    assert 'synthetic-key' not in str(data)


@pytest.mark.parametrize('status,data',[(401,{'error':'secret echoed by upstream'}),(200,{'data':'bad'}),(302,{})])
def test_discovery_error_does_not_echo_response(status,data):
    with pytest.raises(SettingsError) as exc:
        deepseek.models('', 'synthetic-key',transport=httpx.MockTransport(lambda req:httpx.Response(status,json=data)))
    assert 'secret echoed' not in str(exc.value) and 'synthetic-key' not in str(exc.value)


def test_discovery_write_boundary_and_missing_key(tmp_path,monkeypatch):
    monkeypatch.setenv('MIDSTREAM_LLM_CONFIG',str(tmp_path/'absent.toml'))
    c=TestClient(api.create_app())
    assert c.get('/api/v1/llm/deepseek').status_code==200
    assert c.post('/api/v1/llm/deepseek/models',json={'credential':'missing'}).status_code==403
    assert c.post('/api/v1/llm/deepseek/models',json={'credential':'missing'},headers={'X-Midstream-Action':'1'}).status_code==422


def test_discovery_uses_saved_key_and_network_errors(tmp_path,monkeypatch):
    from midstream.llm.settings import get, save
    path=tmp_path/'llm.toml'
    monkeypatch.setenv('MIDSTREAM_LLM_CONFIG',str(path))
    monkeypatch.delenv('DEEPSEEK_TEST_KEY',raising=False)
    data={'credentials':{'deepseek':'DEEPSEEK_TEST_KEY'},'endpoints':{'ds':{'protocol':'chat_completions','base_url':'https://api.deepseek.com','credential':'deepseek'}},'profiles':{'default':{'endpoint':'ds','model':'deepseek-flash'}},'services':{}}
    save(data,{'deepseek':'synthetic-saved-key'},[],get()['revision'])
    def handle(request):
        assert request.headers['authorization']=='Bearer synthetic-saved-key'
        return httpx.Response(200,json={'data':[{'id':'future-model'}]})
    result=deepseek.models('deepseek',transport=httpx.MockTransport(handle))
    assert result['models']==[{'id':'future-model','name':'future-model'}]
    def timeout(request): raise httpx.ReadTimeout('upstream could include credentials')
    with pytest.raises(SettingsError,match='无法获取'):
        deepseek.models('deepseek',transport=httpx.MockTransport(timeout))
