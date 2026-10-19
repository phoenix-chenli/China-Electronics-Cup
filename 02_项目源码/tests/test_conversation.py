"""Conversation isolation, immutable branches, and clarification resumption (offline)."""
import copy
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.query_planner import conversation_context, unresolved_result
from test_answers_api import configure, project, upload, request_body, submit, wait_terminal, FakeGatewayFactory


class FollowupFactory(FakeGatewayFactory):
    def __call__(self,config,cancelled):
        original=super().__call__(config,cancelled)
        factory=self
        class Gateway:
            models=original.models
            usage=original.usage
            def __call__(self,messages,purpose):
                payload=json.loads(messages[-1]['content'])
                if purpose=='verify' and 'interpretation' in payload:
                    return {'supported':True}
                if 'history' in payload and 'allowed_fields' not in payload:
                    factory.calls.append((purpose,payload))
                    return {'status':'ready','normalized_question':'列出项目和金额','intent':{'filters':['latest']}}
                return original(messages,purpose)
        return Gateway()


@pytest.fixture
def env(tmp_path):
    root=tmp_path/'run';configure(root);gateway=FollowupFactory()
    app=create_app(root,tmp_path/'samples',False,gateway)
    with TestClient(app) as client:yield client,gateway,root


def answer(client,pid,detail,parent=None,question='列出项目和金额',**updates):
    body=request_body(detail,question=question,**updates)
    if parent:body['parent_answer_id']=parent['id']
    return wait_terminal(client,pid,submit(client,pid,body)['id'])


def test_six_turn_branch_persists_full_context_and_current_intent(env):
    client,gateway,root=env;pid=project(client);detail=upload(client,pid)
    chain=[];parent=None
    for number in range(6):
        parent=answer(client,pid,detail,parent,question=f'第{number+1}轮')
        assert parent['status']=='completed',parent
        assert parent['turn_index']==number+1
        chain.append(parent)
    assert len({x['conversation_id'] for x in chain})==1
    assert chain[-1]['normalized_question']=='列出项目和金额'
    saved=client.get(f'/api/projects/{pid}/answers/{parent["id"]}/conversation').json()
    assert [a['id'] for a in saved]==[a['id'] for a in chain]
    resolutions=[p for _,p in gateway.calls if 'history' in p and 'allowed_fields' not in p]
    assert [len(p['history']) for p in resolutions]==[1,2,3,4,5]
    assert len(client.get(f'/api/projects/{pid}/answers').json())==6


def test_branch_isolation_new_question_and_changed_request_parent(env):
    client,gateway,_=env;pid=project(client);detail=upload(client,pid)
    first=answer(client,pid,detail)
    left=answer(client,pid,detail,first,'仅左边')
    right=answer(client,pid,detail,first,'仅右边')
    chain=client.get(f'/api/projects/{pid}/answers/{right["id"]}/conversation').json()
    assert [x['id'] for x in chain]==[first['id'],right['id']]
    assert '仅左边' not in json.dumps([p for _,p in gateway.calls if 'history' in p][-1],ensure_ascii=False)
    fresh=answer(client,pid,detail)
    assert fresh['turn_index']==1 and fresh['conversation_id']!=first['conversation_id']
    body=request_body(detail,parent_answer_id=first['id']);done=submit(client,pid,body)
    wait_terminal(client,pid,done['id'])
    response=client.post(f'/api/projects/{pid}/answers',json={**body,'parent_answer_id':right['id']})
    assert response.status_code==409


def test_parent_project_source_version_mode_and_status_guards(env):
    client,gateway,_=env;pid=project(client);other=project(client,'Other')
    detail=upload(client,pid);detail2=upload(client,other)
    first=answer(client,pid,detail)
    assert client.post(f'/api/projects/{other}/answers',json=request_body(detail2,parent_answer_id=first['id'])).status_code==409
    assert client.get(f'/api/projects/{other}/answers/{first["id"]}/conversation').status_code==404
    assert client.post(f'/api/projects/{pid}/answers',json=request_body(detail,parent_answer_id=first['id'],mode='database',table_name=None)).status_code==409
    with client.app.state.service.store.transaction() as c:
        c.execute('UPDATE sources SET parser_version=? WHERE id=?',('changed',detail['source']['id']))
    assert client.post(f'/api/projects/{pid}/answers',json=request_body(detail,parent_answer_id=first['id'])).status_code==409


def test_clarification_answer_keeps_original_question_and_choices(env):
    client,gateway,_=env;pid=project(client);detail=upload(client,pid)
    config=client.app.state.answers.config.load()
    original,_=client.app.state.answers.store.create(pid,client.app.state.service.store.get('sources',detail['source']['id']),'统计增长率','table',detail['tables'][0]['name'],str(uuid.uuid4()),config['models'])
    status,result=unresolved_result({'status':'needs_clarification','clarification':{'question':'比较哪两年？','options':['2024与2023','2023与2022'],'field':'基期'}})
    original=client.app.state.answers.store.update(pid,original['id'],status=status,result=result)
    child=answer(client,pid,detail,original,'2024与2023')
    assert child['status']=='completed'
    resolution=[p for _,p in gateway.calls if 'history' in p][-1]
    assert resolution['history'][0]['question']=='统计增长率'
    assert resolution['history'][0]['clarification']['field']=='基期'
    assert client.app.state.answers.store.get(pid,original['id'])['status']=='needs_clarification'


def test_rejected_followup_rewrite_stops_before_a_second_query(env):
    client,gateway,_=env;pid=project(client);detail=upload(client,pid)
    first=answer(client,pid,detail)
    original_factory=client.app.state.answers.gateway_factory
    reviews=[]
    def reject_rewrite(config,cancelled):
        original=original_factory(config,cancelled)
        class Gateway:
            models=original.models
            usage=original.usage
            def __call__(self,messages,purpose):
                payload=json.loads(messages[-1]['content'])
                if purpose=='verify' and 'interpretation' in payload:
                    reviews.append(payload)
                    return {'supported':False,'failure_kind':'invalid_plan','reason':'改写遗漏用户已明确的筛选条件'}
                return original(messages,purpose)
        return Gateway()
    client.app.state.answers.gateway_factory=reject_rewrite
    calls_before=len(gateway.calls)
    child=answer(client,pid,detail,first,'只看A项目')
    assert child['status']=='failed' and 'clarification' not in child['result']
    assert 'query' not in child['result'] and not child['result'].get('rows')
    assert len(reviews)==1 and len(gateway.calls)==calls_before+1
    assert reviews[0]['history'][0]['question']==first['question']


def test_conversation_survives_restart_without_replaying_models(env):
    client,gateway,root=env;pid=project(client);detail=upload(client,pid)
    first=answer(client,pid,detail);child=answer(client,pid,detail,first,'继续')
    # Read through a fresh store instance; main process still owns the runtime lock.
    from backend.store import Store
    from backend.answer_store import AnswerStore
    store=AnswerStore(Store(root))
    assert [x['id'] for x in store.conversation(pid,child['id'])]==[first['id'],child['id']]
    assert store.get(pid,child['id'])['normalized_question']=='列出项目和金额'


def test_context_budget_is_explicit_not_silent_truncation():
    chain=[{'question':'x'*4000,'result':{'text':'y'*1600},'status':'completed'} for _ in range(30)]
    with pytest.raises(ValueError,match='上文较长'):conversation_context(chain)
