"""Strict per-source retrieval checks and whole-scope publication, no model API."""
import collections
import copy
import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.retrieval import RetrievalService
from backend.deterministic_cache import mechanical_call
from test_answers_api import configure, project, upload, submit, wait_terminal, FakeGatewayFactory


def run_case(tmp_path, monkeypatch, scenario, count=2):
    root=tmp_path/'runtime';configure(root)
    app=create_app(root,tmp_path/'samples',False,FakeGatewayFactory(),
        retrieval_factory=lambda path:RetrievalService(path,default_mode='lexical'))
    state={'inside':False,'checked':[]}
    def engine(sources,question,history,gateway,cancelled,progress,*,retrieval_factory=None,**kwargs):
        state['inside']=True
        try:scenario(sources,retrieval_factory,state,app)
        finally:state['inside']=False
        return 'completed',{'text':'Explicit offline fixture result','claims':[],
            'evidence':[],'notes':[],'verification':{'status':'passed','checks':['fake']},'steps':[]}
    monkeypatch.setitem(sys.modules,'backend.cross_source_engine',types.SimpleNamespace(answer_cross_source=engine))
    with TestClient(app) as client:
        pid=project(client)
        ids=[upload(client,pid,f'doc-{i}.txt',f'Document {i} unique fact.'.encode())['source']['id'] for i in range(count)]
        state['unselected']=upload(client,pid,'not-selected.txt',b'Unselected private fact.')['source']['id']
        original_inject=app.state.answers.semantics.inject
        def tracked(source):
            if state['inside']:state['checked'].append(source['id'])
            return original_inject(source)
        monkeypatch.setattr(app.state.answers.semantics,'inject',tracked)
        body={'question':'核对所选资料','source_id':ids[0],'source_ids':ids,
              'mode':'multi_source','request_id':str(uuid.uuid4())}
        done=wait_terminal(client,pid,submit(client,pid,body)['id'])
    return done,state


@pytest.mark.parametrize('change',['pages','original_bytes'])
def test_current_source_is_strictly_rechecked_on_every_select(tmp_path,monkeypatch,change):
    def scenario(sources,factory,state,app):
        source=sources[0];retriever=factory(source);retriever.prepared()
        first,_=retriever.select('fact',limit=4);assert first
        if change=='pages':
            with app.state.service.store.transaction() as c:
                c.execute('UPDATE sources SET pages=? WHERE id=?',
                    (json.dumps([{'number':1,'text':'Changed body'}]),source['id']))
        else:
            path=Path(source['original_path']);stat=path.stat();path.chmod(0o600)
            path.write_bytes(b'Changed original bytes.')
            os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))  # Same mtime cannot bypass content hash.
        with pytest.raises(ValueError,match='变化'):
            retriever.select('fact',limit=4)
    done,_=run_case(tmp_path,monkeypatch,scenario)
    assert done['status']=='failed' and done['result'] is None


def test_other_selected_source_change_is_rejected_before_publication(tmp_path,monkeypatch):
    def scenario(sources,factory,state,app):
        retriever=factory(sources[0]);retriever.prepared()
        with app.state.service.store.transaction() as c:
            c.execute('UPDATE sources SET parser_version=? WHERE id=?',('changed',sources[1]['id']))
        found,_=retriever.select('fact',limit=4)
        assert found and found[0].text==sources[0]['pages'][0]['text']
        state['local_read_completed']=True
    done,state=run_case(tmp_path,monkeypatch,scenario)
    assert state['local_read_completed']
    assert done['status']=='failed' and done['result'] is None and '版本' in done['error']


def test_unselected_or_replaced_snapshot_cannot_borrow_callback(tmp_path,monkeypatch):
    def scenario(sources,factory,state,app):
        foreign=app.state.service.store.get('sources',state['unselected'])
        with pytest.raises(ValueError,match='选定范围'):factory(foreign)
        forged=copy.deepcopy(sources[0]);forged['pages'][0]['text']='Different snapshot'
        with pytest.raises(ValueError,match='快照'):factory(forged)
        assert not state['checked']  # Neither attempt got a content-check callback or index.
    done,_=run_case(tmp_path,monkeypatch,scenario)
    assert done['status']=='completed'


def test_cross_source_mechanical_cache_still_checks_the_whole_scope(tmp_path,monkeypatch):
    def scenario(sources,factory,state,app):
        with app.state.service.store.transaction() as c:
            c.execute('UPDATE sources SET parser_version=? WHERE id=?',('changed',sources[1]['id']))
        computed=[]
        with pytest.raises(ValueError,match='版本'):
            mechanical_call('scope-test',{'n':1},lambda:computed.append(True),source_ids=[sources[0]['id']])
        assert computed==[]
    done,_=run_case(tmp_path,monkeypatch,scenario)
    assert done['status']=='failed' and done['result'] is None


def test_twelve_small_sources_keep_index_results_with_linear_check_count(tmp_path,monkeypatch):
    def scenario(sources,factory,state,app):
        state['generations']=[]
        for source in sources:
            retriever=factory(source)
            chunks,_=retriever.prepared()
            found,_=retriever.select('unique fact',limit=4)
            assert [(c.id,c.page,c.start,c.end,c.text) for c in found]==[(c.id,c.page,c.start,c.end,c.text) for c in chunks]
            assert found[0].text==source['pages'][0]['text']
            state['generations'].append(retriever.audit()['index_generation'])
            assert retriever.service.store.active(source['project_id'],source['id'],retriever.generation)
    done,state=run_case(tmp_path,monkeypatch,scenario,count=12)
    assert done['status']=='completed' and len(set(state['generations']))==12
    scope_width=int(os.environ.get('FRESHNESS_EXPECTED_WIDTH','1'))
    assert len(state['checked'])==6*12*scope_width
    assert set(collections.Counter(state['checked']).values())=={6*scope_width}
    if path:=os.environ.get('FRESHNESS_COUNT_OUTPUT'):
        Path(path).write_text(json.dumps({'scope_width':scope_width,'sources':12,
            'strict_source_checks':len(state['checked']),'checks_per_source':6*scope_width,
            'evidence_and_active_generation_checks_passed':True,'model_api_calls':0},indent=2)+'\n')
