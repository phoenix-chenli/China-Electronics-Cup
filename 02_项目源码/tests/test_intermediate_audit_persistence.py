"""Received candidate checks survive a later failure; no real model calls."""
import copy
import pytest
from backend.model_gateway import ModelError
from test_answers_api import app_env,project,upload,request_body,submit,wait_terminal


@pytest.mark.parametrize('cancelled',[False,True])
@pytest.mark.parametrize('engine',['database','multi_source'])
def test_received_sql_check_survives_later_unknown_or_cancel(app_env,monkeypatch,cancelled,engine):
    client,_,_=app_env
    pid=project(client);source=upload(client,pid)
    received={'semantic_reviews':[{'candidate_index':1,'sql':'SELECT amount FROM facts',
        'parameters':[],'review_response':{'supported':False,'reason':'Wrong population'},
        'execution_preview':{'columns':['amount'],'rows':[['0.10']],
                             'returned_rows':1,'truncated':False}}]}
    if engine=='multi_source':received={'audit_history':[{'sequence':1,'kind':'candidate_rejected','record':{'publication_status':'not_adopted','review':{'supported':False}}}]}
    field='query_diagnostics' if engine=='database' else 'diagnostics'
    original=copy.deepcopy(received)
    def fake_answer_database(*args,audit,**kwargs):
        audit(received)
        # The host persists a detached snapshot, not this mutable local object.
        received.clear()
        if cancelled:raise InterruptedError('controlled cancellation')
        raise ModelError('controlled unknown POST outcome','read_timeout')
    monkeypatch.setattr('backend.query_planner.answer_database' if engine=='database' else 'backend.cross_source_engine.answer_cross_source',fake_answer_database)
    record=wait_terminal(client,pid,submit(client,pid,request_body(source,mode=engine,table_name=None))['id'])
    assert record['status']==('cancelled' if cancelled else 'failed')
    result=record['result']
    assert result[field]==original
    assert result['claims']==[] and result['evidence']==[] and result['result_tables']==[]
    assert result['verification']['status']=='pending'
    assert 'rows' not in result and 'query' not in result
    # Reopening uses the saved audit, without any new gateway request.
    again=client.get(f"/api/projects/{pid}/answers/{record['id']}").json()
    assert again['result'][field]==original
