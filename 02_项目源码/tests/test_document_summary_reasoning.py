"""In-place reasoning routing; all network is replaced by MockTransport."""
import copy
import json

import httpx
import pytest

from backend import document_engine as engine
from backend.model_gateway import ModelGateway,ModelError
from test_model_gateway import config
from test_document_user_context import document,SCOPE,ACTUAL
from test_document_request_context import RAW,NORMALIZED,history


def reply(payload):
    if 'chunks' in payload:
        chunk=next(c for c in payload['chunks'] if ACTUAL in c['text'])
        return {'status':'completed','claims':[{'text':'月末实盘为11件。','citations':[{'chunk_id':chunk['chunk_id'],'quote':ACTUAL}]}]}
    return {'claims':[{'id':c['id'],'supported':True} for c in payload['claims']],'answer_complete':True,
            'coverage':[{'question_id':q['id'],'status':'answered','claim_ids':[c['id'] for c in payload['claims']]} for q in payload['question_parts']]}


def setup_gateway(config,monkeypatch,handler=None):
    value={**config,'models':dict.fromkeys(config['models'],'deepseek-flash'),'timeout':90}
    sent=[]
    def handle(request):
        body=json.loads(request.content);sent.append(body)
        result=handler(body) if handler else reply(json.loads(body['messages'][-1]['content']))
        return httpx.Response(200,json={'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(handle)))
    return ModelGateway(value),sent


def run(gateway,*,raw=RAW,h=None,audit=None,cancelled=lambda:False):
    return engine.answer_document(document(),NORMALIZED,gateway,cancelled,lambda _:None,
             actual_user_question=raw,history=history() if h is None else h,source_revisions=SCOPE,audit=audit)


def test_real_gateway_summary_uses_two_existing_high_calls_with_audit_and_480_wait(config,monkeypatch):
    g,sent=setup_gateway(config,monkeypatch);events=[];result=run(g,audit=events.append)
    assert result['status']=='completed' and len(sent)==2
    assert all(b['thinking']=={'type':'enabled'} and b['reasoning_effort']=='high' for b in sent)
    assert all(a['request_settings']['reasoning_effort']=='high' and a['effective_timeout_seconds']==480 for a in g.attempts)
    assert [a['purpose'] for a in g.attempts]==['document','verify']
    assert len(result['diagnostics']['document_responses'])==2 and events[-1]['document_responses'][-1]['received']
    assert result['diagnostics']['document_execution_policy']['additional_model_calls']==0
    assert g._reasoning_effort is None
    # A later ordinary request uses the original disabled mode on this same gateway.
    run(g,raw='当前库存是多少？',h=[])
    assert len(sent)==4 and all(b['thinking']=={'type':'disabled'} and 'reasoning_effort' not in b for b in sent[2:])


@pytest.mark.parametrize('kind',['no_history','ordinary','wrong_scope','failed_tail','wrong_branch'])
def test_no_new_reasoning_for_single_turn_or_untrusted_summary_history(config,monkeypatch,kind):
    h=history();raw=RAW
    if kind=='no_history':h=[]
    elif kind=='ordinary':raw='当前库存是多少？'
    elif kind=='wrong_scope':h[-1]['source_revisions']=[{**SCOPE[0],'sha256':'b'*64}]
    elif kind=='failed_tail':h[-1]['status']='failed'
    else:h[-1]['parent_answer_id']='wrong-branch'
    g,sent=setup_gateway(config,monkeypatch);result=run(g,raw=raw,h=h)
    assert len(sent)==2 and all(b['thinking']=={'type':'disabled'} for b in sent)
    assert not result['diagnostics']['document_execution_policy']['summary_with_trusted_history']
    assert all(a['effective_timeout_seconds']==90 for a in g.attempts)


def test_unknown_post_failure_has_one_high_attempt_no_normal_mode_fallback(config,monkeypatch):
    def fail(body):raise httpx.ReadTimeout('controlled unknown outcome')
    g,sent=setup_gateway(config,monkeypatch,fail)
    with pytest.raises(ModelError):run(g)
    assert len(sent)==len(g.attempts)==1 and sent[0]['reasoning_effort']=='high'
    assert g._reasoning_effort is None


def test_cancellation_after_received_high_response_keeps_receipt_and_stops_verify(config,monkeypatch):
    g,sent=setup_gateway(config,monkeypatch);cancel=[False];events=[]
    def audit(data):events.append(data);cancel[0]=True
    with pytest.raises(engine.DocumentCancelled):run(g,audit=audit,cancelled=lambda:cancel[0])
    assert len(sent)==1 and len(events[-1]['document_responses'])==1
    assert events[-1]['document_responses'][0]['received'] is True


def test_high_protocol_correction_is_same_existing_bounded_flow(config,monkeypatch):
    calls=[]
    def respond(body):
        p=json.loads(body['messages'][-1]['content']);calls.append(p)
        return {'status':'completed','claims':'invalid'} if len(calls)==1 else reply(p)
    g,sent=setup_gateway(config,monkeypatch,respond);result=run(g)
    assert result['status']=='completed' and len(sent)==3
    assert all(b['reasoning_effort']=='high' for b in sent)
    assert result['diagnostics']['document_responses'][0]['envelope_errors']


def test_high_does_not_bypass_unsupported_numeric_fact(config,monkeypatch):
    def unsupported(body):
        p=json.loads(body['messages'][-1]['content']);result=reply(p)
        if 'chunks' in p:result['claims'][0]['text']='当前库存为7件。'
        return result
    g,sent=setup_gateway(config,monkeypatch,unsupported);result=run(g)
    assert result['status']=='insufficient_evidence' and not result['claims']
    assert all(a['purpose']=='document' for a in g.attempts)
    assert all(b['reasoning_effort']=='high' for b in sent)
