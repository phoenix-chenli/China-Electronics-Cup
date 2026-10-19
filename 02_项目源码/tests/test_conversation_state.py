import copy
import json
import uuid

import pytest
from backend.conversation_state import build_initial, finalize_state, validate_patch, context_history
from test_answers_api import app_env, project, upload, request_body, submit, wait_terminal


def test_replace_add_remove_keep_user_provenance_and_parent_immutable():
    parent={'id':'p','context_state':build_initial('p','原问题',[],patch=[{'op':'replace','field':'time_range','value':'2024'},
        {'op':'replace','field':'filters','value':['地区A']}])}
    original=copy.deepcopy(parent)
    state=build_initial('c','改成2025，加入地区B',[],parent,[{'op':'replace','field':'time_range','value':'2025'},
        {'op':'add','field':'filters','value':['地区B']},{'op':'remove','field':'time_range'}])
    assert parent==original
    assert 'time_range' not in state['slots']
    assert state['slots']['filters']['value']==['地区A','地区B']
    assert state['slots']['filters']['source_answer_id']=='c'
    assert state['slots']['filters']['confirmed_by_user']
    assert [p['operation'] for p in state['changes']]==['replace','add','remove']


def test_model_interpretation_is_attributed_but_not_user_confirmation():
    item={'id':'a','question':'看2024年金额','sources':[],'intent':{'time_range':'2024','metric':'amount'}}
    item['context_state']=build_initial('a',item['question'],[])
    state=finalize_state(item,'completed',{'verification':{'status':'passed'},'claims':[]})
    slot=state['slots']['time_range']
    assert slot['source_answer_id']=='a' and slot['user_question']==item['question']
    assert slot['origin']=='model_interpretation' and not slot['confirmed_by_user']
    failed=finalize_state(item,'failed',{'verification':{'status':'limited'}})
    assert failed['slots']=={}


def test_removed_filter_cannot_return_from_model_and_uncertain_compression_stops():
    item={'id':'a','question':'不要地区筛选','sources':[],
          'context_patch':[{'op':'remove','field':'filters'}],'intent':{'filters':['A']}}
    item['context_state']=build_initial('a',item['question'],[],patch=item['context_patch'])
    state=finalize_state(item,'completed',{'verification':{'status':'passed'}})
    assert 'filters' not in state['slots'] and state['unresolved']
    with pytest.raises(ValueError):validate_patch([{'op':'add','field':'time_range','value':'2024'}])
    with pytest.raises(ValueError):validate_patch([{'op':'remove','field':'filters','value':[]}])


def test_long_context_preserves_current_slots_and_exact_retrieval_ids():
    chain=[]
    parent=None
    for i in range(36):
        turn={'id':str(i),'question':'查询金额','status':'completed','sources':[],
              'intent':{'metric':'amount','time_range':'2024'},'result':{'text':'x'*1700,'rows':[[j] for j in range(30)],'verification':{'status':'passed'}}}
        turn['context_state']=build_initial(turn['id'],turn['question'],[],parent)
        turn['context_state']=finalize_state(turn,'completed',turn['result'])
        chain.append(turn);parent=turn
    history=context_history(chain)
    assert len(history)==9 and history[0]['kind']=='structured_context'
    assert history[0]['omitted_answer_ids']==[str(i) for i in range(28)]
    assert history[0]['context_state']['slots']['time_range']['source_answer_id']=='0'
    assert history[-1]['answer_id']=='35' and history[-1]['history_projection']['rows_omitted']==22
    old=[{'question':'x'*4000,'result':{'text':'y'*1600},'status':'completed'} for _ in range(30)]
    with pytest.raises(ValueError,match='上文较长'):context_history(old)


def test_patch_version_guard_and_request_idempotency(app_env):
    client,_,_=app_env;pid=project(client);detail=upload(client,pid)
    first=wait_terminal(client,pid,submit(client,pid,request_body(detail))['id'])
    store=client.app.state.answers.store;source=client.app.state.service.store.get('sources',first['source_id'])
    source=client.app.state.answers.semantics.inject(source)
    patch=[{'op':'replace','field':'time_range','value':'2024'}]
    with pytest.raises(ValueError,match='口径已变化'):
        store.create(pid,source,'new','table',first['table_name'],'id1',{},first['id'],[source],patch,'0'*64)
    child,new=store.create(pid,source,'new','table',first['table_name'],'id1',{},first['id'],[source],patch,first['context_revision'])
    assert new and child['context_state']['slots']['time_range']['value']=='2024'
    same,new=store.create(pid,source,'new','table',first['table_name'],'id1',{},first['id'],[source],patch,first['context_revision'])
    assert not new and same['id']==child['id']
    with pytest.raises(ValueError,match='调整发生变化'):
        store.create(pid,source,'new','table',first['table_name'],'id1',{},first['id'],[source],[],first['context_revision'])


def test_paginated_history_reaches_older_than_50_and_is_branch_isolated(app_env):
    client,gateway,_=app_env;pid=project(client);detail=upload(client,pid)
    store=client.app.state.answers.store;source=client.app.state.service.store.get('sources',detail['source']['id'])
    parent=None;ids=[]
    for i in range(62):
        item,_=store.create(pid,source,f'query {i}','table',detail['tables'][0]['name'],str(uuid.uuid4()),{},parent['id'] if parent else None)
        parent=store.update(pid,item['id'],status='completed',result={'text':'explicit fixture','claims':[],'evidence':[]})
        ids.append(item['id'])
    first=client.get(f'/api/projects/{pid}/answer-index?limit=50').json()
    second=client.get(f'/api/projects/{pid}/answer-index?before={first["next_before"]}').json()
    assert len(first['items'])==50 and len(second['items'])==12 and not second['has_more']
    assert [x['id'] for x in first['items']+second['items']]==ids[::-1]
    chain=store.conversation(pid,ids[-1]);assert [x['id'] for x in chain]==ids
    exported=client.get(f'/api/projects/{pid}/answers/{ids[-1]}/export?format=json&scope=conversation').json()
    assert len(exported['answers'])==62
    other=project(client,'other')
    assert client.get(f'/api/projects/{other}/answer-index?conversation_id={ids[0]}').json()['items']==[]
    literal=client.get(f'/api/projects/{pid}/answer-index?search=%25').json()
    assert literal['items']==[]
    assert not gateway.calls


def test_explicit_correction_resolves_only_its_own_old_conflict():
    parent={'id':'p','context_state':build_initial('p','原问题',[])}
    old=[{'field':'time_range','kind':'interpretation_differs_from_user_patch','source_answer_id':'p'},
         {'field':'filters','kind':'removed_condition_reintroduced','source_answer_id':'p'}]
    parent['context_state']['unresolved']=old
    state=build_initial('c','年份改为2025',[],parent,[{'op':'replace','field':'time_range','value':'2025'}])
    assert state['unresolved']==[old[1]]
    assert state['resolved_conflicts'][0]['resolved_from']==[old[0]]
    assert parent['context_state']['unresolved']==old
    later=build_initial('d','只添加条件',[],{'id':'c','context_state':state},[{'op':'add','field':'filters','value':['地区A']}])
    assert later['unresolved']==[old[1]]
    cleared=build_initial('e','明确撤销筛选',[],{'id':'c','context_state':state},[{'op':'remove','field':'filters'}])
    assert cleared['unresolved']==[] and 'filters' not in cleared['slots']
