"""Contract/version/event invariants. Model behavior is explicitly simulated."""
import copy
import json

from backend.answer_contract import public_record, prepare_result, evidence_graph
from test_answers_api import app_env, project, upload, request_body, submit, wait_terminal


def test_legacy_projection_keeps_saved_outcome_and_does_not_mutate():
    legacy = dict(id='old', status='completed', result={'text':'historic', 'claims':[], 'evidence':[]})
    before = copy.deepcopy(legacy)
    value = public_record(legacy)
    assert legacy == before
    assert value['status'] == 'completed'
    assert value['schema_version'] == 'zhiheng.answer.legacy'
    assert value['result']['coverage']['complete'] is None
    assert value['context_revision'] is None
    assert value['events'] == []
    assert value['result']['projection_only'] is True


def test_rejected_query_never_becomes_published_result_table():
    result = {'columns':['amount'], 'rows':[[999]], 'claims':[], 'evidence':[],
              'query':{'sql':'select amount from t', 'executed_result':{'rows':[[999]]}}}
    assert prepare_result(result, 'failed')['result_tables'] == []
    assert prepare_result(result, 'insufficient_evidence')['result_tables'] == []
    ready = prepare_result(result, 'completed')
    assert ready['result_tables'][0]['rows'] == [[999]]
    assert ready['coverage']['status'] == 'not_recorded'


def test_graph_preserves_page_precision_derivation_and_dangling_refs():
    result = {'evidence':[{'id':'E1','source_id':'s1','sha256':'abc','parser_version':'p1',
                            'page':2,'localization_scope':'original_page','bbox':[0,0,1,1],
                            'start':0,'end':4,'quote':'12.0'}],
              'calculations':[{'id':'F1','evidence_ids':['E1']}],
              'claims':[{'id':'C1','evidence_ids':['E1'],'calculation_ids':['F1']},
                        {'id':'C2','evidence_ids':['missing']}],
              'source_groups':[{'group_id':'original','source_ids':['s1','converted']} ]}
    graph = evidence_graph(result)
    assert next(n for n in graph['nodes'] if n['id']=='evidence:E1')['precision']=='page'
    assert {'source':'calculation:F1','target':'claim:C1','relation':'derives'} in graph['edges']
    assert graph['unresolved']==[{'kind':'claim_evidence','owner':'C2','reference':'missing'}]
    assert graph['source_groups']==result['source_groups']


def test_new_contract_events_roundtrip_export_and_project_isolation(app_env):
    client, gateway, _ = app_env
    pid = project(client); detail = upload(client,pid)
    record = wait_terminal(client,pid,submit(client,pid,request_body(detail))['id'])
    assert record['schema_version']=='zhiheng.answer.v6'
    assert record['source_revisions']==record['sources']
    assert record['result']['result_tables'][0]['rows']==record['result']['rows']
    saved = client.app.state.answers.store.get(pid,record['id'])
    assert saved['result']==record['result']
    events = record['events']
    assert events[0]['kind']=='created' and events[-1]['status']=='completed'
    assert [e['sequence'] for e in events]==list(range(1,len(events)+1))
    assert all(e['since_previous_event_ms']>=0 for e in events)
    first = client.get(f'/api/projects/{pid}/answers/{record["id"]}/events?limit=2').json()
    assert first['items']==events[:2] and first['has_more']
    second = client.get(f'/api/projects/{pid}/answers/{record["id"]}/events?after=2').json()
    assert second['items']==events[2:]
    foreign=project(client,'foreign')
    assert client.get(f'/api/projects/{foreign}/answers/{record["id"]}/events').status_code==404
    exported = client.get(f'/api/projects/{pid}/answers/{record["id"]}/export?format=json').json()
    assert exported['execution_events'][record['id']]==events
    assert 'fake-secret' not in json.dumps(exported)
    assert client.app.state.answers.store.get(pid,record['id'])==saved


def test_cancellation_is_terminal_for_events_and_late_results(app_env):
    client,gateway,_=app_env
    gateway.block_on='table'
    pid=project(client); detail=upload(client,pid)
    record=submit(client,pid,request_body(detail))
    assert gateway.entered.wait(2)
    done=client.post(f'/api/projects/{pid}/answers/{record["id"]}/cancel').json()
    gateway.release.set()
    before=done['events']
    client.app.state.answers.store.update(pid,record['id'],status='completed',stage='bad late result',result={'text':'fake'})
    final=client.get(f'/api/projects/{pid}/answers/{record["id"]}').json()
    assert final['status']=='cancelled' and final['result'] is None
    assert final['events']==before and before[-1]['status']=='cancelled'


def test_interruption_event_records_recovery_without_network(app_env):
    client,gateway,_=app_env
    pid=project(client); detail=upload(client,pid)
    store=client.app.state.answers.store
    source=client.app.state.service.store.get('sources',detail['source']['id'])
    item,_=store.create(pid,source,'pending','document',None,'interrupted',{})
    store.initialize()
    interrupted=store.get(pid,item['id'])
    assert interrupted['status']=='failed'
    assert store.events(pid,item['id'])[-1]['kind']=='interrupted'
    store.initialize()
    assert len(store.events(pid,item['id']))==2
    assert not gateway.calls
