"""Controlled embeddings test indexing/ranking contracts, never semantic accuracy."""
import copy
import json

import pytest

from backend.document_engine import _chunks, answer_document
from backend.retrieval import RetrievalService, EmbeddingError, StaleIndexError, ExactContextReranker


def source(texts, **updates):
    out = {'id':'s1','project_id':'p1','name':'测试资料.txt','sha256':'a'*64,'parser_version':'p1',
           'status':'ready','archived':False,'metadata':{},'warnings':[],
           'pages':[{'number':i+1,'text':text} for i,text in enumerate(texts)]}
    out.update(updates)
    return out


class ControlledEmbedding:
    identity = {'model':'controlled-test-vector-not-production','revision':'1','dimension':3}
    def __init__(self):
        self.calls=[]
        self.fail=False
        self.hook=None
    def embed(self,texts,*,kind,cancelled):
        self.calls.append((kind,list(texts)))
        if self.hook:
            self.hook()
        if self.fail:
            raise EmbeddingError('controlled failure')
        return [[1.,0.,0.] if ('辞职' in t or '离职' in t) else [0.,1.,0.] if '工业' in t else [0.,0.,1.] for t in texts]


def test_index_survives_restart_without_rechunk_or_passage_embedding(tmp_path, monkeypatch):
    source1=source(['员工辞职应提前通知。']*30)
    model=ControlledEmbedding();service=RetrievalService(tmp_path,model)
    first=service.prepare(source1,'p1')
    assert not first.cache_hit
    import backend.document_engine as engine
    monkeypatch.setattr(engine,'_chunks',lambda *args:pytest.fail('ready index must not re-chunk'))
    new_model=ControlledEmbedding();second=RetrievalService(tmp_path,new_model).prepare(source1,'p1')
    assert second.cache_hit
    assert second.select('离职手续',limit=1)[0]
    assert [call[0] for call in new_model.calls]==['query']


def test_tail_vector_retrieved_after_multiple_exact_scan_blocks(tmp_path):
    document=source([f'仓储设备条款第{i}项。' for i in range(600)]+['员工辞职应提前通知人事。'])
    prepared=RetrievalService(tmp_path,ControlledEmbedding()).prepare(document,'p1')
    selected,_=prepared.select('离职手续',limit=1,mode='vector')
    assert selected[0].page==601
    assert prepared.audit()['searches'][-1]['vector_rows_scanned']==601


def test_every_chunk_retains_exact_offsets_table_identity_and_heading(tmp_path):
    text='第一章 管理\n员工辞职应提前通知人事。\n内部页脚'
    start=text.index('员工');end=text.index('\n内部')
    document=source([text],metadata={'document_structure':{'headings':[{'id':'h1','title':'第一章 管理','parent_id':None,
        'source_position':{'page':1,'start':0,'end':7},'body_spans':[{'page':1,'start':start,'end':end}]}],
        'excluded_spans':[{'page':1,'start':end,'end':len(text),'role':'footer'}]}})
    prepared=RetrievalService(tmp_path,None).prepare(document,'p1')
    chunks,_=prepared.prepared()
    body=next(c for c in chunks if '辞职' in c.text)
    assert body.heading_path==('第一章 管理',) and body.parent_id=='h1'
    assert all(text[c.start:c.end]==c.text for c in chunks)
    assert not any('内部页脚' in c.text for c in chunks)


def test_current_source_version_and_parser_change_build_new_generation(tmp_path):
    service=RetrievalService(tmp_path,ControlledEmbedding());document=source(['甲收入10万元。'])
    first=service.prepare(document,'p1')
    edited=copy.deepcopy(document);edited['pages'][0]['text']='甲收入20万元。'
    second=service.prepare(edited,'p1')
    assert first.generation!=second.generation
    with pytest.raises(StaleIndexError):first.select('收入')
    edited['parser_version']='p2';third=service.prepare(edited,'p1')
    assert third.generation!=second.generation


def test_model_revision_invalidates_cached_vectors(tmp_path):
    model=ControlledEmbedding();service=RetrievalService(tmp_path,model);document=source(['甲收入10万元。'])
    first=service.prepare(document,'p1')
    model.identity={**model.identity,'revision':'2'}
    second=service.prepare(document,'p1')
    assert first.generation!=second.generation


def test_scope_is_applied_before_recall_and_archive_kills_prepared_handle(tmp_path):
    service=RetrievalService(tmp_path,ControlledEmbedding());document=source(['员工辞职须通知。'])
    first=service.prepare(document,'p1')
    with pytest.raises(PermissionError):service.prepare(document,'p2')
    other=source(['项目二秘密。'],id='s2',project_id='p2')
    service.prepare(other,'p2')
    assert all('秘密' not in c.text for c in first.select('秘密',limit=1)[0])
    service.invalidate('p1','s1')
    with pytest.raises(StaleIndexError):first.select('辞职')
    document['archived']=True
    with pytest.raises(StaleIndexError):service.prepare(document,'p1')


def test_source_changes_mid_build_never_publish(tmp_path):
    current=[True];model=ControlledEmbedding();model.hook=lambda:current.__setitem__(0,False)
    service=RetrievalService(tmp_path,model)
    with pytest.raises(StaleIndexError):service.prepare(source(['员工辞职须通知。']),'p1',is_current=lambda:current[0])
    assert service.store.status('p1','s1')['state']=='not_ready'


def test_new_version_invalidates_previous_handle_even_when_rebuild_cancelled(tmp_path):
    model=ControlledEmbedding();service=RetrievalService(tmp_path,model)
    document=source(['甲收入10万元。']);old=service.prepare(document,'p1')
    edited=copy.deepcopy(document);edited['pages'][0]['text']='甲收入20万元。'
    cancel=[False];model.hook=lambda:cancel.__setitem__(0,True)
    with pytest.raises(InterruptedError):service.prepare(edited,'p1',lambda:cancel[0])
    with pytest.raises(StaleIndexError):old.select('收入')


def test_invalidation_during_build_cannot_reactivate_generation(tmp_path):
    model=ControlledEmbedding();service=RetrievalService(tmp_path,model)
    model.hook=lambda:service.invalidate('p1','s1')
    with pytest.raises(StaleIndexError):service.prepare(source(['员工辞职须通知。']),'p1')
    assert service.store.status('p1','s1')['state']=='not_ready'


def test_cancel_preserves_committed_batches_and_resume_only_missing(tmp_path):
    model=ControlledEmbedding();service=RetrievalService(tmp_path,model,batch_size=2)
    document=source([f'第{i}项员工辞职条款。' for i in range(8)])
    def cancelled():return len(model.calls)>=2
    with pytest.raises(InterruptedError):service.prepare(document,'p1',cancelled)
    assert service.store.status('p1','s1')['state']=='not_ready'
    model.calls.clear()
    handle=service.prepare(document,'p1')
    assert handle.audit()['coverage']['resumed_chunks']==2
    assert sum(len(t) for kind,t in model.calls if kind=='passage')==6


def test_embedding_failure_is_explicit_persisted_lexical_fallback(tmp_path):
    model=ControlledEmbedding();model.fail=True;service=RetrievalService(tmp_path,model)
    document=source(['辞职手续需要交接。']*30)
    handle=service.prepare(document,'p1');selected,_=handle.select('辞职手续',limit=1)
    assert selected and handle.audit()['mode']=='lexical'
    assert handle.audit()['searches'][0]['fallback_reason']=='embedding_build_failed'
    count=len(model.calls);service.prepare(document,'p1').select('辞职手续',limit=1)
    assert len(model.calls)==count


def test_query_embedding_failure_does_not_claim_absent_answer(tmp_path):
    model=ControlledEmbedding();handle=RetrievalService(tmp_path,model).prepare(source(['辞职手续需要交接。']*30),'p1')
    model.fail=True;selected,_=handle.select('辞职',limit=1)
    assert selected and handle.audit()['searches'][-1]['mode_used']=='lexical'
    assert handle.audit()['searches'][-1]['fallback_reason']=='embedding_query_failed'


def test_explicit_lexical_comparison_is_not_reported_as_failure(tmp_path):
    handle=RetrievalService(tmp_path,None,default_mode='lexical').prepare(source(['员工辞职通知人事。']),'p1')
    handle.select('辞职')
    assert handle.audit()['searches'][0]['fallback_reason'] is None


def test_rrf_and_period_rerank_keep_distinct_same_value_rows(tmp_path):
    document=source(['2023年甲项目收入10万元。','2024年甲项目收入10万元。']+['普通材料']*30)
    service=RetrievalService(tmp_path,ControlledEmbedding(),reranker=ExactContextReranker())
    handle=service.prepare(document,'p1')
    selected,_=handle.select('2024年甲项目收入',limit=1,mode='rerank')
    assert selected[0].page==2
    assert len(handle.prepared()[0])==32
    assert handle.audit()['searches'][-1]['reranker']=='exact-year-entity-v1'


def test_focused_recovery_excludes_already_used_evidence(tmp_path):
    handle=RetrievalService(tmp_path,None).prepare(source(['甲收入10万元。','乙收入20万元。']+['普通材料']*30),'p1')
    initial=handle.select('甲收入',limit=1)[0]
    more=handle.select('甲乙收入',focus='乙收入',limit=1,exclude_ids=[initial[0].id])[0]
    assert more[0].page==2
    assert handle.audit()['searches'][-1]['excluded_chunk_ids']==[initial[0].id]


def test_no_lexical_match_on_long_document_returns_miss_not_arbitrary_prefix(tmp_path):
    handle=RetrievalService(tmp_path,None).prepare(source(['机器维修条款。']*30),'p1')
    assert handle.select('养老金申领',limit=2)[0]==[]


def test_document_integration_publishes_audit_without_changing_citations(tmp_path):
    document=source(['员工辞职应提前通知人事。']);handle=RetrievalService(tmp_path,None).prepare(document,'p1')
    def model(messages,purpose):
        payload=json.loads(messages[-1]['content'])
        if purpose=='document':
            chunk=payload['chunks'][0]
            return {'status':'completed','claims':[{'text':chunk['text'],'citations':[{'chunk_id':chunk['chunk_id'],'quote':chunk['text']}]}]}
        return {'answer_complete':True,'claims':[{'id':c['id'],'supported':True} for c in payload['claims']],
                'coverage':[{'question_id':p['id'],'status':'answered','claim_ids':[c['id'] for c in payload['claims']]} for p in payload['question_parts']]}
    result=answer_document(document,'辞职须做什么？',model,lambda:False,lambda _:None,retriever=handle)
    assert result['status']=='completed'
    assert result['retrieval_audit']['mode']=='lexical'
    e=result['evidence'][0]
    assert document['pages'][0]['text'][e['start']:e['end']]==e['quote']


def test_cleanup_keeps_active_and_fresh_builds(tmp_path):
    service=RetrievalService(tmp_path,None);handle=service.prepare(source(['甲收入10万元。']),'p1')
    assert service.store.cleanup(older_than_seconds=0)==0
    assert handle.select('收入')[0]
    service.invalidate('p1','s1')
    assert service.store.cleanup(older_than_seconds=0)==1


def test_traditional_query_variant_preserves_exact_evidence(tmp_path):
    import copy
    from backend.retrieval import RetrievalService
    source={'id':'trad','project_id':'p','name':'统计.txt','sha256':'h','parser_version':'test','metadata':{},
            'pages':[{'number':1,'text':'无关的园林名录。'},{'number':2,'text':'工业总产值为12亿元。'}]}
    before=copy.deepcopy(source)
    prepared=RetrievalService(tmp_path,None,default_mode='lexical').prepare(source,'p')
    result,_=prepared.select('工業總產值',limit=1)
    assert result[0].page==2 and result[0].text=='工业总产值为12亿元。'
    assert source==before
    search=prepared.audit()['searches'][-1]
    assert search['query']=='工業總產值' and search['lexical_query_variants']==['工業總產值','工业总产值']


def test_simplified_query_matches_traditional_source_without_mutation(tmp_path):
    from backend.retrieval import RetrievalService
    source={'id':'trad2','project_id':'p','name':'統計.txt','sha256':'h','parser_version':'test','metadata':{},
            'pages':[{'number':1,'text':'无关的园林名录。'},{'number':2,'text':'工業總產值為12億元。'}]}
    prepared=RetrievalService(tmp_path,None,default_mode='lexical').prepare(source,'p')
    result,_=prepared.select('工业总产值',limit=1)
    assert result[0].page==2 and result[0].text=='工業總產值為12億元。'
    assert 'lexical' in prepared.audit()['version']
