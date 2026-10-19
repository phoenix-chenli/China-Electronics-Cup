"""Terminal diagnostic append tests. All gateways are explicit offline fakes."""
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.answer_store import AnswerStore
from backend.store import Store
from backend.export_service import read_snapshot
from test_answers_api import app_env, project, upload, submit


@pytest.fixture
def stored(tmp_path):
    store = Store(tmp_path / 'store'); store.initialize()
    project_id = store.create_project('fixture', '')['id']
    other_project = store.create_project('other', '')['id']
    answers = AnswerStore(store); answers.initialize()
    item = {'id': 'answer-1', 'project_id': project_id, 'request_id': 'request-1',
            'status': 'failed', 'stage': '处理失败', 'error': 'original failure',
            'created_at': '2026-01-01', 'updated_at': '2026-01-02', 'elapsed_ms': 17,
            'source_id': 's1', 'sources': [{'id':'s1', 'sha256':'old', 'parser_version':'3.1'}],
            'context_state': {'slots': [{'value':'original'}]}, 'context_revision': 'original-revision',
            'result': {'claims':[{'id':'old'}], 'evidence':[{'id':'old-source'}],
                       'diagnostics': {'original_failure': {'supported':False}}, 'text':'old partial'}}
    with store.transaction() as connection:
        connection.execute('INSERT INTO answers VALUES(?,?,?,?)',
                           (item['id'], project_id, item['request_id'], json.dumps(item)))
    return answers, item, other_project


def rows(item):
    return item.get('diagnostics', {}).get('late_received_audits', [])


def test_terminal_append_preserves_entire_original_snapshot_and_detaches(stored):
    answers, before, _ = stored
    data = {'semantic_reviews':[{'supported':False, 'reason':'original received rejection'}]}
    original = copy.deepcopy(data)
    after = answers.append_terminal_audit(before['project_id'], before['id'], data)
    assert {key:value for key,value in after.items() if key != 'diagnostics'} == before
    assert rows(after)[0]['data'] == original
    assert rows(after)[0]['destination'] == 'query_diagnostics'
    assert rows(after)[0]['publication_status'] == 'diagnostic_only'
    data['semantic_reviews'].clear(); rows(after)[0]['data'].clear()
    assert rows(answers.get(before['project_id'], before['id']))[0]['data'] == original
    assert answers.events(before['project_id'], before['id']) == []
    # The ordinary generic update must still be unable to revive the job.
    unchanged = answers.update(before['project_id'], before['id'], status='completed', result={'claims':[{'id':'new'}]})
    assert unchanged['result'] == before['result'] and unchanged['status'] == 'failed'


def test_project_answer_identity_and_destination_are_strict(stored):
    answers, before, other = stored
    assert answers.append_terminal_audit(other, before['id'], {}) is None
    assert answers.append_terminal_audit(before['project_id'], 'absent', {}) is None
    with pytest.raises(ValueError):
        answers.append_terminal_audit(before['project_id'], before['id'], {}, destination='result')
    with pytest.raises(ValueError):
        answers.append_terminal_audit(before['project_id'], before['id'], [])
    assert answers.get(before['project_id'], before['id']) == before


@pytest.mark.parametrize('status', ['queued','running','completed','needs_clarification','insufficient_evidence'])
def test_only_cancelled_failed_accept_late_audit(stored, status):
    answers, before, _ = stored; before['status'] = status
    with answers.store.transaction() as connection:
        connection.execute('UPDATE answers SET body=? WHERE id=?', (json.dumps(before), before['id']))
    assert answers.append_terminal_audit(before['project_id'], before['id'], {'status':'completed'}) == before
    assert answers.get(before['project_id'], before['id']) == before


def test_concurrent_callbacks_across_store_instances_are_append_only_and_idempotent(stored):
    answers, before, _ = stored
    # Separate Python locks exercise SQLite's BEGIN IMMEDIATE serialization too.
    another = AnswerStore(Store(answers.store.root))
    def append(index):
        target = answers if index % 2 else another
        return target.append_terminal_audit(before['project_id'], before['id'],
                                             {'sequence':index % 12, 'received':True}, destination='diagnostics')
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(append, range(96)))
    after = answers.get(before['project_id'], before['id'])
    assert len(rows(after)) == 12
    assert {row['data']['sequence'] for row in rows(after)} == set(range(12))
    assert len({row['sha256'] for row in rows(after)}) == 12
    assert {key:value for key,value in after.items() if key != 'diagnostics'} == before


def test_same_receipt_updated_validation_is_added_not_overwritten(stored):
    answers, before, _ = stored
    first = {'document_responses':[{'sequence':1,'response':{'value':{'status':'ready'}}}]}
    last = copy.deepcopy(first); last['document_responses'][0]['envelope_errors'] = [{'field':'status'}]
    for data in (first, last, first, last):
        answers.append_terminal_audit(before['project_id'], before['id'], data, destination='diagnostics')
    assert [row['data'] for row in rows(answers.get(before['project_id'], before['id']))] == [first,last]


def test_storage_limit_is_explicit_and_never_evicts_original(stored, monkeypatch):
    from backend import answer_store
    answers, before, _ = stored
    monkeypatch.setattr(answer_store, 'MAX_LATE_AUDIT_CHARACTERS', 300)
    monkeypatch.setattr(answer_store, 'MAX_LATE_AUDITS', 2)
    answers.append_terminal_audit(before['project_id'], before['id'], {'first':True})
    answers.append_terminal_audit(before['project_id'], before['id'], {'large':'x'*1000})
    after = answers.append_terminal_audit(before['project_id'], before['id'], {'third':True})
    assert rows(after)[0]['data'] == {'first':True}
    assert rows(after)[1]['data_omitted'] is True and 'data' not in rows(after)[1]
    assert after['diagnostics']['late_received_audits_limit_reached'] is True
    assert len(rows(after)) == 2 and after['result'] == before['result']


def wait_worker(service, identifier):
    for _ in range(300):
        with service.lock:
            done = identifier not in service.events
        if done:
            return
        time.sleep(.01)
    pytest.fail('Controlled worker did not finish')


@pytest.mark.parametrize('terminal', ['cancelled', 'failed'])
def test_actual_document_callback_after_terminal_keeps_late_receipt_but_no_result(app_env, monkeypatch, terminal):
    client, _, runtime = app_env; pid = project(client)
    detail = upload(client, pid, 'manual.txt', '等于8件无需采购。'.encode())
    entered = threading.Event(); release = threading.Event(); calls = []
    class Gateway:
        models = {}; usage = []; attempts = []
        def __call__(self, messages, purpose):
            calls.append(purpose); entered.set()
            assert release.wait(5)
            return {'status':'ready','claims':[], 'api_key':'must-not-leak',
                    'message':'Bearer hidden-token /Users/example/private.txt'}
    service = client.app.state.answers
    monkeypatch.setattr(service, 'gateway_factory', lambda *a:Gateway())
    record = submit(client, pid, {'source_id':detail['source']['id'], 'mode':'document',
        'question':'等于8件是否采购？', 'request_id':str(uuid.uuid4())})
    try:
        assert entered.wait(5)
        if terminal == 'cancelled':
            response = client.post(f"/api/projects/{pid}/answers/{record['id']}/cancel")
            assert response.status_code == 200
        else:
            # Simulates a host failure racing with an already sent response.
            service.store.update(pid, record['id'], status='failed', stage='处理失败', error='original failure')
            assert not service.events[record['id']].is_set()
        before = service.store.get(pid, record['id'])
        events_before = service.store.events(pid, record['id'])
    finally:
        release.set()
    wait_worker(service, record['id'])
    after = service.store.get(pid, record['id'])
    assert {key:value for key,value in after.items() if key != 'diagnostics'} == before
    assert after['status'] == terminal and after['result'] is None and calls == ['document']
    assert service.store.events(pid, record['id']) == events_before
    receipts = [event['data']['document_responses'][0] for event in rows(after)]
    assert receipts and receipts[0]['response']['value']['status'] == 'ready'
    assert receipts[-1]['envelope_errors']
    assert all(event['publication_status'] == 'diagnostic_only' for event in rows(after))
    serialized = json.dumps(after)
    assert all(private not in serialized for private in ('must-not-leak','hidden-token','/Users/example'))
    public = client.get(f"/api/projects/{pid}/answers/{record['id']}").json()
    assert public['diagnostics'] == after['diagnostics'] and public['result'] is None
    exported = read_snapshot(runtime/'workspace.sqlite', pid, record['id'])['answers'][0]
    # Export applies its own redaction labels again, so private marker wording
    # may change; receipt identity and nonprivate diagnostic content must not.
    assert [r['sha256'] for r in rows(exported)] == [r['sha256'] for r in rows(after)]
    exported_receipts = [r['data']['document_responses'][0] for r in rows(exported)]
    assert all(r['response']['value']['status'] == 'ready' for r in exported_receipts)
    assert exported_receipts[-1]['envelope_errors'] == receipts[-1]['envelope_errors']
    assert exported['result'] is None and exported['status'] == terminal
    assert all(private not in json.dumps(exported) for private in ('must-not-leak','hidden-token','/Users/example'))


@pytest.mark.parametrize('mode,destination', [('database','query_diagnostics'), ('multi_source','diagnostics')])
def test_existing_sql_cross_callback_uses_same_late_terminal_path(app_env, monkeypatch, mode, destination):
    client, _, _ = app_env; pid = project(client); detail = upload(client, pid)
    entered = threading.Event(); release = threading.Event(); calls = []
    class Gateway:
        models = {}; usage = []; attempts = []
        def __call__(self, messages, purpose):
            calls.append(purpose); entered.set(); assert release.wait(5)
            return {'supported':False, 'reason':'received but rejected'}
    service = client.app.state.answers
    monkeypatch.setattr(service, 'gateway_factory', lambda *a:Gateway())
    data = {'semantic_reviews':[{'supported':False, 'reason':'received but rejected'}]}
    def fake_engine(*args, audit, **kwargs):
        gateway = args[3]
        gateway([], 'verify')
        audit(data); audit(copy.deepcopy(data))
        raise InterruptedError('late response cannot publish')
    if mode == 'database':
        monkeypatch.setattr('backend.query_planner.answer_database', fake_engine)
    else:
        monkeypatch.setattr('backend.cross_source_engine.answer_cross_source', fake_engine)
    body = {'source_id':detail['source']['id'], 'source_ids':[detail['source']['id']],
            'mode':mode, 'question':'列出项目金额', 'request_id':str(uuid.uuid4())}
    record = submit(client, pid, body)
    try:
        assert entered.wait(5)
        assert client.post(f"/api/projects/{pid}/answers/{record['id']}/cancel").status_code == 200
        before = service.store.get(pid, record['id'])
    finally:
        release.set()
    wait_worker(service, record['id'])
    after = service.store.get(pid, record['id'])
    assert {key:value for key,value in after.items() if key != 'diagnostics'} == before
    assert calls == ['verify'] and len(rows(after)) == 1
    assert rows(after)[0]['data'] == data and rows(after)[0]['destination'] == destination
    assert after['status'] == 'cancelled' and after['result'] is None


def test_store_defends_against_unredacted_sql_diagnostic_caller(stored):
    answers,before,_=stored
    data={'semantic_reviews':[{'review_response':{'supported':False,'api_key':'synthetic-private-marker','endpoint':'https://synthetic-provider.invalid/v1',
            'reason':'api_key="synthetic quote with spaces" /Users/synthetic-owner/private.txt'},'sha256':'a'*64,'rows':[[0,12.5]]}]}
    original=copy.deepcopy(data)
    after=answers.append_terminal_audit(before['project_id'],before['id'],data)
    assert data==original
    assert {k:v for k,v in after.items()if k!='diagnostics'}==before
    payload=rows(after)[0]['data'];record=payload['semantic_reviews'][0]
    assert record['review_response']['supported'] is False and record['sha256']=='a'*64 and record['rows']==[[0,12.5]]
    serialized=json.dumps(after)
    assert all(x not in serialized for x in ['synthetic-private-marker','synthetic-provider','synthetic quote with spaces','synthetic-owner'])


@pytest.mark.parametrize('cancel_before_receipt',[False,True])
def test_actual_sql_callback_sanitizes_running_and_cancelled_snapshots(app_env,monkeypatch,cancel_before_receipt):
    client,_,_=app_env;pid=project(client);detail=upload(client,pid)
    entered=threading.Event();release=threading.Event();received=threading.Event();finish=threading.Event()
    service=client.app.state.answers
    data={'semantic_reviews':[{'review_response':{'supported':False,'api_key':'synthetic-private-marker','endpoint':'https://synthetic-provider.invalid/v1',
         'reason':'password="synthetic quoted secret" /Users/synthetic-owner/private.txt'},'sha256':'b'*64,'rows':[[7]]}]}
    before_data=copy.deepcopy(data)
    def engine(*args,audit,**kwargs):
        entered.set();assert release.wait(5);audit(data);received.set();assert finish.wait(5)
        raise InterruptedError('controlled stop')
    monkeypatch.setattr('backend.query_planner.answer_database',engine)
    answer=submit(client,pid,{'source_id':detail['source']['id'],'mode':'database','question':'列出项目金额','request_id':str(uuid.uuid4())})
    try:
        assert entered.wait(5)
        if cancel_before_receipt:assert client.post(f"/api/projects/{pid}/answers/{answer['id']}/cancel").status_code==200
        release.set();assert received.wait(5)
        record=client.get(f"/api/projects/{pid}/answers/{answer['id']}").json()
        text=json.dumps(record)
        assert all(x not in text for x in ['synthetic-private-marker','synthetic-provider','synthetic quoted secret','synthetic-owner'])
        if cancel_before_receipt:
            assert record['status']=='cancelled' and record['result'] is None
            saved=rows(record)[0]['data']
        else:saved=record['result']['query_diagnostics']
        assert saved['semantic_reviews'][0]['review_response']['supported'] is False
        assert saved['semantic_reviews'][0]['sha256']=='b'*64
        assert data==before_data
    finally:release.set();finish.set()
    wait_worker(service,answer['id'])
