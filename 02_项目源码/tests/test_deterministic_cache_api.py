"""Actual FastAPI/worker/cache wiring with an explicit fake semantic reviewer."""
import json,uuid
from backend.sql_engine import execute_sql
from backend.parsers import quoted
from test_answers_api import app_env, project, upload, submit, wait_terminal


def test_api_reuses_mechanical_sql_but_reviews_each_answer_and_archive_evicts(app_env,monkeypatch):
    from backend import query_planner,sql_engine
    client,gateway,_=app_env;pid=project(client);detail=upload(client,pid);sid=detail['source']['id'];executions=[]
    real=sql_engine._execute_sql_uncached
    def actual(*a,**k):executions.append(1);return real(*a,**k)
    monkeypatch.setattr(sql_engine,'_execute_sql_uncached',actual)
    def fake_plan(source,question,history,model,*args,**kwargs):
        table=source['tables'][0]['name'];data=execute_sql(source,f'SELECT amount FROM {quoted(table)}')
        review=model([{'role':'user','content':json.dumps({'mechanical_result':data})}],'verify')
        accepted=review['supported'] is True
        return ('completed' if accepted else 'insufficient_evidence'),{'text':'Explicit fake acceptance' if accepted else 'Explicit fake rejection',
            'query':data,'rows':data['rows'],'columns':data['columns'],'evidence':data['evidence'],
            'claims':[{'id':'C1','text':'Explicit fake acceptance','evidence_ids':['E1']}] if accepted else [],
            'verification':{'status':'passed' if accepted else 'limited','checks':['Explicit fake semantic review, no external model']},'notes':[]}
    monkeypatch.setattr(query_planner,'answer_database',fake_plan)
    def ask():
        return wait_terminal(client,pid,submit(client,pid,{'question':'列出金额','source_id':sid,'mode':'database','request_id':str(uuid.uuid4())})['id'])
    a=ask();b=ask()
    assert a['status']==b['status']=='completed'
    assert a['result']['query']['cache']['status']=='miss_stored'
    assert b['result']['query']['cache']['status']=='hit'
    gateway.semantic_support=False;c=ask()
    assert c['status']=='insufficient_evidence' and not c['result']['claims']
    assert len(executions)==1 and sum(p=='verify' for p,_ in gateway.calls)==3
    assert client.app.state.answers.deterministic_cache.entries
    assert client.post(f'/api/sources/{sid}/archive').status_code==200
    assert not client.app.state.answers.deterministic_cache.entries
