"""Real local API/storage, controlled answer fixtures, no model network calls."""
import copy
import json
import sqlite3
import time
import uuid
from test_answers_api import app_env, project, upload, request_body, submit, wait_terminal


def completed(env):
    client,_,_=env;pid=project(client);detail=upload(client,pid)
    answer=wait_terminal(client,pid,submit(client,pid,request_body(detail))['id'])
    assert answer['status']=='completed'
    return client,pid,detail,answer


def raw(root,aid):
    with sqlite3.connect(root/'workspace.sqlite') as c:return c.execute('SELECT body FROM answers WHERE id=?',(aid,)).fetchone()[0]


def test_favorite_isolated_idempotent_and_does_not_rewrite_answer(app_env):
    client,pid,_,answer=completed(app_env);other=project(client,'other');before=raw(app_env[2],answer['id'])
    path=f'/api/projects/{pid}/answers/{answer["id"]}/favorite'
    assert client.put(path,json={'enabled':True}).status_code==200
    assert client.put(path,json={'enabled':True}).status_code==200
    assert len(client.get(f'/api/projects/{pid}/favorites').json())==1
    assert client.put(path.replace(pid,other),json={'enabled':True}).status_code==404
    assert client.put(path,json={'enabled':'yes'}).status_code==422
    assert client.put(path,json={'enabled':False}).status_code==200
    assert client.get(f'/api/projects/{pid}/favorites').json()==[]
    assert raw(app_env[2],answer['id'])==before


def test_draft_tabs_optimistic_revision_and_project_boundaries(app_env):
    client,pid,detail,answer=completed(app_env);other=project(client,'other');sid=detail['source']['id']
    path=f'/api/projects/{pid}/workspace/tab_123456_branch1'
    assert client.get(path).json()['revision']==0
    state={'draft':'比较上一轮的地区？','source_ids':[sid],'active_answer_id':answer['id'],'branch_id':answer['id'],'selected_page':1,'scroll_top':321}
    assert client.put(path,json={'state':state,'expected_revision':0}).json()['revision']==1
    assert client.put(path,json={'state':{'draft':'late'},'expected_revision':0}).status_code==409
    assert client.get(path).json()['state']==state
    assert client.get(path.replace('tab_123456_branch1','tab_654321_branch1')).json()['state']=={}
    assert client.put(path.replace(pid,other),json={'state':state,'expected_revision':0}).status_code==409
    assert client.put(path,json={'state':{'scroll_top':True},'expected_revision':1}).status_code==409


def test_brief_is_immutable_escaped_and_version_bound(app_env):
    client,pid,detail,answer=completed(app_env);aid=answer['id'];before=raw(app_env[2],aid)
    path=f'/api/projects/{pid}/briefs'
    result=client.post(path,json={'title':'分析 <script>alert(1)</script>','answer_ids':[aid]})
    assert result.status_code==201,result.text
    bid=result.json()['id'];html=client.get(path+'/'+bid)
    assert html.status_code==200 and '<script>' not in html.text and '&lt;script&gt;' in html.text
    assert answer['result']['claims'][0]['text'].replace('\n','<br>') in html.text
    assert 'background:#fff' in html.text and 'default-src' in html.text
    stored=client.get(path+'/'+bid+'?format=json').json()
    assert stored['record_metadata'][aid]['stored_body_sha256'] and stored['answers'][0]['id']==aid
    other=project(client,'other');assert client.get(path.replace(pid,other)+'/'+bid).status_code==404
    assert client.post(path,json={'title':'重复','answer_ids':[aid,aid]}).status_code==409
    source=client.app.state.service.store.get('sources',detail['source']['id'])
    with client.app.state.service.store.transaction() as c:
        c.execute('UPDATE sources SET parser_version=? WHERE id=?',('changed-parser',source['id']))
    assert client.post(path,json={'title':'已过期','answer_ids':[aid]}).status_code==409
    assert client.get(path+'/'+bid).content==html.content
    assert raw(app_env[2],aid)==before


def test_suggestion_accept_fills_without_new_answer_or_model_call(app_env):
    client,pid,detail,answer=completed(app_env);aid=answer['id'];gateway=app_env[1]
    count=len(gateway.calls);answer_count=len(client.get(f'/api/projects/{pid}/answers').json())
    response=client.post(f'/api/projects/{pid}/answers/{aid}/suggestions',json={})
    assert response.status_code==202,response.text
    record=response.json();path=f'/api/projects/{pid}/suggestions/{record["key"]}'
    for _ in range(100):
        record=client.get(path).json()
        if record['state']!='preparing':break
        time.sleep(.01)
    assert record['state']=='ready',record
    choice=record['suggestions'][0]
    accepted=client.post(path+'/accept',json={'suggestion_id':choice['id'],'expected_scope':record['scope']})
    assert accepted.status_code==200,accepted.text
    assert accepted.json()['fill_text']==choice['text']
    assert len(gateway.calls)==count and len(client.get(f'/api/projects/{pid}/answers').json())==answer_count
    assert client.post(path+'/accept',json={'suggestion_id':choice['id'],'expected_scope':record['scope']}).status_code==409
    assert client.put(f'/api/projects/{pid}/suggestion-preference',json={'enabled':False}).status_code==200
    assert client.get(f'/api/projects/{pid}/suggestion-preference').json()['enabled'] is False


def test_scenario_overrides_are_assumptions_and_cannot_rewrite_formula(app_env):
    client,pid,detail,answer=completed(app_env);answers=client.app.state.answers
    source=answers.semantics.inject(client.app.state.service.store.get('sources',detail['source']['id']))
    item,_=answers.store.create(pid,source,'按 a*b 计算','table',detail['tables'][0]['name'],str(uuid.uuid4()),{},sources=[source])
    result=copy.deepcopy(answer['result']);eid=result['evidence'][0]['id']
    result['claims'][0]['calculation_ids']=['calc1']
    result['calculations']=[{'id':'calc1','expression':'a*b','parameters':[{'name':'a','value':'100','unit':'元','evidence_ids':[eid]}, {'name':'b','value':'0.2','unit':'1','evidence_ids':[eid]}],
                             'value':'20.0','unit':'元','display_value':'20.00','precision':2,'rule_origin':'user_question','evidence_ids':[eid],'rule_evidence_ids':[]}]
    saved=answers.store.update(pid,item['id'],status='completed',stage='回答完成',result=result)
    before=raw(app_env[2],item['id']);path=f'/api/projects/{pid}/answers/{item["id"]}/scenarios'
    body={'calculation_id':'calc1','expected_context_revision':saved['context_revision'],'overrides':{'b':{'value':'0.3','unit':'1'}}}
    response=client.post(path,json=body);assert response.status_code==201,response.text
    value=response.json();assert value['scenario']['value']=='30.0' and value['delta']['value']=='10.0'
    assert value['changes'][0]['is_observed_fact'] is False and '不是预测' in value['boundary']
    assert client.post(path,json={**body,'expression':'a/b'}).status_code==422
    assert client.post(path,json={**body,'overrides':{'c':{'value':'4','unit':'1'}}}).status_code==409
    assert client.post(path,json={**body,'overrides':{'b':{'value':'NaN','unit':'1'}}}).status_code==409
    assert raw(app_env[2],item['id'])==before
    assert len(client.get(f'/api/projects/{pid}/scenarios?answer_id={item["id"]}').json())==1


def test_default_auto_mode_selects_database_and_keeps_parent_scope(app_env):
    client,_,_=app_env;pid=project(client);detail=upload(client,pid)
    answers=client.app.state.answers
    # Submission/route selection is checked independently of the fake table-only planner.
    request=request_body(detail);request.pop('mode');request.pop('table_name')
    answer=submit(client,pid,request)
    assert answer['mode']=='database' and answer['table_name'] is None
    wait_terminal(client,pid,answer['id'])
