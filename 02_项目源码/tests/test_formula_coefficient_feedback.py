"""Source coefficient diagnostics; synthetic fixtures, no model requests."""
import copy
import pytest
from backend.formula_engine import calculate_formula, FormulaError
from backend.calculation_protocol import failure_feedback
from backend.cross_source_engine import PLANNER
from backend import user_scenarios


def proposal(coefficient):
    question='若 x=2 MPa，按原文公式估计强度。'
    context=user_scenarios.build_context(question)
    parameter=context['inputs'][0]
    user_binding={key:parameter[key] for key in ('quote','start','end','unit','symbol')}
    user_binding['user_input_id']=parameter['id']
    action={'expression':f'{coefficient}*x','bindings':{'x':user_binding},'output_unit':'MPa','rule_evidence_ids':['E1']}
    evidence={'E1':{'kind':'text','quote':f'For this estimate, q = {coefficient} x. The coefficient depends on material type.'}}
    return question,context,action,evidence


@pytest.mark.parametrize('coefficient',['24','12.7','3e1'])
def test_literal_rejection_identifies_source_binding_without_rewriting(coefficient):
    question,context,action,evidence=proposal(coefficient)
    before=copy.deepcopy(action)
    with pytest.raises(FormulaError,match='数值常量 .* 缺少来源绑定') as failure:
        calculate_formula(**action,evidence=evidence,user_formula=question,user_context=context)
    assert coefficient in str(failure.value)
    feedback=failure_feedback(action,str(failure.value),evidence)
    assert feedback['code']=='coefficient_binding_required'
    assert feedback['fields']==['expression','bindings','rule_evidence_ids']
    assert feedback['action_rewritten'] is False and feedback['diagnostic_only'] is True
    assert 'read_image' in feedback['repair_guidance'] and 'formula_read' in feedback['repair_guidance']
    assert action==before


@pytest.mark.parametrize('coefficient,expected',[('24','48.00'),('12.7','25.40'),('3e1','60.00')])
def test_explicit_source_coefficient_repairs_only_arithmetic_and_keeps_review_required(coefficient,expected):
    question,context,action,evidence=proposal(coefficient)
    action['expression']='k*x'
    action['bindings']['k']={'evidence_id':'E1','quote':coefficient,'unit':'1'}
    result=calculate_formula(**action,evidence=evidence,user_formula=question,user_context=context)
    assert result['display_value']==expected
    assert result['visual_rule_structure_checked'] is False
    assert result['unit_handling']['semantic_status']=='not_established_by_arithmetic'
    assert result['parameters'][1]['evidence_ids']==['E1']
    assert user_scenarios.required_calculations([{'calculation_ids':['K1']}],{'K1':result})==['K1']
    action['bindings']['k']['quote']='99'
    with pytest.raises(FormulaError,match='精确'):calculate_formula(**action,evidence=evidence,user_formula=question,user_context=context)


def test_protocol_distinguishes_viewing_image_from_verified_formula_and_forbids_literal_workaround():
    assert 'read_image只查看原页' in PLANNER
    assert '不得为绕过约束把系数拆成白名单数字' in PLANNER
