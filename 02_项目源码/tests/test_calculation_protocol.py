"""Rejected tool actions remain diagnostic; all original execution gates apply."""
import copy
import hashlib
import json
import pytest

from backend.calculation_protocol import failure_feedback, rejection_receipt, MAX_ACTION_CHARACTERS
from backend import candidate_audit
from backend.cross_source_engine import answer_cross_source, PLANNER
from backend.reasoning_tools import ReasoningLimits
from test_cross_business_repair import source, sql, calculation, review, Gateway


def finish():
    return {'status':'completed','claims':[{'text':'全站不含税电费为1696.50元。','evidence_ids':['E1'],'calculation_ids':['K1']}]}


class CapturingGateway(Gateway):
    def __init__(self, values):super().__init__(values);self.systems=[]
    def __call__(self,messages,role):
        self.systems.append(messages[0]['content'])
        return super().__call__(messages,role)


def test_main_protocol_unifies_three_paths_and_distinguishes_binding_types(source):
    g=CapturingGateway([sql(),calculation(),finish(),review()])
    status,result=answer_cross_source([source],'计算2025年7月1日全站不含税电费。',[],g)
    assert status=='completed'
    system=g.systems[0]
    assert 'operation?:{kind:"quantity_times_unit_price"' in PLANNER
    assert '计算规则来源只能是：原文rule_evidence_ids、用户问题明确给出的字面表达式' in PLANNER
    assert '公式规则须有原文rule_evidence_ids；仅当' not in PLANNER
    assert 'unit_column?:"真实单位列"' in PLANNER
    assert 'kind=text' in system and 'calculate.bindings 与完成答案的 query_bindings 是不同协议' in system
    assert '"unit_column":"用量单位"' in system
    assert result['verification']['operation_relations'][0]['scope_supported'] is True


@pytest.mark.parametrize('failure_kind', ['value','unit_proof','operation'])
def test_failed_action_record_and_specific_feedback_do_not_rewrite_or_execute(source,failure_kind):
    bad=calculation()
    if failure_kind=='value':bad['actions'][0]['bindings']['q']['value']='2610'
    elif failure_kind=='unit_proof':bad['actions'][0]['bindings']['q']['unit_evidence_id']='E1'
    else:bad['actions'][0].pop('operation')
    before=copy.deepcopy(bad);events=[]
    g=CapturingGateway([sql(),bad,copy.deepcopy(bad),copy.deepcopy(bad)])
    status,result=answer_cross_source([source],'计算全站电费。',[],g,audit=events.append)
    assert status!='completed' and result['claims']==[] and result['calculations']==[]
    assert bad==before
    diag=result['diagnostics'];records=diag['rejected_calculation_actions']
    assert len(records)==3 and diag['calculation_action_rejection_count']==3
    assert diag['calculation_action_omissions']==0
    expected={'value':'model_value_forbidden','unit_proof':'text_unit_proof_required','operation':'rule_authorization_missing'}[failure_kind]
    errors=[e for e in diag['tool_errors'] if e['kind']=='calculate']
    assert all(e['code']==expected and e['action_rewritten'] is False for e in errors)
    assert all(e['rejected_action_sha256']==records[0]['raw_action_sha256'] for e in errors)
    retained=records[0]['received_action']['value']['action']
    if failure_kind=='value':
        assert retained['bindings'][0]['binding']['value']=='2610'
        assert errors[0]['fields']==['bindings.q.value']
    elif failure_kind=='unit_proof':
        assert retained['bindings'][0]['binding']['unit_evidence_id']=='E1'
        assert errors[0]['fields']==['bindings.q.unit_evidence_id']
    else:assert 'operation' not in retained
    rejections=[event for event in events[-1]['audit_history'] if event['kind']=='calculation_action_rejected']
    assert len(rejections)==3 and [event['record'] for event in rejections]==records
    # Only concise typed feedback enters the unchanged context budget; snapshots are audit only.
    for _,payload in g.calls[2:]:
        assert 'rejected_calculation_actions' not in payload and 'received_action' not in json.dumps(payload)
        assert payload['tool_errors'][-1]['code']==expected
    assert not any(s['kind']=='calculate' for s in result['steps'])


def test_corrected_new_action_can_compute_but_still_requires_four_business_checks(source):
    bad=calculation();bad['actions'][0]['bindings']['q']['value']='2610'
    g=Gateway([sql(),bad,calculation(),finish(),review(False),finish(),review(False)])
    status,result=answer_cross_source([source],'计算全站电费。',[],g,limits=ReasoningLimits(max_no_progress_rounds=2))
    assert status!='completed' and not result['claims'] and not result['calculations']
    assert result['diagnostics']['rejected_calculation_actions'][0]['received_action']['value']['action']['bindings'][0]['binding']['value']=='2610'
    assert any(e['kind']=='operation_review' for e in result['diagnostics']['tool_errors'])


def test_rejection_receipt_redacts_before_truncation_preserves_numeric_and_digest():
    action=calculation()['actions'][0]
    action['bindings']['q'].update(value=2610,quote='api_key="dummy secret with space" /Users/example/private/file endpoint="https://private.invalid/v1"')
    action['secret']='unknown field is omitted'
    action['analysis']='private thought is omitted'
    before=copy.deepcopy(action)
    record=rejection_receipt(action,'参数只能绑定证据位置，不能接收模型填写的数值')
    encoded=json.dumps(record,ensure_ascii=False)
    assert action==before
    assert 'dummy secret' not in encoded and '/Users/example' not in encoded and 'private.invalid' not in encoded
    assert 'unknown field is omitted' not in encoded and 'private thought' not in encoded
    assert record['received_action']['redacted'] is True
    assert record['raw_action_sha256']==hashlib.sha256(json.dumps(action,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    shown=record['received_action']['value']['action']
    assert shown['bindings'][0]['binding']['value']==2610 and shown['omitted_action_field_count']==2
    assert record['is_evidence'] is False and record['publication_status']=='not_adopted'


def test_malformed_and_oversized_diagnostics_are_bounded_with_omissions():
    action=calculation()['actions'][0]
    action['bindings']={f'p{i}':{'evidence_id':'E1','row':False,'column':'x'*2000,'value':{'nested':['z'*4000]*50},'unknown':'hidden'} for i in range(40)}
    action['operation']['pairs']=[{'quantity':'a'*2000,'unit_price':'b'*2000}]*30
    record=rejection_receipt(action,'无效')
    assert len(json.dumps(record,ensure_ascii=False))<=MAX_ACTION_CHARACTERS
    assert record['received_action']['truncated'] is True
    assert record['diagnostic_only'] and not record['is_evidence']
    recent=[]
    for _ in range(7):recent=candidate_audit.append_attempt(recent,record)
    assert len(recent)==3 and len(json.dumps(recent,ensure_ascii=False))<=12000


def test_false_boolean_row_is_preserved_not_coerced_by_diagnostic_projection():
    action=calculation()['actions'][0];action['bindings']['q']['row']=False
    receipt=rejection_receipt(action,'参数格式无效')
    assert receipt['received_action']['value']['action']['bindings'][0]['binding']['row'] is False


def test_unknown_post_after_local_failure_is_never_replayed_and_receipt_survives_callback(source):
    bad=calculation();bad['actions'][0]['bindings']['q']['value']='2610'
    class Failing(Gateway):
        def __call__(self,messages,role):
            if len(self.calls)==2:
                self.calls.append((role,{}));raise TimeoutError('unknown POST outcome')
            return super().__call__(messages,role)
    g=Failing([sql(),bad]);events=[]
    with pytest.raises(TimeoutError,match='unknown POST'):
        answer_cross_source([source],'计算全站电费。',[],g,audit=events.append)
    assert len(g.calls)==3 and len(events[-1]['rejected_calculation_actions'])==1


def test_cancel_after_received_failure_does_not_replan_and_keeps_diagnostic(source):
    bad=calculation();bad['actions'][0]['bindings']['q']['value']='2610'
    cancel=[False];events=[]
    def audit(event):
        events.append(event)
        if any(record['kind']=='calculation_action_rejected' for record in event['audit_history']):
            cancel[0]=True
    g=Gateway([sql(),bad])
    with pytest.raises(InterruptedError):
        answer_cross_source([source],'计算全站电费。',[],g,audit=audit,cancelled=lambda:cancel[0])
    assert len(g.calls)==2 and events[-1]['rejected_calculation_actions'][0]['diagnostic_only'] is True
    rejections=[event for event in events[-1]['audit_history'] if event['kind']=='calculation_action_rejected']
    assert len(rejections)==1
    assert rejections[0]['record']==events[-1]['rejected_calculation_actions'][0]
    assert rejections[0]['record']['received_action']['value']['action']['bindings'][0]['binding']['value']=='2610'


@pytest.mark.parametrize('field', ['intent_supported','parameter_roles_supported','scope_supported','pricing_supported'])
@pytest.mark.parametrize('invalid', [False, 1, None])
def test_each_operation_verdict_must_still_be_independent_boolean_true(field, invalid):
    from backend.business_operations import review_passed
    row={'id':'K1','intent_supported':True,'parameter_roles_supported':True,'scope_supported':True,'pricing_supported':True}
    if invalid is None:row.pop(field)
    else:row[field]=invalid
    assert not review_passed({'operation_relations':[row]},['K1'])


@pytest.mark.parametrize('padding', [10, 1])
def test_outer_budget_uses_only_privacy_projected_snapshot(padding):
    action=calculation()['actions'][0]
    action['bindings']['q']['value']={
        'authorization_header':'DUMMY_PRIVATE_HEADER_VALUE',
        'analysis':'DUMMY_HIDDEN_REASONING',
        'value':False, 'count':2610}
    for i in range(padding):
        action['bindings'][f'z{i}']={'evidence_id':'E1','quote':'a'*500}
    before=copy.deepcopy(action)
    receipt=rejection_receipt(action,'参数不能接收模型填写的数值')
    text=json.dumps(receipt,ensure_ascii=False)
    assert 'DUMMY_PRIVATE_HEADER_VALUE' not in text
    assert 'DUMMY_HIDDEN_REASONING' not in text
    assert receipt['received_action']['redacted'] is True
    assert len(text)<=MAX_ACTION_CHARACTERS
    assert action==before
    if padding==10:
        assert receipt['received_action']['truncated'] is True
        assert 'prefix' in receipt['received_action']
