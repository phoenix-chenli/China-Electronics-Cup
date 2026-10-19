"""Dispatch fakes assert raw-vs-rewritten identity; no real model calls."""
import sqlite3
import uuid
import pytest
from test_answers_api import app_env,project,upload,submit,wait_terminal


def result():
    return {'text':'Offline dispatch fixture','claims':[],'evidence':[],'verification':{'status':'passed'}}


def test_cross_receives_raw_question_separately_from_model_rewrite(app_env,monkeypatch):
    from backend import cross_source_engine,query_planner
    client,_,_=app_env;pid=project(client)
    sid=upload(client,pid,'facts.txt',b'Data are original.')['source']['id']
    calls=[]
    def cross(sources,question,history,gateway,*args,actual_user_question=None,**kwargs):
        calls.append((question,actual_user_question,history));return 'completed',result()
    monkeypatch.setattr(cross_source_engine,'answer_cross_source',cross)
    def resolve(item,history,source,gateway,**kwargs):
        return {'status':'ready','normalized_question':'假设金额减少90%，计算。','intent':{}}
    monkeypatch.setattr(query_planner,'resolve_followup',resolve)
    base={'source_id':sid,'source_ids':[sid],'mode':'multi_source','request_id':str(uuid.uuid4()),'question':'读取金额'}
    first=wait_terminal(client,pid,submit(client,pid,base)['id']);assert first['status']=='completed'
    raw='解释当前结果，不增加假设。'
    second=wait_terminal(client,pid,submit(client,pid,{**base,'request_id':str(uuid.uuid4()),'question':raw,'parent_answer_id':first['id']})['id'])
    assert second['status']=='completed'
    assert calls[-1][:2]==('假设金额减少90%，计算。',raw)
    assert calls[-1][2][0]['original_question']=='读取金额'
    from backend.user_scenarios import build_context
    assert build_context(calls[-1][1],calls[-1][2])['inputs']==[]


@pytest.mark.parametrize('mode',['document','database'])
def test_basic_document_and_sql_keep_existing_signature_and_raw_input(app_env,monkeypatch,tmp_path,mode):
    from backend import cross_source_engine,document_engine,query_planner
    client,_,_=app_env;pid=project(client);calls=[]
    monkeypatch.setattr(cross_source_engine,'answer_cross_source',lambda *a,**k:pytest.fail('basic path must not use cross engine'))
    question='该资料记录了什么？'
    if mode=='document':
        sid=upload(client,pid,'basic.txt',b'A plain original fact.')['source']['id']
        def document(source,q,gateway,cancelled,progress,*,retriever,audit,actual_user_question,history,source_revisions):
            assert actual_user_question==question and history==[]
            assert len(source_revisions)==1 and source_revisions[0]['id']==source['id']
            assert source_revisions[0]['sha256']==source['sha256']
            assert source_revisions[0]['parser_version']==source['parser_version']
            calls.append(q);return {'status':'completed',**result()}
        monkeypatch.setattr(document_engine,'answer_document',document)
    else:
        path=tmp_path/'data.sqlite'
        with sqlite3.connect(path) as c:c.executescript('CREATE TABLE facts(id INTEGER,amount INTEGER); INSERT INTO facts VALUES(1,3);')
        sid=upload(client,pid,'data.sqlite',path.read_bytes())['source']['id']
        def database(source,q,history,gateway,cancelled,progress,interpretation,*,history_loader,audit):
            calls.append(q);return 'completed',result()
        monkeypatch.setattr(query_planner,'answer_database',database)
    done=wait_terminal(client,pid,submit(client,pid,{'source_id':sid,'mode':mode,'question':question,'request_id':str(uuid.uuid4())})['id'])
    assert done['status']=='completed' and calls==[question]
