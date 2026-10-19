"""Business-definition changes invalidate meaning, without rewriting originals."""
import uuid
from test_answers_api import app_env, project, upload, request_body, submit, wait_terminal


def definition(detail):
    return {'id':'amount-label','kind':'field','label':'实收金额','definition':'amount表示本文件的实收金额，单位为元。',
            'aliases':['实收'],'unit':'元','references':[{'table':detail['tables'][0]['name'],'column':'amount'}]}


def confirm(client,detail):
    sid=detail['source']['id']
    state=client.get(f'/api/sources/{sid}/semantics').json()
    response=client.patch(f'/api/sources/{sid}/semantics',json={'action':'confirm','confirmed':True,
        'expected_revision':state['revision'],'expected_source_revision':state['source_revision'],'definition':definition(detail)})
    assert response.status_code==200,response.text
    return response.json()


def test_confirmation_injects_actual_plan_scope_and_stales_old_answers(app_env):
    client,gateway,_=app_env;pid=project(client);detail=upload(client,pid)
    first=wait_terminal(client,pid,submit(client,pid,request_body(detail))['id'])
    before=client.get(f'/api/sources/{detail["source"]["id"]}/original').content
    state=confirm(client,detail)
    assert state['revision']==1 and state['confirmed'][0]['origin']=='user_confirmed'
    old=client.get(f'/api/projects/{pid}/answers/{first["id"]}').json()
    assert old['scope_stale'] and old['result']['evidence'][0]['stale']
    exported=client.get(f'/api/projects/{pid}/answers/{first["id"]}/export?format=json').json()
    reasons=exported['record_metadata'][first['id']]['freshness']['sources'][0]['reasons']
    assert '业务定义版本已变化' in reasons
    assert client.get(f'/api/projects/{pid}/answers/{first["id"]}/export?format=csv').status_code==409
    assert client.post(f'/api/projects/{pid}/answers',json=request_body(detail,parent_answer_id=first['id'])).status_code==409
    second=wait_terminal(client,pid,submit(client,pid,request_body(detail))['id'])
    assert second['status']=='completed' and not second['scope_stale']
    assert second['sources'][0]['semantic_revision']==state['semantic_revision']
    plan=[payload for purpose,payload in gateway.calls if purpose=='table'][-1]
    assert plan['table']['semantic_layer']['definitions'][0]['label']=='实收金额'
    assert client.get(f'/api/sources/{detail["source"]["id"]}/original').content==before


def test_semantic_change_while_running_cannot_publish_old_meaning(app_env):
    client,gateway,_=app_env;pid=project(client);detail=upload(client,pid)
    gateway.block_on='table'
    item=submit(client,pid,request_body(detail));assert gateway.entered.wait(2)
    confirm(client,detail);gateway.release.set()
    done=wait_terminal(client,pid,item['id'])
    assert done['status']=='failed' and done['result'] is None


def test_candidate_is_not_confirmed_and_revocation_keeps_event(app_env):
    client,_,_=app_env;pid=project(client);detail=upload(client,pid);sid=detail['source']['id']
    current=client.get(f'/api/sources/{sid}/semantics').json()
    payload={'action':'save_candidate','expected_revision':0,'expected_source_revision':current['source_revision'],'definition':definition(detail)}
    response=client.patch(f'/api/sources/{sid}/semantics',json=payload)
    assert response.status_code==200 and not response.json()['confirmed']
    assert client.patch(f'/api/sources/{sid}/semantics',json={**payload,'action':'confirm','expected_revision':1}).status_code==409
    state=confirm(client,detail)
    revoked=client.patch(f'/api/sources/{sid}/semantics',json={'action':'revoke','expected_revision':state['revision'],
        'expected_source_revision':state['source_revision'],'definition_id':'amount-label'})
    assert revoked.status_code==200 and not revoked.json()['confirmed']
    events=client.get(f'/api/sources/{sid}/semantics/events').json()
    assert [e['action'] for e in events]==['save_candidate','confirm','revoke']
    assert events[-1]['before']['label']=='实收金额'
