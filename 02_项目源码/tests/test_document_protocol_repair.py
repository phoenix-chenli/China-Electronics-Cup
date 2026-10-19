"""Explicit fake responses exercise received-response correction, never model quality."""
import copy
import json
import pytest
from backend import document_engine as e, completeness as c, document_protocol as p


def source(text='密封圈最低库存8件；低于8件申请采购，等于8件无需采购。'):
    return {'id':'s','name':'manual.txt','sha256':'a'*64,'parser_version':'test','status':'ready',
            'metadata':{},'pages':[{'number':1,'text':text}]}


def request(messages):
    return json.loads(messages[-1]['content'])


def claim(payload, text, quote=None, chunk_id=None):
    return {'text':text,'citations':[{'chunk_id':chunk_id or payload['chunks'][0]['chunk_id'],
                                    'quote':text if quote is None else quote}]}


def coverage(payload, status='answered', claim_ids=None, evidence_ids=None):
    return [{'question_id':part['id'],'status':status,
             'claim_ids':claim_ids if claim_ids is not None else [x['id'] for x in payload['claims']],
             'evidence_ids':evidence_ids or []} for part in payload['question_parts']]


def verified(payload, *, supported=True, complete=True, rows=None):
    return {'claims':[{'id':x['id'],'supported':supported} for x in payload['claims']],
            'answer_complete':complete,'coverage':rows if rows is not None else coverage(payload)}


def run(src, model, q='等于8件是否采购？', cancelled=lambda:False):
    return e.answer_document(src,q,model,cancelled,lambda _:None)


@pytest.mark.parametrize('raw,field,code',[
    (None,'coverage','missing_field'),('no','coverage','expected_array'),
    ([], 'coverage','question_count_mismatch'),
    ([{'question_id':'Q9','status':'answered','claim_ids':['c1']}],'coverage[0].question_id','unknown_question_id'),
    ([{'question_id':'Q1','status':'answered','claim_ids':['c9']}],'coverage[0].claim_ids','unknown_or_unsupported_id'),
    ([{'question_id':'Q1','status':'evidence_available','evidence_ids':['e1']}],'coverage[0].evidence_ids','unknown_or_unsupported_id'),
    ([{'question_id':'Q1','status':'answered','claim_ids':[]}],'coverage[0].claim_ids','answered_requires_supported_claim'),
])
def test_explicit_coverage_diagnostics_do_not_authorize_bad_input(raw,field,code):
    before=copy.deepcopy(raw);value=c.assess(c.question_parts('测试？'),raw,['c1'],['p1:0-20'])
    assert any(x['field']==field and x['code']==code for x in value['validation_errors'])
    assert not c.permits_completion(value) and raw==before


def test_invalid_coverage_can_be_corrected_against_only_accepted_claims():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append((purpose,payload))
        if purpose=='document':return {'status':'completed','claims':[claim(payload,'等于8件无需采购')]}
        if len(calls)==2:return verified(payload,rows=coverage(payload,claim_ids=['forged']))
        assert payload['allowed_claim_ids']==['c1'] and 'forged' not in payload['allowed_claim_ids']
        assert payload['protocol_feedback'][0]['code']=='unknown_or_unsupported_id'
        return {'answer_complete':True,'coverage':coverage(payload)}
    out=run(source(),model)
    assert out['status']=='completed' and [x[0] for x in calls]==['document','verify','verify']
    audit=out['protocol_audit'];assert audit['coverage_responses'][0]['response']['value']['coverage'][0]['claim_ids']==['forged']
    assert audit['initial_semantic_review']['value']['coverage'][0]['claim_ids']==['forged']


def test_missing_coverage_is_protocol_correction_not_automatic_completion():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':return {'status':'completed','claims':[claim(payload,'等于8件无需采购')]}
        if len(calls)==2:return {'claims':[{'id':'c1','supported':True}],'answer_complete':True}
        return {'answer_complete':False,'coverage':coverage(payload,'missing_evidence',[])}
    out=run(source(),model)
    # The missing_evidence path can supplement, but never infer completeness.
    assert out['status']=='insufficient_evidence' and out['claims']
    assert out['protocol_audit']['coverage_responses'][0]['validation_errors'][0]['code']=='missing_field' or out['completion_repair']['initial_protocol_audit']


@pytest.mark.parametrize('error_kind',['chunk_id','quote','number'])
def test_precise_received_candidate_repair_then_independent_review(error_kind):
    src=source('M-080尾部追加：型号RX-9B，柜C12，暗号“青石”。');before=copy.deepcopy(src);calls=[]
    good='型号RX-9B，柜C12，暗号“青石”'
    def model(messages,purpose):
        payload=request(messages);calls.append((purpose,payload))
        if purpose=='document':
            if len(calls)==1:
                item=claim(payload,good)
                if error_kind=='chunk_id':item['citations'][0]['chunk_id']='e1'
                elif error_kind=='quote':item['citations'][0]['quote']='型号RX9B，柜C12，暗号青石'
                else:item['text']='型号RX-8B，柜C12，暗号“青石”'
                return {'status':'completed','claims':[item]}
            assert payload['protocol_feedback'][0]['code'] in {'citation_chunk_id','citation_quote','claim_number'}
            assert payload['previous_candidate']['value']['claims']
            return {'status':'completed','claims':[claim(payload,good)]}
        return verified(payload)
    out=run(src,model,'型号、柜和暗号是什么？')
    assert out['status']=='completed' and calls[-1][0]=='verify' and src==before
    assert len(out['protocol_audit']['candidate_responses'])==2
    assert out['evidence'][0]['quote']==good


def test_same_bad_candidate_stops_without_verifier_or_publishing():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        assert purpose=='document'
        return {'status':'completed','claims':[claim(payload,'等于9件无需采购','等于8件无需采购')]}
    out=run(source(),model)
    assert calls==['document','document'] and not out['claims']
    assert out['protocol_audit']['candidate_responses'][-1]['stop_reason']=='duplicate_candidate'
    assert '不表示原件没有答案' in out['text']


def test_distinct_wrong_numbers_are_bounded_and_never_allowed():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        return {'status':'completed','claims':[claim(payload,f'等于{8+len(calls)}件无需采购','等于8件无需采购')]}
    out=run(source(),model)
    assert len(calls)==3 and not out['claims']
    assert out['protocol_audit']['candidate_responses'][-1]['stop_reason']=='no_progress'


def test_transport_after_completed_bad_response_is_not_replayed():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if len(calls)==2:raise TimeoutError('unknown POST')
        return {'status':'completed','claims':[claim(payload,'等于8件无需采购',chunk_id='bad')]}
    with pytest.raises(TimeoutError,match='unknown POST'):run(source(),model)
    assert calls==['document','document']


def test_cancellation_before_correction_does_not_start_request():
    calls=[];cancel=[False]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose);cancel[0]=True
        return {'status':'completed','claims':[claim(payload,'等于8件无需采购',chunk_id='bad')]}
    with pytest.raises(e.DocumentCancelled):run(source(),model,cancelled=lambda:cancel[0])
    assert calls==['document']


def test_semantic_rejected_sibling_does_not_suppress_focused_missing_part_repair():
    text='物料980元不含运费。运费120元另列。未记录税额。';calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append((purpose,payload))
        if purpose=='document':
            texts=['物料980元不含运费','税额已经记录'] if len(calls)==1 else ['未记录税额']
            return {'status':'completed','claims':[claim(payload,x,'未记录税额' if x=='税额已经记录' else x) for x in texts]}
        if len(calls)==2:return {'claims':[{'id':'c1','supported':True},{'id':'c2','supported':False}], 'answer_complete':False,'coverage':coverage(payload,'evidence_available',['c1'],[payload['available_evidence'][0]['id']])}
        assert [x['text'] for x in payload['claims']]==['物料980元不含运费','未记录税额']
        return verified(payload)
    out=run(source(text),model,'980元是否包括运费和税额？')
    assert out['status']=='completed' and len(calls)==4
    assert '税额已经记录' not in out['text'] and '未记录税额' in out['text']
    assert out['completion_repair']['initial_protocol_audit']['semantic_rejections']==['c2']


def test_supplement_rejection_preserves_previously_supported_fact_and_scope():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append((purpose,payload))
        if purpose=='document':return {'status':'completed','claims':[claim(payload,'等于8件无需采购')]}
        if len(calls)==2:return verified(payload,complete=False,rows=coverage(payload,'evidence_available',['c1'],[payload['available_evidence'][0]['id']]))
        return verified(payload,supported=False,complete=False,rows=coverage(payload,'missing_evidence',[]))
    out=run(source(),model,'等于8件是否采购？另问交付时间？')
    assert out['status']=='insufficient_evidence' and out['claims'][0]['text']=='等于8件无需采购'
    assert out['completion_repair']['reason']=='retained_claim_not_supported'
    assert out['completeness']['status']=='assessed'


def test_source_conflict_is_not_coverage_protocol_repaired():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':return {'status':'completed','claims':[claim(payload,'等于8件无需采购')]}
        return {**verified(payload),'coverage':[],'conflict':True}
    out=run(source(),model)
    assert calls==['document','verify'] and not out['claims'] and out['completeness']['status']=='conflict'


def test_bounded_audit_marks_truncation_and_keeps_hash_not_silent_raw_loss():
    raw={'text':'x'*30000};out=p.snapshot(raw)
    assert out['truncated'] and len(out['sha256'])==64 and len(out['prefix'])==24000
    assert 'value' not in out and raw['text']=='x'*30000


def test_mechanical_stage_improvements_allow_more_than_two_corrections():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':
            item=claim(payload,'等于8件无需采购')
            if len(calls)==1:item['citations'][0]['chunk_id']='bad'
            elif len(calls)==2:item['citations'][0]['quote']='等于8件不采购'
            elif len(calls)==3:item['text']='等于9件无需采购'
            return {'status':'completed','claims':[item]}
        return verified(payload)
    out=run(source(),model)
    assert out['status']=='completed' and calls==['document']*4+['verify']
    assert [a['errors'][0]['code'] for a in out['protocol_audit']['candidate_responses'][:3]]==['citation_chunk_id','citation_quote','claim_number']


def test_coverage_correction_transport_unknown_is_not_replayed():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':return {'status':'completed','claims':[claim(payload,'等于8件无需采购')]}
        if len(calls)==2:return verified(payload,rows=coverage(payload,claim_ids=['bad']))
        raise TimeoutError('unknown POST')
    with pytest.raises(TimeoutError):run(source(),model)
    assert calls==['document','verify','verify']


@pytest.mark.parametrize('status',[{},[],1,True])
def test_invalid_coverage_status_is_diagnostic_not_unhashable_crash(status):
    value=c.assess(c.question_parts('测试'),[{'question_id':'Q1','status':status,'claim_ids':['c1']}],['c1'],[])
    assert value['status']=='invalid' and value['validation_errors'][0]['code']=='invalid_status'


def test_coverage_corrector_never_receives_rejected_claim_as_answer():
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':return {'status':'completed','claims':[claim(payload,'等于8件无需采购'),claim(payload,'等于8件需要采购','等于8件无需采购')]}
        if len(calls)==2:return {'claims':[{'id':'c1','supported':True},{'id':'c2','supported':False}],'answer_complete':True,'coverage':coverage(payload,claim_ids=['c2'])}
        assert payload['allowed_claim_ids']==['c1']
        assert [x['text'] for x in payload['claims']]==['等于8件无需采购']
        assert 'retained_claims' not in payload and 'completion_repair' not in payload
        return {'answer_complete':True,'coverage':coverage(payload)}
    out=run(source(),model)
    assert [x['text'] for x in out['claims']]==['等于8件无需采购']
    # A denied sibling is not retroactively blessed by a coverage-only repair.
    assert out['status']=='insufficient_evidence'


def test_too_large_repair_feedback_stops_before_post_and_keeps_local_failure_audit(monkeypatch):
    calls=[]
    def model(messages,purpose):
        payload=request(messages);calls.append(purpose)
        if purpose=='document':
            monkeypatch.setattr(p,'MAX_REQUEST_CHARACTERS',1)
            return {'status':'completed','claims':[claim(payload,'等于8件无需采购',chunk_id='bad')]}
        raise AssertionError('no valid claim to review')
    out=run(source(),model)
    assert calls==['document'] and out['protocol_audit']['candidate_responses'][-1]['stop_reason']=='request_budget'
    assert not out['claims']


def test_coverage_error_budget_reports_actual_cut_not_merely_full_list():
    def assess_bad(n):
        return c.assess(c.question_parts('测试'),[{'question_id':'Q1','status':'answered','claim_ids':[f'wrong{i}' for i in range(n)]}],[],[])
    exact=assess_bad(32);cut=assess_bad(33)
    assert len(exact['validation_errors'])==32 and not exact['errors_truncated']
    assert len(cut['validation_errors'])==32 and cut['errors_truncated'] and cut['error_count']==33
