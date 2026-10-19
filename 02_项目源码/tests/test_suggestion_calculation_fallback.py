"""Offline actual published-record projection; no API or answer execution."""
import copy,json
from pathlib import Path
import pytest
from backend.suggestion_service import candidate_suggestions

@pytest.fixture
def answer():
    return json.loads((Path(__file__).parent/'fixtures/published_calculation_followup.json').read_text())['answer']

def asks(answer,**kwargs):
    return [s for s in candidate_suggestions(answer,**kwargs) if s['action']=='ask']

def test_actual_unanchored_published_calculation_provides_grounded_question(answer):
    before=copy.deepcopy(answer)
    result=asks(answer)
    assert len(result)==1
    suggestion=result[0]
    assert suggestion['kind']=='explain_calculation'
    assert suggestion['payload']['calculation_id']=='K1'
    assert suggestion['payload']['claim_ids']==['C2']
    assert suggestion['payload']['evidence_ids']==['E1','E2']
    assert suggestion['payload']['context_revision']==answer['context_revision']
    assert suggestion['payload']['new_facts_asserted'] is False
    assert suggestion['automatic_submission'] is False
    assert not any(v in suggestion['text'] for v in ['2024','10%','10000','100.00','orders'])
    assert answer==before

def test_already_asked_calculation_can_offer_units_once(answer):
    answer['question']='说明这次已核对计算的参数来源与计算步骤？'
    result=asks(answer)
    assert len(result)==1 and result[0]['kind']=='explain_units'
    assert result[0]['payload']['calculation_id']=='K1'
    assert asks(answer,answered_questions=[result[0]['text']])==[]
    answer['question']='说明这次已核对计算的参数和单位换算过程？'
    assert asks(answer)==[]

@pytest.mark.parametrize('mutation',[
    lambda a:a.update(status='failed'),
    lambda a:a['result']['verification'].update(status='failed'),
    lambda a:a['result']['claims'][1].pop('calculation_ids'),
    lambda a:a['result']['calculations'][0].update(rule_origin='model_candidate'),
    lambda a:a['result']['claims'][1].update(evidence_ids=['missing']),
])
def test_fallback_never_promotes_failed_unpublished_unused_or_unbound_calculation(answer,mutation):
    mutation(answer)
    assert asks(answer)==[]

def test_generic_question_is_deduplicated_and_user_rule_keeps_binding(answer):
    answer['result']['calculations'][0]['rule_origin']='user_question'
    result=asks(answer)
    assert len(result)==1 and result[0]['payload']['calculation_id']=='K1'
    assert asks(answer,answered_questions=[result[0]['text']])==[]
