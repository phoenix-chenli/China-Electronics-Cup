"""Synthetic received envelopes; rag-033's discarded original is not recreated."""
import copy
import hashlib
import json

import pytest

from backend import document_engine as e, document_protocol as p
from backend.document_receipts import ReceivedResponses, received_snapshot, MAX_RECEIPT_CHARACTERS
from backend.model_gateway import ModelError
from test_document_protocol_repair import source, request, claim, verified
from test_answers_api import app_env, project, upload, submit, wait_terminal


def good(payload):
    return {'status':'completed', 'claims':[claim(payload, '等于8件无需采购')]}


def run(model, audit=None, cancelled=lambda:False):
    return e.answer_document(source(), '等于8件是否采购？', model, cancelled, lambda _:None, audit=audit)


@pytest.mark.parametrize('bad', [
    [], 'not an object', None, {'claims':[]}, {'status':[],'claims':[]},
    {'status':'ready','claims':[]}, {'status':'completed','claims':{}},
    {'status':'completed','claims':[]}, {'status':'completed','claims':[{}]*17},
])
def test_received_envelope_is_feedback_then_fully_verified(bad):
    calls=[];events=[];original=copy.deepcopy(bad)
    def model(messages,purpose):
        payload=request(messages);calls.append((purpose,payload))
        if len(calls)==1:return bad
        if purpose=='document':
            assert payload['envelope_feedback']
            return good(payload)
        return verified(payload)
    result=run(model,events.append)
    assert result['status']=='completed' and [c[0] for c in calls]==['document','document','verify']
    assert result['claims'][0]['text']=='等于8件无需采购' and bad==original
    records=result['diagnostics']['document_responses']
    assert records[0]['response']['value']==original
    assert records[0]['envelope_errors'] and len(records)==3
    assert events[-1]['document_responses']==records
    assert all(r['publication_status']=='not_adopted' for r in records)


def test_normal_response_keeps_two_calls_and_original_evidence():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        return good(payload) if purpose=='document' else verified(payload)
    result=run(model)
    assert calls==['document','verify'] and result['status']=='completed'
    assert result['evidence'][0]['quote']=='等于8件无需采购'


@pytest.mark.parametrize('distinct,expected',[(False,2),(True,3)])
def test_repeated_or_no_progress_empty_claims_stop_without_verifier(distinct,expected):
    calls=[];events=[]
    def model(messages,purpose):
        calls.append(purpose)
        return {'status':'completed','claims':[], 'note':len(calls) if distinct else 'same'}
    with pytest.raises(e.DocumentEngineError):run(model,events.append)
    assert calls==['document']*expected
    assert events[-1]['document_responses'][-1]['stop_reason']==('no_progress' if distinct else 'duplicate_envelope')
    assert events[-1]['document_responses'][0]['response']['value']['claims']==[]


def test_mechanical_envelope_progress_can_reach_valid_fourth_response():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if len(calls)==1:return []
        if len(calls)==2:return {'claims':[]}
        if len(calls)==3:return {'status':'completed','claims':[]}
        return good(payload) if purpose=='document' else verified(payload)
    result=run(model)
    assert calls==['document']*4+['verify'] and result['status']=='completed'


def test_envelope_repair_does_not_bypass_semantic_rejection():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if len(calls)==1:return {'claims':[]}
        return good(payload) if purpose=='document' else verified(payload,supported=False,complete=False)
    result=run(model)
    assert calls==['document','document','verify'] and result['claims']==[]
    assert result['status']=='insufficient_evidence'
    assert result['diagnostics']['document_responses'][-1]['response']['value']['claims'][0]['supported'] is False


@pytest.mark.parametrize('error',[TimeoutError('unknown POST'),ModelError('503 received','http_503'),ModelError('bad service JSON','response_json_failed')])
def test_failure_after_received_envelope_is_not_replayed(error):
    calls=[];events=[]
    def model(messages,purpose):
        calls.append(purpose)
        if len(calls)==1:return {'status':'ready','claims':[]}
        raise error
    with pytest.raises(type(error)):run(model,events.append)
    assert calls==['document','document']
    assert len(events[-1]['document_responses'])==1
    assert events[-1]['document_responses'][0]['response']['value']['status']=='ready'


def test_first_transport_failure_is_not_treated_as_received_response():
    calls=[];events=[]
    def model(*args):calls.append(1);raise ModelError('unknown POST','read_timeout')
    with pytest.raises(ModelError):run(model,events.append)
    assert len(calls)==1 and events==[]


def test_received_malformed_response_is_saved_before_cancellation():
    calls=[];events=[];cancel=[False]
    def model(*args):calls.append(1);cancel[0]=True;return {'claims':[]}
    with pytest.raises(e.DocumentCancelled):run(model,events.append,lambda:cancel[0])
    assert len(calls)==1 and events[-1]['document_responses'][0]['envelope_errors']


def test_cancelled_before_first_call_sends_nothing():
    with pytest.raises(e.DocumentCancelled):run(lambda *a:pytest.fail('No POST'),cancelled=lambda:True)


def test_candidate_and_received_verdict_persist_before_later_coverage_failure():
    calls=[];events=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':return good(payload)
        if len(calls)==2:return {'claims':[{'id':'c1','supported':True}],'answer_complete':True}
        raise TimeoutError('unknown coverage POST')
    with pytest.raises(TimeoutError):run(model,events.append)
    assert calls==['document','verify','verify']
    assert [r['purpose'] for r in events[-1]['document_responses']]==['document','verify']
    assert 'coverage' not in events[-1]['document_responses'][1]['response']['value']


def test_citation_repair_then_verify_503_keeps_two_received_candidates():
    calls=[];events=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if len(calls)==1:
            value=good(payload);value['claims'][0]['citations'][0]['chunk_id']='wrong-id'
            return value
        if purpose=='document':return good(payload)
        raise ModelError('received HTTP503','http_503')
    with pytest.raises(ModelError):run(model,events.append)
    assert calls==['document','document','verify']
    receipts=events[-1]['document_responses'];assert len(receipts)==2
    assert receipts[0]['response']['value']['claims'][0]['citations'][0]['chunk_id']=='wrong-id'


def test_snapshot_redacts_secrets_and_paths_with_original_digest_and_no_mutation():
    raw={'status':'ready','claims':[], 'api_key':'NEVER-SAVE', 'analysis':'private reasoning',
         'message':'Bearer abcdef /Users/example/private.txt password=hunter2',
         'nested':{'endpoint':'https://provider.example/v1','payload':'data:image/png;base64,abcd'}}
    original=copy.deepcopy(raw);snapshot=received_snapshot(raw);text=json.dumps(snapshot)
    assert all(value not in text for value in ('NEVER-SAVE','private reasoning','hunter2','abcdef','/Users/example','provider.example','base64,abcd'))
    assert snapshot['redacted'] and raw==original
    assert snapshot['sha256']==hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    huge=received_snapshot({'status':'ready','claims':[{'text':'x'*30000}]*1000})
    assert huge['truncated'] and len(json.dumps(huge,ensure_ascii=False))<=MAX_RECEIPT_CHARACTERS
    escaped=received_snapshot({'claims':[{'text':'\\"'*3000}]*24})
    assert escaped['truncated'] and len(json.dumps(escaped,ensure_ascii=False))<=MAX_RECEIPT_CHARACTERS


def test_history_bound_preserves_first_and_latest_and_explicit_omission():
    receipts=ReceivedResponses(lambda *a:{'claims':[]},None,lambda:None,16)
    for _ in range(70):receipts([], 'document')
    out=receipts.snapshot()
    assert len(out['document_responses'])==64 and out['document_response_omissions']==6
    assert [out['document_responses'][0]['sequence'],out['document_responses'][-1]['sequence']]==[1,70]


def test_request_budget_stops_before_correction_and_retains_receipt(monkeypatch):
    events=[];calls=[]
    def model(*args):
        calls.append(1);monkeypatch.setattr(p,'MAX_REQUEST_CHARACTERS',1)
        return {'claims':[]}
    with pytest.raises(e.DocumentEngineError):run(model,events.append)
    assert len(calls)==1 and events[-1]['document_responses'][-1]['stop_reason']=='request_budget'


@pytest.mark.parametrize('cancel',[False,True])
def test_document_host_persists_receipts_without_publishing_candidates(app_env,monkeypatch,cancel):
    client,_,_=app_env;pid=project(client)
    detail=upload(client,pid,'manual.txt','等于8件无需采购。'.encode())
    calls=[]
    class Gateway:
        models={};usage=[];attempts=[]
        def __call__(self,messages,purpose):
            calls.append(purpose)
            if len(calls)==1:return {'status':'ready','claims':[], 'api_key':'must-not-leak'}
            if cancel:raise InterruptedError('controlled cancellation')
            raise ModelError('controlled unknown POST','read_timeout')
    monkeypatch.setattr(client.app.state.answers,'gateway_factory',lambda *a:Gateway())
    # Importer's retrieval service remains real; only the gateway is explicit fake.
    import uuid
    record=wait_terminal(client,pid,submit(client,pid,{'source_id':detail['source']['id'],
        'mode':'document','question':'等于8件是否采购？','request_id':str(uuid.uuid4())})['id'])
    assert record['status']==('cancelled' if cancel else 'failed'),record
    result=record['result'];assert result['claims']==[] and result['evidence']==[] and result['result_tables']==[]
    assert result['verification']['status']=='pending'
    assert result['diagnostics']['document_responses'][0]['response']['value']['status']=='ready'
    assert 'must-not-leak' not in json.dumps(result)
    again=client.get(f"/api/projects/{pid}/answers/{record['id']}").json()
    assert again['result']['diagnostics']==result['diagnostics'] and calls==['document','document']


def test_successful_document_host_retains_initial_error_after_final_result(app_env,monkeypatch):
    client,_,_=app_env;pid=project(client)
    detail=upload(client,pid,'manual.txt','等于8件无需采购。'.encode());calls=[]
    class Gateway:
        models={};usage=[];attempts=[]
        def __call__(self,messages,purpose):
            payload=request(messages);calls.append(purpose)
            if 'allowed_fields' in payload:return {'status':'unavailable'}
            if len(calls)==1:return {'claims':[]}
            return good(payload) if purpose=='document' else verified(payload)
    monkeypatch.setattr(client.app.state.answers,'gateway_factory',lambda *a:Gateway())
    import uuid
    record=wait_terminal(client,pid,submit(client,pid,{'source_id':detail['source']['id'],
        'mode':'document','question':'等于8件是否采购？','request_id':str(uuid.uuid4())})['id'])
    assert record['status']=='completed',record
    result=record['result'];assert result['claims'][0]['text']=='等于8件无需采购'
    receipts=result['diagnostics']['document_responses']
    assert len(receipts)==3 and receipts[0]['envelope_errors']==[{'field':'status','code':'invalid_status'}]
    assert receipts[0]['response']['value']=={'claims':[]}
    again=client.get(f"/api/projects/{pid}/answers/{record['id']}").json()
    assert again['result']['diagnostics']==result['diagnostics']
