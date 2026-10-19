"""Received chart callback contracts using scripted replies, never model calls."""
import copy
import json

import pytest

from backend import chart_answer_engine as engine, chart_receipts
from backend.chart_receipts import ChartResponses
from backend.document_receipts import MAX_RECEIPT_CHARACTERS
from backend.model_gateway import ModelError
from test_chart_answer_engine import (Gateway, candidate, payload, reading, responses, review_for,
                                     source, detailed_source, _detail_relation, _final_denial_prefix, _text_coverage)


@pytest.mark.parametrize('failure',[ModelError('unknown POST','transport_failed'), ModelError('503','http_failed'),
                                    ModelError('timeout','timeout'),InterruptedError('cancelled')])
def test_early_read_receipt_survives_next_request_failure(source,failure):
    saved=[];first=responses()[0];gateway=Gateway(first,failure)
    with pytest.raises(type(failure)):
        engine.answer_chart(source,'第1页图1收入是多少？',gateway,audit=saved.append)
    assert len(gateway.calls)==2
    assert saved[-1]['chart_responses']==saved[0]['chart_responses']
    assert saved[-1]['chart_responses'][0]['response']['value']==first
    assert saved[-1]['chart_response_omissions']==0
    assert all(not {'claims','evidence','text'} & set(s) for s in saved)


def test_first_unknown_request_does_not_invent_received_response(source):
    saved=[];gateway=Gateway(ModelError('unknown','transport_failed'))
    with pytest.raises(ModelError):
        engine.answer_chart(source,'第1页图1收入是多少？',gateway,audit=saved.append)
    assert len(gateway.calls)==1 and saved==[]


def test_every_successful_phase_retained_in_callback_and_normal_result(source):
    saved=[];gateway=Gateway(*responses())
    result=engine.answer_chart(source,'第1页图1收入是多少？',gateway,audit=saved.append)
    records=result['diagnostics']['chart_responses']
    assert result['status']=='completed' and len(gateway.calls)==len(records)==4
    assert [r['sequence'] for r in records]==[1,2,3,4]
    assert [r['purpose'] for r in records]==['document','verify','document','verify']
    assert records==saved[-1]['chart_responses']
    assert all(r['publication_status']=='not_adopted' and r['received'] for r in records)
    assert 'envelope_errors' not in json.dumps(records)
    # The second emit before composing includes processing since reader return.
    prior=[s for s in saved if len(s['chart_responses'])==2]
    assert any(s['chart_response_progress'].get('value',{}).get('readings') for s in prior)
    assert all(len(json.dumps(r['response'],ensure_ascii=False))<=MAX_RECEIPT_CHARACTERS for r in records)


def test_late_reader_return_during_cancellation_is_emitted_before_interruption(source):
    saved=[];state={'cancelled':False}
    def returned(m,p):
        state['cancelled']=True
        return responses()[0]
    gateway=Gateway(returned)
    with pytest.raises(InterruptedError):
        engine.answer_chart(source,'第1页图1收入是多少？',gateway,lambda:state['cancelled'],audit=saved.append)
    assert len(gateway.calls)==1
    assert saved[-1]['chart_responses'][0]['response']['value']==responses()[0]


def test_callback_cancellation_prevents_next_request(source):
    saved=[];state={'cancelled':False};gateway=Gateway(responses()[0])
    def persist(data):
        saved.append(data);state['cancelled']=True
    with pytest.raises(InterruptedError):
        engine.answer_chart(source,'第1页图1收入是多少？',gateway,lambda:state['cancelled'],audit=persist)
    assert len(gateway.calls)==1 and len(saved[-1]['chart_responses'])==1


def test_callback_failure_blocks_new_requests_without_replaying_receipt(source):
    gateway=Gateway(responses()[0]);seen=[]
    def persist(data):
        seen.append(data)
        raise RuntimeError('synthetic local persistence failure')
    with pytest.raises(RuntimeError,match='persistence'):
        engine.answer_chart(source,'第1页图1收入是多少？',gateway,audit=persist)
    assert len(gateway.calls)==1 and len(seen)==1


def test_read_protocol_error_and_repair_are_receipts_not_document_envelopes(source):
    saved=[];gateway=Gateway({'items':'bad'},*responses())
    result=engine.answer_chart(source,'第1页图1收入是多少？',gateway,audit=saved.append)
    records=result['diagnostics']['chart_responses']
    assert result['status']=='completed' and len(records)==5
    assert records[0]['response']['value']=={'items':'bad'}
    assert records[1]['response']['value']==responses()[0]
    assert 'envelope_errors' not in json.dumps(records)
    assert saved[-1]['chart_responses']==records


def test_reader_detail_review_receipt_is_saved_before_compose_failure(detailed_source):
    saved=[];item=_detail_relation()
    gateway=Gateway({'items':[item]}, {'items':[{'id':'V1','supported':False}]},
                    {'items':[{'id':'V1','supported':True}]},ModelError('unknown','transport_failed'))
    with pytest.raises(ModelError):
        engine.answer_chart(detailed_source,'第1页图1甲柱与参考线关系如何？',gateway,audit=saved.append)
    assert len(gateway.calls)==4 and len(saved[-1]['chart_responses'])==3
    assert payload(gateway.calls[2][0])['detail_view']['target_localization_verified'] is False
    assert saved[-1]['chart_responses'][1]['response']['value']['items'][0]['supported'] is False
    assert saved[-1]['chart_responses'][2]['response']['value']['items'][0]['supported'] is True


def test_final_detail_receipt_survives_accepted_coverage_unknown(detailed_source):
    saved=[];gateway=Gateway(*_final_denial_prefix(), {'claims':[{'id':'C1','supported':True}]},
                            ModelError('unknown','transport_failed'))
    with pytest.raises(ModelError):
        engine.answer_chart(detailed_source,'第1页图1甲柱与参考线关系如何？',gateway,audit=saved.append)
    assert len(gateway.calls)==6
    records=saved[-1]['chart_responses']; assert len(records)==5
    assert records[3]['response']['value']['claims'][0]['supported'] is False
    assert records[4]['response']['value']['claims'][0]['supported'] is True
    assert payload(gateway.calls[4][0])['detail_view']['target_localization_verified'] is False


def test_final_detail_and_accepted_coverage_all_retained_on_normal_completion(detailed_source):
    saved=[];gateway=Gateway(*_final_denial_prefix(),{'claims':[{'id':'C1','supported':True}]},_text_coverage('answered'))
    result=engine.answer_chart(detailed_source,'第1页图1甲柱与参考线关系如何？',gateway,audit=saved.append)
    assert result['status']=='completed' and len(result['diagnostics']['chart_responses'])==6
    assert saved[-1]['chart_responses']==result['diagnostics']['chart_responses']
    assert result['verification']['initial_chart_relations'][0]['supported'] is False
    assert result['verification']['chart_relations'][0]['supported'] is True


def test_gateway_attributes_and_raw_reply_identity_are_preserved():
    raw={'items':[]}
    class Original:
        config={'models':{'document':'explicit-fake'}}
        attempts=[];usage=[];models={}
        def __call__(self,messages,purpose):
            self.attempts.append({'purpose':purpose,'status':'completed'})
            return raw
    original=Original();wrapper=ChartResponses(original,None,lambda:None,lambda:{})
    assert wrapper.config is original.config and wrapper.attempts is original.attempts
    assert wrapper.usage is original.usage and wrapper.models is original.models
    assert wrapper([], 'document') is raw and len(wrapper.attempts)==1
    with pytest.raises(AttributeError):
        _=wrapper.nonexistent_attribute


def test_safe_snapshots_are_detached_bounded_and_private_fields_redacted():
    secret='fake credential with spaces';raw={'items':[{'text':'secret="'+secret+'" /Users/private/source.png'}],
        'authorization':secret,'analysis':'private reasoning','extra':['long'*2000]*20}
    before=copy.deepcopy(raw);saved=[]
    wrapper=ChartResponses(lambda m,p:raw,saved.append,lambda:None,lambda:{'detail':'Bearer fake-token','large':'x'*20000})
    assert wrapper([], 'document') is raw
    exported=json.dumps(saved,ensure_ascii=False)
    assert all(token not in exported for token in [secret,'/Users/private','private reasoning','fake-token'])
    assert len(json.dumps(saved[-1]['chart_responses'][0]['response'],ensure_ascii=False))<=MAX_RECEIPT_CHARACTERS
    assert len(json.dumps(saved[-1]['chart_response_progress'],ensure_ascii=False))<=MAX_RECEIPT_CHARACTERS
    saved[-1]['chart_responses'].clear()
    assert len(wrapper.snapshot()['chart_responses'])==1 and raw==before


def test_storage_cap_keeps_first_and_latest_with_explicit_omission_not_call_limit(monkeypatch):
    monkeypatch.setattr(chart_receipts,'MAX_RECEIPTS',5)
    counter={'n':0};saved=[]
    def gateway(m,p):
        counter['n']+=1;return {'items':[], 'counter':counter['n']}
    wrapper=ChartResponses(gateway,saved.append,lambda:None,lambda:{})
    for _ in range(8):wrapper([], 'document')
    last=wrapper.snapshot()
    assert [r['sequence'] for r in last['chart_responses']]==[1,5,6,7,8]
    assert last['chart_response_omissions']==3 and counter['n']==8
