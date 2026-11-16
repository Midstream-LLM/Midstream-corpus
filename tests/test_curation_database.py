"""Transactional tests against an explicitly configured, disposable PostgreSQL DB."""
import os
import pytest
from psycopg.types.json import Jsonb
from test_pipeline import conn, row, write
from test_curation import FakeServices, FakeRetrieval, candidate
from midstream.curation import pipeline, repository
from midstream.curation.models import CurationConfig, ResumeOptions
from midstream.curation.apply import apply_item, rollback_item, store_blob
from midstream.corpus import management, query
from midstream.ingest import pipeline as ingest, batches
from midstream.ingest.adapters import ADAPTERS
from midstream.curation.ingestion import configure_batch, ensure_ready
from midstream.corpus.metadata import RecordMetadata
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

pytestmark=pytest.mark.skipif(not os.environ.get('MIDSTREAM_TEST_DSN'),reason='需要专用 MIDSTREAM_TEST_DSN')


def seed(conn):
    source=conn.execute("INSERT INTO source(name) VALUES('curation-test') RETURNING source_id").fetchone()[0]
    doc=conn.execute("INSERT INTO document(source_id,source_key) VALUES(%s,'one') RETURNING doc_id",(source,)).fetchone()[0]
    run=conn.execute("INSERT INTO processing_run(kind,rule_version) VALUES('clean','test') RETURNING run_id").fetchone()[0]
    digest=store_blob(conn,'春眠不觉晓。')
    version=conn.execute("INSERT INTO document_version(doc_id,version_no,text_hash,run_id,is_current,quality_status,char_count,title)"
        " VALUES(%s,1,%s,%s,true,'ok',6,'春晓') RETURNING version_id",(doc,digest,run)).fetchone()[0]
    conn.commit()
    return doc,version


def run(conn,**kwargs):
    return pipeline.run(conn,actor='test',reason='curation test',retrieval=FakeRetrieval(),**kwargs)


def test_auto_apply_review_and_rollback_are_independent(conn):
    doc,old=seed(conn)
    result=run(conn,services=FakeServices([{'action':'finish','claims':[candidate().model_dump()]}]))
    assert result['counts']=={'applied':1}
    current=query.metadata(conn,doc)
    assert current['version_id']!=old and current['composed_label']=='明'
    assert current['processed_steps']['ai_curation'] is True
    cid,item=conn.execute('SELECT claim_id,item_id FROM curation_claim').fetchone()
    assert conn.execute('SELECT human_reviewed,source_kind FROM curation_claim').fetchone()==(False,'llm')
    repository.review(conn,cid,'confirmed','reviewer','checked evidence')
    assert conn.execute('SELECT human_reviewed,source_kind FROM curation_claim').fetchone()==(True,'llm')
    assert run(conn,services=FakeServices([]))['items']==0
    rollback_item(conn,item,'test','restore original')
    restored=query.metadata(conn,doc)
    assert restored['composed_label'] is None
    assert restored['processed_steps']['ai_curation'] is False
    assert conn.execute('SELECT count(*) FROM document_version WHERE doc_id=%s',(doc,)).fetchone()[0]==3


def test_empty_result_is_completed_and_manual_edit_inherits_marks(conn):
    doc,old=seed(conn)
    run(conn,services=FakeServices([{'action':'finish'}]))
    current=query.metadata(conn,doc)
    assert current['processed_steps']['ai_curation'] is True
    management.review_document(conn,doc,current['version_id'],{'title':'新标题'},actor='test',reason='edit')
    current=query.metadata(conn,doc)
    assert current['processed_steps']['ai_curation'] is True
    assert current['processed_steps']['manual_edit'] is True


def test_proposal_retry_does_not_call_model_and_conflict_does_not_overwrite(conn):
    doc,old=seed(conn)
    result=run(conn,config=CurationConfig(apply=False),services=FakeServices([{'action':'finish'}]))
    item=conn.execute('SELECT item_id FROM curation_item WHERE run_id=%s',(result['run_id'],)).fetchone()[0]
    assert query.metadata(conn,doc)['version_id']==old
    again=run(conn,resume_run_id=result['run_id'],services=FakeServices([]))
    assert again['counts']=={'proposed':1}
    management.review_document(conn,doc,old,{'title':'edited'},actor='test',reason='concurrent edit')
    with pytest.raises(management.ManagementError):apply_item(conn,item,CurationConfig())
    conn.rollback()
    assert query.metadata(conn,doc)['title']=='edited'


def test_failed_budget_keeps_record_unclean_and_can_resume(conn):
    doc,old=seed(conn)
    result=run(conn,config=CurationConfig(max_tool_calls=0),services=FakeServices([{'action':'search','query':'甲'}]*2))  # 超预算先被拒一次，再请求才失败
    assert result['counts']=={'failed':1}
    assert not query.metadata(conn,doc)['processed_steps'].get('ai_curation',False)
    result=run(conn,resume_run_id=result['run_id'],resume_options=ResumeOptions(max_tool_calls=2),services=FakeServices([{'action':'finish'}]))
    assert result['counts']=={'applied':1}


def test_staging_cleaning_binds_evidence_and_preserves_edition_years(conn,tmp_path):
    path=write(tmp_path/'book.jsonl',[row('book','古籍正文足以检验发布。')])
    adapter=ADAPTERS['ccc_corpus']
    bid=ingest.register_batch(conn,adapter,[path])
    configure_batch(conn,bid,CurationConfig())
    conn.commit()
    with ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn')) as pool:
        ingest.stage(conn,bid,adapter,pool)
        ingest.validate(conn,bid)
        with pytest.raises(management.ManagementError):ensure_ready(conn,bid)
        conn.rollback()
        value=candidate('edition_years',{'start_year':1870,'end_year':1870}).model_dump()
        result=run(conn,batch_id=bid,services=FakeServices([{'action':'finish','claims':[value]}]))
        assert result['complete']
        ingest.publish(conn,bid,pool)
    doc,version=conn.execute('SELECT doc_id,version_id FROM document_version WHERE is_current').fetchone()
    assert query.metadata(conn,doc)['edition_years']=={'start_year':1870,'end_year':1870}
    assert conn.execute('SELECT doc_id,result_version_id FROM curation_item').fetchone()==(doc,version)
    assert query.metadata(conn,doc)['processed_steps']['ai_curation']
    assert conn.execute("SELECT count(*) FROM processing_event WHERE step='basic_clean'").fetchone()[0]==1


def test_database_rejects_non_boolean_flags(conn):
    doc,_=seed(conn)
    with pytest.raises(Exception):
        conn.execute('UPDATE document_version SET processed_steps=%s WHERE doc_id=%s',(Jsonb({'tagging':'true'}),doc))
    conn.rollback()


def test_resume_replays_paid_calls_after_result_save_failure(conn,monkeypatch):
    seed(conn)
    original=pipeline.save_result
    def fail(*args,**kwargs):raise ValueError('synthetic persistence interruption')
    monkeypatch.setattr(pipeline,'save_result',fail)
    first=run(conn,services=FakeServices([{'action':'finish','claims':[candidate().model_dump()]}]))
    assert first['counts']=={'failed':1}
    # 一次书目考证加一次逐段处理，恢复时都直接复用。
    assert conn.execute("SELECT count(*) FROM curation_call WHERE kind='model'").fetchone()[0]==2
    monkeypatch.setattr(pipeline,'save_result',original)
    restored=run(conn,resume_run_id=first['run_id'],services=FakeServices([]))
    assert restored['counts']=={'applied':1}


@pytest.mark.parametrize('concurrent_edit',[False,True])
def test_reimport_reuses_manual_metadata_and_checks_its_version(conn,tmp_path,concurrent_edit):
    original=row('a','重复来源的合成正文。')
    path=write(tmp_path/'repeat.jsonl',[original])
    adapter=ADAPTERS['ccc_corpus']
    ingest.ingest(conn,adapter,[path])
    doc,version=conn.execute('SELECT doc_id,version_id FROM document_version WHERE is_current').fetchone()
    management.review_document(conn,doc,version,{'title':'核定篇名'},actor='test',reason='verified title')
    base=query.metadata(conn,doc)['version_id']
    write(path,[original,row('b','新增来源合成正文。')])
    bid=ingest.register_batch(conn,adapter,[path])
    configure_batch(conn,bid,CurationConfig())
    conn.commit()
    with ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn')) as pool:
        ingest.stage(conn,bid,adapter,pool)
        ingest.validate(conn,bid)
        result=run(conn,batch_id=bid,services=FakeServices([{'action':'finish'},{'action':'finish'}]))
        assert result['counts']=={'applied':2}
        if concurrent_edit:
            management.review_document(conn,doc,base,{'title':'后续修订'},actor='test',reason='later edit')
            with pytest.raises(management.ManagementError):ingest.publish(conn,bid,pool)
            conn.rollback()
            assert query.metadata(conn,doc)['title']=='后续修订'
        else:
            ingest.publish(conn,bid,pool)
            current=query.metadata(conn,doc)
            assert current['title']=='核定篇名' and current['field_basis']['title']=='human'
            assert current['processed_steps']['ai_curation'] is True


def test_uncurated_records_publish_only_after_confirmation(conn,tmp_path):
    path=write(tmp_path/'long.jsonl',[row('long','长'*3000)])
    adapter=ADAPTERS['ccc_corpus']
    bid=ingest.register_batch(conn,adapter,[path])
    configure_batch(conn,bid,CurationConfig(max_text_chars=1000))
    conn.commit()
    with ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn')) as pool:
        ingest.stage(conn,bid,adapter,pool)
        ingest.validate(conn,bid)
        result=run(conn,batch_id=bid,config=CurationConfig(max_text_chars=1000),services=FakeServices([]))
        assert result['counts']=={'failed':1} and not result['complete']
        assert conn.execute('SELECT error_code FROM curation_item').fetchone()[0]=='curation_too_long'
    assert batches.inspect(conn,bid)['uncurated']==1
    with pytest.raises(management.ManagementError):
        batches.publish(conn,bid,actor='test',reason='publish')
    assert batches.publish(conn,bid,actor='test',reason='publish',allow_uncurated=True)['status']=='published'
    doc=conn.execute('SELECT doc_id FROM document').fetchone()[0]
    assert query.metadata(conn,doc)['processed_steps']['ai_curation'] is False
    event=conn.execute("SELECT after_state FROM management_event WHERE action='batch_resume' ORDER BY event_id DESC").fetchone()[0]
    assert event['uncurated_published']==1
