"""Real rejected conversion action replay; feedback never authorizes or edits it."""
import copy,hashlib,json
from pathlib import Path
import pytest
from backend.calculation_protocol import INSTRUCTIONS,failure_feedback
from backend.formula_engine import calculate_formula,FormulaError


def fixture():return json.loads((Path(__file__).parent/'fixtures/unit_conversion_rejected_s3.json').read_text())
def execute(d):
    a=d['action'];return calculate_formula(a['expression'],a['bindings'],d['evidence'],a['output_unit'],precision=a['precision'],rule_evidence_ids=a['rule_evidence_ids'],user_formula=d['question'])

def test_exact_real_s3_missing_rule_still_rejected_and_feedback_is_specific_not_authorization():
    d=fixture();before=copy.deepcopy(d)
    assert hashlib.sha256(json.dumps(d['action'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()==d['raw_action_sha256']
    with pytest.raises(FormulaError,match='缺少计算规则原文') as error:execute(d)
    fb=failure_feedback(d['action'],str(error.value),d['evidence']);g=fb['repair_guidance']
    assert fb['code']=='rule_authorization_missing' and fb['action_rewritten'] is False and fb['diagnostic_only']
    assert '纯单位换算' in g and 'unit_evidence_id只证明参数单位' in g and '真实ID显式列入rule_evidence_ids' in g
    assert '普通换算不能套用费用计价operation' in g and '独立业务/语义复核' in g
    assert d==before
    with pytest.raises(FormulaError,match='缺少计算规则原文'):execute(d)


def test_planner_can_explicitly_cite_actual_unit_relation_but_feedback_does_not_do_it():
    d=fixture();d['action']['rule_evidence_ids']=['E1']
    out=execute(d);assert out['display_value']=='100.00' and out['unit']=='元' and out['is_hypothetical'] is False
    assert out['rule_evidence_ids']==['E1'] and out['unit_handling']['semantic_status']=='not_established_by_arithmetic'

@pytest.mark.parametrize('mutate',[
    lambda d:d['action'].update(rule_evidence_ids=['E-forged']),
    lambda d:d['action']['bindings']['a'].pop('unit_evidence_id'),
    lambda d:d['action']['bindings']['a'].update(unit_evidence_id='E2'),
])
def test_explicit_rule_does_not_bypass_unknown_reference_or_unit_checks(mutate):
    d=fixture();d['action']['rule_evidence_ids']=['E1'];mutate(d)
    with pytest.raises(FormulaError):execute(d)


def test_protocol_explains_rule_vs_unit_and_keeps_cost_operation_conditional():
    assert 'unit_evidence_id只证明该参数单位' in INSTRUCTIONS
    assert '仍须在rule_evidence_ids显式引用' in INSTRUCTIONS
    assert '不得将费用计价operation用于普通单位换算' in INSTRUCTIONS
    assert '只有用户明确要求费用计算' in INSTRUCTIONS
