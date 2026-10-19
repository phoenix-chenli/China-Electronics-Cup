"""Multi-source lifecycle uses an explicit fake engine; no network/model claims."""
import json
import sys
import threading
import types
import uuid
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from test_answers_api import configure,project,upload,submit,wait_terminal,FakeGatewayFactory

@pytest.fixture
def env(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event();state={'block':False,'calls':[],'resolutions':[]}
    def resolve(item,history,source,gateway,*,history_loader=None):
        # This fixture isolates lifecycle/routing. Real interpretation and its
        # independent verdict are covered by query/context tests.
        assert history and callable(history_loader)
        state['resolutions'].append((item['question'],history,source['id']))
        return {'status':'ready','normalized_question':item['question'],'intent':{}}
    monkeypatch.setattr('backend.query_planner.resolve_followup',resolve)
    def engine(sources,question,history,gateway,cancelled,progress,*,retrieval_factory=None,audit=None,actual_user_question=None):
        assert callable(retrieval_factory) and callable(audit)
        state['calls'].append((sources,question,history));entered.set()
        if state['block']:assert release.wait(5)
        evidence=[dict(id=f'E{i}',kind='text',source_id=s['id'],source_name=s['name'],sha256=s['sha256'],parser_version=s['parser_version'],quote=s['pages'][0]['text'],page=1,start=0,end=len(s['pages'][0]['text']),label=s['name'],stale=False) for i,s in enumerate(sources)]
        return 'completed',dict(text='Explicit fake result',claims=[],evidence=evidence,notes=[],verification={'status':'passed','checks':['fake']},steps=[])
    monkeypatch.setitem(sys.modules,'backend.cross_source_engine',types.SimpleNamespace(answer_cross_source=engine))
    root=tmp_path/'run';configure(root);app=create_app(root,tmp_path/'samples',False,FakeGatewayFactory())
    with TestClient(app) as client:
        try:yield client,state,entered,release
        finally:release.set()

def setup(client):
    pid=project(client);a=upload(client,pid,'a.txt',b'AAA');b=upload(client,pid,'b.txt',b'BBB')
    ids=[a['source']['id'],b['source']['id']]
    body={'question':'Compare facts','source_id':ids[0],'source_ids':ids,'mode':'multi_source','request_id':str(uuid.uuid4())}
    return pid,ids,body

def test_scope_idempotency_history_and_secondary_source_staleness(env):
    client,state,_,_=env;pid,ids,body=setup(client)
    first=wait_terminal(client,pid,submit(client,pid,body)['id'])
    assert first['status']=='completed' and not first['scope_stale']
    assert set(first['source_ids'])==set(ids) and len(first['sources'])==2
    assert all('data_path' not in s for s in first['sources'])
    assert submit(client,pid,body)['id']==first['id'] and len(state['calls'])==1
    assert client.post(f'/api/projects/{pid}/answers',json={**body,'source_ids':ids[:1]}).status_code==409
    reply={**body,'request_id':str(uuid.uuid4()),'question':'And then?','parent_answer_id':first['id']}
    second=wait_terminal(client,pid,submit(client,pid,reply)['id'])
    assert second['status']=='completed' and second['turn_index']==2 and len(state['calls'][-1][2])==1
    assert len(state['resolutions'])==1 and state['resolutions'][0][1][0]['answer_id']==first['id']
    # Same parser_version but regenerated content must invalidate citations and follow-ups.
    with client.app.state.service.store.transaction() as c:
        c.execute('UPDATE sources SET pages=? WHERE id=?',(json.dumps([{'number':1,'text':'Changed'}]),ids[1]))
    saved=client.get(f'/api/projects/{pid}/answers/{first["id"]}').json()
    assert saved['scope_stale']
    assert [e['stale'] for e in saved['result']['evidence']]==[False,True]
    assert client.post(f'/api/projects/{pid}/answers',json={**reply,'request_id':str(uuid.uuid4())}).status_code==409

def test_cross_project_unusable_duplicates_and_wrong_mode_rejected(env):
    client,state,_,_=env;pid,ids,body=setup(client)
    other=project(client,'Other');foreign=upload(client,other,'foreign.txt',b'Foreign')['source']['id']
    for changes,status in [({'source_ids':ids+[foreign]},404),({'source_ids':ids+ids},409),({'source_ids':ids[1:]},409),({'mode':'document'},409),({'source_ids':ids*7},422)]:
        assert client.post(f'/api/projects/{pid}/answers',json={**body,**changes}).status_code==status
    with client.app.state.service.store.transaction() as c:c.execute('UPDATE sources SET archived=1 WHERE id=?',(ids[1],))
    assert client.post(f'/api/projects/{pid}/answers',json=body).status_code==409
    assert not state['calls']


def test_twelve_source_scope_roundtrip_and_ownership_are_preserved(env):
    client,state,_,_=env;pid=project(client)
    ids=[upload(client,pid,f'doc-{i}.txt',f'Document {i} independent body'.encode())['source']['id'] for i in range(13)]
    body={'question':'核对选中的资料','source_id':ids[0],'source_ids':ids[:12],
          'mode':'multi_source','request_id':str(uuid.uuid4())}
    answer=wait_terminal(client,pid,submit(client,pid,body)['id'])
    assert answer['status']=='completed' and set(answer['source_ids'])==set(ids[:12])
    assert len(state['calls'])==1 and len(state['calls'][0][0])==12
    path=f'/api/projects/{pid}/workspace/test_12_sources'
    response=client.put(path,json={'state':{'source_ids':ids[:12]},'expected_revision':0})
    assert response.status_code==200
    assert client.get(path).json()['state']['source_ids']==ids[:12]
    assert client.post(f'/api/projects/{pid}/answers',json={**body,'request_id':str(uuid.uuid4()),'source_ids':ids}).status_code==422
    assert client.put(path,json={'state':{'source_ids':ids},'expected_revision':1}).status_code==409
    other=project(client,'foreign');foreign=upload(client,other,'foreign.txt',b'Foreign unique body')['source']['id']
    invalid={**body,'request_id':str(uuid.uuid4()),'source_ids':ids[:11]+[foreign]}
    assert client.post(f'/api/projects/{pid}/answers',json=invalid).status_code==404
    assert len(state['calls'])==1

def test_secondary_source_changed_during_run_never_published(env):
    client,state,entered,release=env;pid,ids,body=setup(client);state['block']=True
    item=submit(client,pid,body);assert entered.wait(2)
    with client.app.state.service.store.transaction() as c:c.execute('UPDATE sources SET parser_version=? WHERE id=?',('new-version',ids[1]))
    release.set();done=wait_terminal(client,pid,item['id'])
    assert done['status']=='failed' and done['result'] is None and '版本' in done['error']

def test_multisource_cancel_remains_terminal(env):
    client,state,entered,release=env;pid,ids,body=setup(client);state['block']=True
    item=submit(client,pid,body);assert entered.wait(2)
    assert client.post(f'/api/projects/{pid}/answers/{item["id"]}/cancel').json()['status']=='cancelled'
    release.set();done=wait_terminal(client,pid,item['id'])
    assert done['status']=='cancelled' and done['result'] is None


def _mark_ocr_fixture(client, sid, table_name=None, *, native_selected=False):
    """Explicit parsed-source fixture; real Vision parsing is covered separately."""
    store=client.app.state.service.store
    source=store.get('sources',sid)
    metadata=dict(source['metadata'],ocr_performed=True)
    if table_name:
        metadata['table_regions']=[dict(table_name=table_name,page=1,extraction='native_grid' if native_selected else 'ocr_grid',requires_review=not native_selected,
                                       cells=[dict(row=2,col=2,text='0.10',ocr_disagreement=True,ocr_candidates=['0.70','0.10'])])]
    pages=source['pages'] or [dict(number=1,title='扫描页',text='Explicit OCR fixture text',char_count=25,has_image=True)]
    pages[0]['extraction']='ocr'
    if native_selected:
        from backend.table_semantics import build_table_semantics
        values = [['item', 'amount'], ['A', '0.10'], ['B', '0.20']]
        region = dict(table_name=table_name, page=1, extraction='native_grid', requires_review=False,
                      row_count=3, column_count=2, bbox=[.1, .1, .9, .7],
                      cells=[dict(row=r, col=c, text=value, rowspan=1, colspan=1, covered_by=None,
                                  bbox=[.1+(c-1)*.4, .1+(r-1)*.2, .1+c*.4, .1+r*.2])
                             for r,row in enumerate(values,1) for c,value in enumerate(row,1)])
        semantic = build_table_semantics(region, format='pdf',
                    source_position={'page':1,'page_unit':'physical_page'}, explicit_header_rows=[1])
        # The actual uploaded CSV has no header data row; this explicit native
        # fixture maps its two SQL rows to physical table rows 2 and 3.
        semantic['sql_row_mapping'] = {'runs':[[1,2,2]], 'header_source_row':1, 'sql_row_count':2}
        metadata.update(table_regions=[region], table_semantics=[semantic])
        native_text='item amount\nA 0.10\nB 0.20'
        pages=[dict(number=1,title='原生表格页',text=native_text,char_count=len(native_text),
                    has_image=False,extraction='native'),
               dict(number=2,title='另一扫描页',text='Explicit OCR fixture text',char_count=25,
                    has_image=True,extraction='ocr')]
    with store.transaction() as c:
        c.execute('UPDATE sources SET metadata=?,pages=?,status=? WHERE id=?',(json.dumps(metadata),json.dumps(pages),'attention',sid))


def test_legacy_ocr_document_uses_visual_engine_and_preserves_single_source_history(env):
    client,state,_,_=env
    pid=project(client);detail=upload(client,pid,'scan-fixture.txt',b'Explicit OCR fixture text')
    sid=detail['source']['id'];_mark_ocr_fixture(client,sid)
    body={'question':'Read the scan','source_id':sid,'mode':'document','request_id':str(uuid.uuid4())}
    first=wait_terminal(client,pid,submit(client,pid,body)['id'])
    assert first['status']=='completed' and first['mode']=='document'
    assert len(state['calls'])==1 and [s['id'] for s in state['calls'][0][0]]==[sid]
    second=wait_terminal(client,pid,submit(client,pid,{**body,'question':'And the next value?','request_id':str(uuid.uuid4()),'parent_answer_id':first['id']})['id'])
    assert second['status']=='completed' and second['mode']=='document'
    assert second['turn_index']==2 and len(state['calls'][-1][2])==1
    assert [s['id'] for s in state['calls'][-1][0]]==[sid]
    assert len(state['resolutions'])==1 and state['resolutions'][0][2]==sid
    assert state['resolutions'][0][1][0]['answer_id']==first['id']
    assert not client.app.state.answers.gateway_factory.calls  # Explicit resolver/engine stubs, no API.


@pytest.mark.parametrize('mode',['table','database'])
def test_legacy_ocr_table_scope_fails_before_models_or_widening_scope(env,mode):
    client,state,_,_=env
    pid=project(client);detail=upload(client,pid);sid=detail['source']['id'];table=detail['tables'][0]['name']
    _mark_ocr_fixture(client,sid,table)
    body={'question':'Read the amount','source_id':sid,'mode':mode,'request_id':str(uuid.uuid4())}
    if mode=='table':body['table_name']=table
    done=wait_terminal(client,pid,submit(client,pid,body)['id'])
    assert done['status']=='failed' and done['result'] is None
    assert '多源分析' in done['error'] and '未执行查询' in done['error']
    assert done['mode']==mode and done.get('table_name')==(table if mode=='table' else None)
    assert not state['calls'] and not client.app.state.answers.gateway_factory.calls


def test_known_native_table_in_mixed_ocr_source_keeps_selected_table_path(env):
    client,state,_,_=env
    pid=project(client);detail=upload(client,pid);sid=detail['source']['id'];table=detail['tables'][0]['name']
    _mark_ocr_fixture(client,sid,table,native_selected=True)
    body={'question':'Read the amount','source_id':sid,'mode':'table','table_name':table,'request_id':str(uuid.uuid4())}
    done=wait_terminal(client,pid,submit(client,pid,body)['id'])
    assert done['status']=='completed' and done['table_name']==table
    assert not state['calls']
    assert [purpose for purpose,_ in client.app.state.answers.gateway_factory.calls]==['table','verify','document']
    assert done['result']['rows']==[['A','0.10'],['B','0.20']]
    context=done['result']['evidence'][0]['table_context']['tables'][0]
    assert context['page']==1 and set(context['selected_cell_ids'])=={'R2C1','R2C2','R3C1','R3C2'}
    assert done['result']['verification']['table_relations']==[{'id':'C1','supported':True}]
    review=next(payload for purpose,payload in reversed(client.app.state.answers.gateway_factory.calls) if purpose=='verify')
    assert review['table_relation_claim_ids']==['C1']
