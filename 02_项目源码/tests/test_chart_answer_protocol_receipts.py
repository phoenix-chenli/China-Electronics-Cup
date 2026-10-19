"""Synthetic received-response protocols; no model/network or accuracy claims."""
import copy
import hashlib
import json

import pytest

from backend import chart_answer_engine as engine
from backend.document_receipts import MAX_RECEIPT_CHARACTERS
from backend.model_gateway import ModelError
from test_chart_answer_engine import Gateway, candidate, payload, reading, responses, review_for, source


def ask(gateway, audit, *, body=None, cancelled=lambda: False, progress=lambda _: None, system='本地测试协议'):
    def validate(raw):
        return (raw['ok'], []) if isinstance(raw, dict) and raw.get('ok') is True else (None, ['ok须为true'])
    return engine._ask(gateway, 'document', system, body or {'question': '受控测试'}, validate,
                       cancelled, progress, '本地协议测试', audit)


def test_rejected_candidate_reaches_correction_without_becoming_evidence(source):
    bad={'claims':[candidate('2021年收入是999亿元。')]}
    good={'claims':[candidate()]}
    before=copy.deepcopy(bad)
    gateway=Gateway(*responses()[:2], bad, good, review_for)
    result=engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    correction=payload(gateway.calls[3][0])['protocol_correction']
    receipt=correction['previous_rejected_candidate']
    assert receipt['value']==bad and receipt['sha256']==hashlib.sha256(json.dumps(bad, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert correction['errors'] and '不是来源' in correction['instruction']
    assert result['status']=='completed' and result['claims'][0]['text']==candidate()['text']
    assert [p for _,p in gateway.calls]==['document','verify','document','document','verify']
    events=result['diagnostics']['answer_attempts']
    assert events[0]['received_snapshot']['value']==bad
    assert events[0]['publication_status']=='diagnostic_only'
    assert '999' not in json.dumps(result['claims'],ensure_ascii=False)
    assert bad==before


def test_correction_still_obeys_independent_negative_verdict(source):
    gateway=Gateway(*responses()[:2], {}, {'claims':[candidate()]},
                    lambda m,p: review_for(m,p,supported=False,coverage_status='missing_evidence'))
    result=engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['claims']==[] and result['status']=='insufficient_evidence'
    assert len(gateway.calls)==5
    assert result['verification']['chart_relations']==[{'id':'C1','supported':False}]


def test_same_received_error_stops_after_one_correction_and_keeps_both_receipts():
    bad={'claims':'invalid'}
    audit=[];gateway=Gateway(bad,bad)
    assert ask(gateway,audit) is None
    assert len(gateway.calls)==2 and len(audit)==2
    assert audit[1]['repeated_rejected_response'] is True
    assert audit[0]['received_snapshot']['sha256']==audit[1]['received_snapshot']['sha256']


@pytest.mark.parametrize('code',['transport_failed','response_json_failed','timeout','http_failed'])
@pytest.mark.parametrize('after_received',[False,True])
def test_unknown_or_transport_failure_is_never_replayed(code,after_received):
    audit=[]; failure=ModelError('explicit synthetic failure',code)
    gateway=Gateway(*([{'claims':'invalid'}] if after_received else []),failure)
    with pytest.raises(ModelError) as exc:
        ask(gateway,audit)
    assert exc.value is failure
    assert len(gateway.calls)==1+int(after_received) and len(audit)==int(after_received)
    if after_received:
        assert audit[0]['received_snapshot']['value']=={'claims':'invalid'}


@pytest.mark.parametrize('code',['schema_failed','content_json_failed','output_truncated'])
def test_completed_protocol_exception_has_explicit_missing_snapshot_and_may_correct(code):
    audit=[];gateway=Gateway(ModelError('opaque response',code), {'ok':True})
    assert ask(gateway,audit) is True
    assert len(gateway.calls)==2
    assert audit[0]['received_snapshot_unavailable']==code
    assert 'received_snapshot' not in audit[0]
    assert payload(gateway.calls[1][0])['protocol_correction']['previous_rejected_candidate_omitted']=='gateway_did_not_expose_response'


def test_cancel_with_returned_response_keeps_receipt_and_does_not_correct():
    state={'cancelled':False};audit=[]
    def returned(m,p):
        state['cancelled']=True
        return {'claims':'invalid'}
    gateway=Gateway(returned)
    with pytest.raises(InterruptedError):
        ask(gateway,audit,cancelled=lambda:state['cancelled'])
    assert len(gateway.calls)==1 and len(audit)==1
    assert audit[0]['received_snapshot']['value']=={'claims':'invalid'}


def test_cancel_from_progress_does_not_start_post():
    state={'cancelled':False};audit=[];gateway=Gateway()
    with pytest.raises(InterruptedError):
        ask(gateway,audit,cancelled=lambda:state['cancelled'],progress=lambda _:state.update(cancelled=True))
    assert gateway.calls==[] and audit==[]


def test_receipts_and_correction_redact_credentials_and_local_paths():
    secret='synthetic secret with spaces'
    raw={'api_key':secret,'claims':[{'text':'api_key="'+secret+'" /Users/private/source.pdf Bearer fake-token'}],
         'analysis':{'private_reasoning':'do not retain'},'extra':{'password':'not-real'}}
    before=copy.deepcopy(raw);audit=[];gateway=Gateway(raw, {'ok':True})
    assert ask(gateway,audit) is True
    serialized=json.dumps([audit,payload(gateway.calls[1][0])],ensure_ascii=False)
    for forbidden in [secret,'/Users/private/source.pdf','fake-token','do not retain','not-real']:
        assert forbidden not in serialized
    assert audit[0]['received_snapshot']['redacted'] is True
    assert raw==before


@pytest.mark.parametrize('space',[1000,200])
def test_long_source_context_omits_only_audit_projection_explicitly(space):
    system='本地测试协议'
    # Leave little room after the original evidence; no true evidence is cut.
    body={'question':'受控测试','reviewed_items':['原始已核验转录'+('源'*(engine.MAX_CONTEXT-space-100))]}
    original=copy.deepcopy(body)
    raw={'claims':[{'text':'长'*700,'extra':'"\\'*1000} for _ in range(12)]}
    audit=[];gateway=Gateway(raw,{'ok':True})
    assert ask(gateway,audit,body=body,system=system) is True
    second=payload(gateway.calls[1][0]); correction=second['protocol_correction']
    assert second['reviewed_items']==original['reviewed_items'] and body==original
    assert len(system)+len(json.dumps(second,ensure_ascii=False))<=engine.MAX_CONTEXT
    snapshot=correction.get('previous_rejected_candidate')
    assert (snapshot and snapshot['truncated'] and snapshot['feedback_budget_limited']) or correction.get('previous_rejected_candidate_omitted')=='context_budget'
    assert len(json.dumps(audit[0]['received_snapshot'],ensure_ascii=False))<=MAX_RECEIPT_CHARACTERS
    assert audit[0]['received_snapshot']['truncated'] is True


def test_no_space_for_minimum_correction_stops_without_second_request():
    system='x';body={'evidence':'原'*engine.MAX_CONTEXT}
    body['evidence']=body['evidence'][:engine.MAX_CONTEXT-len(system)-len(json.dumps({'evidence':''},ensure_ascii=False))-1]
    gateway=Gateway({'claims':'invalid'});audit=[]
    with pytest.raises(ValueError, match='纠错上下文'):
        ask(gateway,audit,body=body,system=system)
    assert len(gateway.calls)==1 and len(audit)==1


def test_user_zero_threshold_is_not_added_to_numeric_allowlist(source):
    bad={'claims':[candidate('2021年一般公共预算收入高于0亿元。')]}
    gateway=Gateway(*responses()[:2],bad,bad)
    result=engine.answer_chart(source, '第1页图1收入是否高于0亿元？', gateway)
    assert result['status']=='failed' and not result['claims']
    assert [purpose for _,purpose in gateway.calls]==['document','verify','document','document']
    assert '提问阈值' in gateway.calls[2][0][0]['content']
    assert all('增加了' in row['errors'][0] for row in result['diagnostics']['answer_attempts'])


def test_successful_response_has_receipt_without_extra_call():
    audit=[];gateway=Gateway({'ok':True})
    assert ask(gateway,audit) is True
    assert len(gateway.calls)==1 and audit[0]['received_snapshot']['value']=={'ok':True}
