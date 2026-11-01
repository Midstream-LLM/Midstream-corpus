import httpx
import pytest
from fastapi.testclient import TestClient
from midstream import api
from midstream.llm import qwen, LLMClient, LLMConfig, LLMServices
from midstream.llm.settings import SettingsError, get, save


@pytest.mark.parametrize('region,code',[('beijing','cn-beijing'),('singapore','ap-southeast-1'),('virginia','us-east-1')])
def test_region_urls(region,code):
    assert qwen.urls(region,'workspace-123')['base_url']==f'https://workspace-123.{code}.maas.aliyuncs.com/compatible-mode/v1'
    for bad in ('https://evil.example','a.b','x@evil','../abc',''):
        with pytest.raises(SettingsError): qwen.urls(region,bad)
    with pytest.raises(SettingsError): qwen.urls('invalid','ws')


def test_discovery_pagination_and_metadata():
    pages=[]
    def handler(request):
        assert request.url.host=='ws.cn-beijing.maas.aliyuncs.com'
        assert request.url.path=='/api/v1/models'
        assert request.headers['authorization']=='Bearer synthetic-qwen-key'
        assert request.url.params['providers']=='qwen' and request.url.params['capabilities']=='TG'
        page=int(request.url.params['page_no']);pages.append(page)
        return httpx.Response(200,json={'success':True,'output':{'total':2,'models':[{'model':f'qwen-{page}','name':f'Qwen {page}','model_info':{'context_window':100000,'max_output_tokens':8000}}]}})
    result=qwen.models('beijing','ws','unused','synthetic-qwen-key',transport=httpx.MockTransport(handler))
    assert pages==[1,2] and not result['truncated']
    assert result['models'][0]['max_output_tokens']==8000
    assert 'synthetic-qwen-key' not in str(result)


@pytest.mark.parametrize('status,body',[(401,{'message':'secret-from-upstream'}),(200,{'success':False}),(200,{'output':{'total':2,'models':'bad'}}),(302,{})])
def test_error_no_secret_echo(status,body):
    with pytest.raises(SettingsError) as exc:
        qwen.models('singapore','ws','unused','synthetic-key',transport=httpx.MockTransport(lambda req:httpx.Response(status,json=body)))
    assert 'secret-from-upstream' not in str(exc.value) and 'synthetic-key' not in str(exc.value)


def test_saved_qwen_profile_generation_and_discovery(tmp_path,monkeypatch):
    monkeypatch.setenv('MIDSTREAM_LLM_CONFIG',str(tmp_path/'llm.toml'))
    monkeypatch.delenv('DASHSCOPE_API_KEY_BEIJING',raising=False)
    data={'credentials':{'qwen_beijing':'DASHSCOPE_API_KEY_BEIJING'},'endpoints':{'qwen':{'protocol':'chat_completions','base_url':qwen.urls('beijing','ws')['base_url'],'credential':'qwen_beijing'}},'profiles':{'qwen':{'endpoint':'qwen','model':'qwen3.8-max','max_tokens':4096}},'services':{'generation':'qwen'}}
    save(data,{'qwen_beijing':'synthetic-saved-key'},[],get()['revision'])
    def handler(request):
        assert request.headers['authorization']=='Bearer synthetic-saved-key'
        if request.method=='GET':return httpx.Response(200,json={'output':{'total':0,'models':[]}})
        assert str(request.url)=='https://ws.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions'
        return httpx.Response(200,json={'id':'fixture','model':'qwen3.8-max','choices':[{'finish_reason':'stop','message':{'content':'合成答复'}}]})
    transport=httpx.MockTransport(handler)
    assert qwen.models('beijing','ws','qwen_beijing',transport=transport)['models']==[]
    with LLMServices(LLMClient(LLMConfig.load(),transport=transport)) as service:
        assert service.generate('合成问题').text=='合成答复'


def test_qwen_http_boundary(tmp_path,monkeypatch):
    monkeypatch.setenv('MIDSTREAM_LLM_CONFIG',str(tmp_path/'none.toml'))
    c=TestClient(api.create_app())
    assert c.get('/api/v1/llm/qwen').json()['regions']['beijing']['code']=='cn-beijing'
    body={'region':'beijing','workspace':'ws','credential':'none'}
    assert c.post('/api/v1/llm/qwen/models',json=body).status_code==403
    assert c.post('/api/v1/llm/qwen/models',json=body,headers={'X-Midstream-Action':'1'}).status_code==422


def test_discovery_truncation():
    def handler(request):
        page=request.url.params['page_no']
        return httpx.Response(200,json={'output':{'total':1000,'models':[{'model':f'qwen-{page}'}]}})
    result=qwen.models('singapore','ws','unused','synthetic-key',transport=httpx.MockTransport(handler))
    assert result['truncated'] and len(result['models'])==5
