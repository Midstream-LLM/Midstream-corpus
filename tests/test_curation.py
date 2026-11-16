"""Contracts and research behavior; external providers are deterministic test doubles."""
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from midstream.corpus.metadata import RecordMetadata, YearRange, database_patch, step_flags, years
from midstream.corpus.query import Filters, where_clause
from midstream.curation.models import Claim, CurationConfig, Evidence
from midstream.curation.policy import decide, patched_text
from midstream.curation.research import analyze
from midstream.curation.retrieval import Retrieval
from midstream.curation.apply import segments_after_patches
from midstream.llm.config import LLMConfig, Endpoint, Profile
from midstream.llm.errors import LLMError
from midstream.ingest.pipeline import _cleaned_metadata


def config():
    return LLMConfig({'key':'TEST_CURATION_KEY'},
        {'test':Endpoint('chat_completions','https://api.deepseek.com/v1','key')},
        {'default':Profile('test','test-model')}, local_keys={'key':'test-only-key'})


class FakeServices:
    def __init__(self, responses):
        self.client=SimpleNamespace(config=config())
        self.responses=iter(responses)
        self.prompts=[]

    def generate(self,prompt,**kwargs):
        self.prompts.append(json.loads(prompt))
        return SimpleNamespace(text=json.dumps(next(self.responses, {'action':'finish'})), model='test-model',profile='default',
                               finish_reason='stop',response_id='test-response',usage={'input_tokens':12})


class FakeRetrieval:
    def __init__(self):
        self.calls=[]

    def lookup(self,action,query):
        self.calls.append((action,query))
        return [Evidence(key='',provider=action,reference='https://example.org/book',excerpt='作者甲，明代')]


def candidate(field='composed_label',value='明',**kwargs):
    return Claim(field=field,value=value,confidence=0.95,rationale='证据明确记载',evidence_keys=['e0'],**kwargs)


@pytest.mark.parametrize('bad',[{'tagging':'true'},{'tagging':1},{'tagging':None},[],False,{'Bad Key':True}])
def test_boolean_markers_reject_coercion(bad):
    with pytest.raises((ValueError,TypeError)):
        step_flags(bad)


def test_metadata_years_and_publication_preservation():
    meta=_cleaned_metadata({'composed_label':'唐','composed_years':[800,810],'edition_years':[1870,1870]},'甲乙')
    assert meta['composed_years']=={'start_year':800,'end_year':810}
    assert meta['edition_years']=={'start_year':1870,'end_year':1870}
    assert meta['processed_steps']['basic_clean'] is True
    assert meta['processed_steps']['ai_curation'] is False
    assert database_patch({'edition_years':meta['edition_years']})=={'edition_years':'[1870,1871)'}
    assert years('[-3,2)')=={'start_year':-3,'end_year':1}
    assert YearRange(start_year=-3,end_year=1).database()=='[-3,2)'


@pytest.mark.parametrize('value',[[0,1],[3,2],[None,None],['800',900]])
def test_years_do_not_invent_or_coerce(value):
    with pytest.raises(ValueError): years(value)


def test_unknown_fields_and_tags_rejected():
    with pytest.raises(ValidationError):RecordMetadata.model_validate({'authros':['甲']})
    with pytest.raises(ValidationError):candidate('tags',[{'taxonomy':'genre','tag_id':'imaginary'}])
    with pytest.raises(ValidationError):candidate('license','public-domain')


def test_filter_false_uses_missing_key_fallback():
    where,params=where_clause(Filters.from_dict({'processed_steps':{'ai_curation':False}}))
    assert 'coalesce((v.processed_steps' in where
    assert params['completed_0'] is False
    with pytest.raises(ValueError): Filters.from_dict({'processed_steps':{'ai_curation':'false'}})


def test_policy_keeps_uncertain_conflicting_and_existing_values():
    evidence=[Evidence(key='e0',provider='wikidata',reference='Q1')]
    a=candidate(); b=candidate(value='清')
    assert decide([a,b],{},evidence,CurationConfig())==['conflict','conflict']
    assert decide([a],{'composed_label':'唐'},evidence,CurationConfig())==['withheld']
    assert decide([a],{},[],CurationConfig())==['withheld']
    assert decide([a],{},evidence,CurationConfig(apply=False))==['proposed']


def test_search_then_entity_lookup_and_evidence_validation():
    fake=FakeServices([{'action':'search','query':'作品 作者'}, {'action':'wikidata','query':'Q42'},
                       {'action':'finish','claims':[candidate().model_dump()]}])
    retrieval=FakeRetrieval()
    result=analyze({'title':'作品'},'正文',CurationConfig(),fake,retrieval)
    assert retrieval.calls==[('wikidata','作品'),('search','作品 作者'),('wikidata','Q42')]
    assert result.tool_calls==3 and len(result.usage)==4  # 三次考证对话加一次逐段处理
    assert result.completed_steps['metadata_enrichment'] is True
    assert all(e.key for e in result.evidence)


def test_invented_citation_fails_without_output():
    value=candidate().model_dump();value['evidence_keys']=['not-retrieved']
    with pytest.raises(LLMError) as exc:
        analyze({},'正文',CurationConfig(),FakeServices([{'action':'finish','claims':[value]}]),FakeRetrieval())
    assert exc.value.code=='invalid_evidence'


def test_tool_and_model_budgets_stop_incomplete_runs():
    with pytest.raises(LLMError) as exc:
        analyze({},'正文',CurationConfig(max_tool_calls=0),FakeServices([{'action':'search','query':'甲'}]*2),FakeRetrieval())
    assert exc.value.code=='curation_budget_exhausted'
    with pytest.raises(LLMError) as exc:
        analyze({},'甲'*1100,CurationConfig(max_text_chars=1000),FakeServices([]),FakeRetrieval())
    assert exc.value.code=='curation_too_long'


def test_long_text_reads_window_once_and_chunks_without_tools():
    fake=FakeServices([])
    text='甲'*30000
    result=analyze({'title':'书'},text,CurationConfig(chunk_chars=10000,context_chars=4000),fake,FakeRetrieval())
    research, *chunks = fake.prompts
    assert len(research['text']) < 4100 and research['steps']==['metadata_enrichment','tagging']
    assert [p['chunk_start'] for p in chunks]==[0,10000,20000] and all(p['available_tools']==[] for p in chunks)
    assert len(result.usage)==4 and all(result.completed_steps.values())
    only=FakeServices([])
    analyze({},'甲'*10_000_000,CurationConfig(steps=['metadata_enrichment']),only,FakeRetrieval())
    assert len(only.prompts)==1


def test_native_tools_cannot_bypass_controlled_research_budget():
    fake=FakeServices([])
    fake.client.config.profiles['default'].parameters['tools']=[{'type':'deepseek_web_search'}]
    with pytest.raises(LLMError) as exc:
        analyze({},'正文',CurationConfig(),fake,FakeRetrieval())
    assert exc.value.code=='invalid_config' and fake.prompts==[]


def test_unicode_patch_offsets_and_segment_mapping():
    patch=Claim(kind='text_patch',field='text',value={'start_char':1,'end_char':2,'before':'𠮷','after':'甲乙'},
                confidence=0.95,rationale='扫描证据',evidence_keys=['e0'])
    assert patched_text('文𠮷末',[patch])=='文甲乙末'
    segments=segments_after_patches([(1,{'start_char':2,'end_char':3,'title':None,'unit_type':'poem'})],[patch])
    assert (segments[0]['start_char'],segments[0]['end_char'])==(3,4)
    with pytest.raises(ValueError):patched_text('文甲末',[patch])


def test_overlapping_patches_are_not_applied():
    def p(a,b):return Claim(kind='text_patch',field='text',value={'start_char':a,'end_char':b,'before':'甲'*(b-a),'after':''},
                           confidence=0.99,rationale='重复',evidence_keys=['e0'])
    evidence=[Evidence(key='e0',provider='corpus',reference='input')]
    assert decide([p(0,2),p(1,3)],{},evidence,CurationConfig(allow_text_edits=True))==['conflict','conflict']


def test_provider_protocols_use_separate_endpoints():
    seen=[]
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/search'):
            assert json.loads(request.content)=={'query':'明代作者','count':5,'need_content':True}
            return httpx.Response(200,json={'results':[{'url':'https://example.org/book','title':'书','content':'作者甲'}]})
        if request.url.path.endswith('/fetch'):
            return httpx.Response(200,json={'title':'书','content':'作者甲'})
        return httpx.Response(200,json={'entities':{'Q42':{'id':'Q42','labels':{'zh':{'value':'甲'}}}}})
    retrieval=Retrieval(None,config(),client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert retrieval._lookup('search','明代作者')[0].excerpt=='作者甲'
    assert retrieval._lookup('fetch','https://example.org/book')[0].provider=='deepseek_fetch'
    assert retrieval._lookup('wikidata','Q42')[0].reference.endswith('Q42')
    assert str(seen[0].url)=='https://api.deepseek.com/v1/tools/search'
    assert 'authorization' not in seen[2].headers
    retrieval.close()


def test_role_projection_preserves_entities_and_rejects_conflicts():
    from midstream.corpus.metadata import consistent_patch
    before={'authors':['甲'],'contributors':[{'name':'甲','role':'author','entity_id':'wikidata:Q42'}],
            'field_claims':{'authors':[1]},'field_basis':{'authors':'source'}}
    result=consistent_patch(before,{'authors':['甲','乙']})
    assert result['contributors'][0]['entity_id']=='wikidata:Q42'
    assert 'authors' not in result['field_claims']
    with pytest.raises(ValueError):
        consistent_patch(before,{'authors':['甲'],'contributors':[{'name':'乙','role':'compiler'}]})


def test_export_preserves_nested_types(tmp_path):
    from midstream.delivery.export import Options
    from midstream.delivery.formats import Parquet
    import pyarrow.parquet as pq
    rows=[{'processed_steps':{'tagging':True},'edition_years':{'start_year':1870,'end_year':1870},
           'tags':[{'taxonomy':'genre','tag_id':'poetry'}]}]
    path=tmp_path/'typed.parquet'
    with path.open('wb') as stream:Parquet().write(stream,rows,Options())
    restored=pq.read_table(path).to_pylist()[0]
    assert dict(restored['processed_steps'])=={'tagging':True}
    assert restored['edition_years']==rows[0]['edition_years']
    assert restored['tags']==rows[0]['tags']



def test_invalid_candidates_are_dropped_individually():
    bad={'kind':'metadata','field':'topic','value':'政治','evidence_keys':['e0'],'confidence':0.9,'rationale':'误用字段'}
    good={'kind':'metadata','field':'genre','value':'诗','evidence_keys':['e0'],'confidence':0.9,'rationale':'五言律诗'}
    result=analyze({},'正文',CurationConfig(use_authorities=False,steps=['tagging']),
                   FakeServices([{'action':'finish','claims':[bad,good]}]),FakeRetrieval())
    assert [c.field for c in result.claims]==['genre'] and len(result.rejected)==1 and '丢弃 1 条' in result.summary


def test_facts_need_outside_evidence_and_retrieved_ids():
    corpus=Evidence(key='e0',provider='corpus',reference='input',excerpt='序　耐道人施士洁')
    wiki=Evidence(key='e1',provider='wikidata',reference='https://www.wikidata.org/wiki/Q42',data={'id':'Q42'})
    def c(field,value,keys):return Claim(field=field,value=value,confidence=0.95,rationale='理由',evidence_keys=keys)
    cases=[(c('authors',['施士洁'],['e0']),'applied'),            # 正文署名
           (c('authors',['罗洪先'],['e0']),'withheld'),           # 正文里没有
           (c('composed_years',{'start_year':1900,'end_year':1900},['e0']),'withheld'),  # 只有正文
           (c('composed_label','明',['e1']),'applied'),           # 有外部证据
           (c('external_ids',{'wikidata':'Q5179'},['e1']),'withheld'),  # 编号不在检索结果里
           (c('external_ids',{'wikidata':'Q42'},['e1']),'applied')]
    for claim,expected in cases:
        assert decide([claim],{},[corpus,wiki],CurationConfig())==[expected], claim.field


def test_segments_must_match_text_and_tags_are_merged():
    text='甲詩一首。乙詩二首。'
    seg=lambda start,anchor:{'kind':'segment','field':'segments','evidence_keys':['e1'],'confidence':0.9,'rationale':'篇題',
                             'value':{'start_char':start,'end_char':start+5,'start_text':anchor,'title':None,'unit_type':'poem'}}
    tag=lambda tid,tax:{'kind':'metadata','field':'tags','value':[{'taxonomy':tax,'tag_id':tid}],'evidence_keys':['e0'],
                        'confidence':0.9,'rationale':tax}
    fake=FakeServices([{'action':'finish','claims':[tag('poetry','genre'),tag('nature','topic')]},
                       {'action':'finish','claims':[seg(5,'乙詩'),seg(0,'丙丁')]}])
    result=analyze({},text,CurationConfig(use_authorities=False,steps=['tagging','segmentation']),fake,FakeRetrieval())
    segments=[c for c in result.claims if c.field=='segments']
    assert [s.value['start_char'] for s in segments]==[5] and len(result.rejected)==1
    tags=[c for c in result.claims if c.field=='tags']
    assert len(tags)==1 and {t['tag_id'] for t in tags[0].value}=={'poetry','nature'}


def test_batch_routes_claims_to_records_and_puts_static_prompt_first():
    from midstream.curation.research import analyze_batch, BATCH_PROMPT
    tag=lambda record,key,tid:{'record':record,'kind':'metadata','field':'tags','evidence_keys':[key],'confidence':0.95,
                               'rationale':'体裁','value':[{'taxonomy':'genre','tag_id':tid}]}
    seen=[]
    class Recording(FakeServices):
        def generate(self,prompt,**kwargs):
            seen.append(kwargs['system']); return super().generate(prompt,**kwargs)
    fake=Recording([{'action':'finish','claims':[tag(0,'e0','poetry'),tag(1,'e1','ci'),tag(1,'e0','qu')]}])
    records=[{'metadata':{'title':'甲'},'text':'床前明月光'},{'metadata':{'title':'乙'},'text':'明月几时有'}]
    first,second=analyze_batch(records,CurationConfig(use_authorities=False),fake,FakeRetrieval())
    assert len(fake.prompts)==1 and seen==[BATCH_PROMPT] and 'records' in fake.prompts[0]
    assert [t['tag_id'] for t in first.claims[0].value]==['poetry']
    assert [t['tag_id'] for t in second.claims[0].value]==['ci'] and len(second.rejected)==1  # 引用了别条的正文
    assert first.usage[0]['batch_size']==2 and second.usage==[{'batched_with':0,'batch_size':2}]



def test_over_budget_request_gets_one_forced_finish_and_trailing_json_is_ignored():
    fake=FakeServices([{'action':'search','query':'甲'}])
    result=analyze({},'正文',CurationConfig(max_tool_calls=0,steps=['tagging']),fake,FakeRetrieval())
    assert len(fake.prompts)==2 and fake.prompts[1]['available_tools']==[] and '已拒绝' in str(fake.prompts[1]['tool_history'])
    from midstream.curation.research import parse
    parsed,_=parse('{"action":"finish","query":"","claims":[],"summary":"甲"}\n\n{"action":"finish"}')
    assert parsed.summary=='甲'
