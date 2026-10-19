"""Offline repair feedback; no automatic answer adoption or model requests."""
import copy
import json
from pathlib import Path

import pytest

from backend.cross_source_engine import (
    CALC_PUBLICATION_GUIDANCE, PLANNER, CrossSourceError,
    _calculation_publication_feedback, _claims, answer_cross_source,
)
from backend.reasoning_tools import ReasoningLimits
from test_cross_source_engine import (
    Responses, actions, approved, calculation, completed, search, sources, sql,
)


def test_received_failed_candidate_gets_only_its_executed_outputs():
    fixture = json.loads((Path(__file__).parent / 'fixtures' /
        'cross-calc-publication-rejected.json').read_text())
    before = copy.deepcopy(fixture)
    claim = fixture['candidate']['claims'][0]
    feedback = _calculation_publication_feedback(claim['calculation_ids'], fixture['calculations'])
    assert feedback['calculations'] == [{
        'id': 'K1', 'display_value': '90.00', 'unit': '元',
        'is_hypothetical': True, 'evidence_ids': ['E2', 'E1'],
    }]
    all_outputs = _calculation_publication_feedback(['K1', 'K2', 'K3'], fixture['calculations'])
    assert [c['is_hypothetical'] for c in all_outputs['calculations']] == [True, False, True]
    assert [c['display_value'] for c in all_outputs['calculations']] == ['90.00', '560.00', '470.00']
    assert feedback['semantic_status'] == 'not_established_by_arithmetic'
    assert 'not_new_evidence' in feedback['scope']
    assert fixture == before  # No edits to received text, inputs, or executed K.


def test_projection_is_bounded_and_never_presents_partial_numeric_value():
    calculations = {f'K{i}': {'display_value': str(i), 'unit': '元',
        'evidence_ids': [f'E{j}' for j in range(20)]} for i in range(1, 11)}
    calculations['K1']['display_value'] = '9' * 121
    feedback = _calculation_publication_feedback(['K999', 'K1', *calculations], calculations)
    assert len(feedback['calculations']) == 8 and feedback['omitted_calculations'] == 2
    first = feedback['calculations'][0]
    assert 'display_value' not in first and first['omitted_fields'] == ['display_value']
    assert len(first['evidence_ids']) == 16 and first['omitted_evidence_ids'] == 4
    assert 'is_hypothetical' not in first  # Missing provenance is not invented as actual.


def test_calculation_feedback_does_not_whitelist_extra_numbers_or_unknown_k():
    evidence = {'E1': {'kind': 'text', 'quote': '金额输入为12元。'}}
    calculations = {'K1': {'value': '6', 'display_value': '6.00', 'unit': '元', 'evidence_ids': ['E1']}}
    candidate = {'claims': [{'text': '金额为6元，中间值987元。', 'calculation_ids': ['K1']}]}
    before = copy.deepcopy(candidate)
    with pytest.raises(CrossSourceError, match='数值') as error:
        _claims(candidate, evidence, calculations)
    failure = error.value.details['claim_failures'][0]
    assert failure['unverified_tokens'] == ['987']
    assert failure['calculation_repair']['calculations'][0]['display_value'] == '6.00'
    assert candidate == before
    candidate['claims'][0]['calculation_ids'] = ['K999']
    with pytest.raises(CrossSourceError, match='不存在'):
        _claims(candidate, evidence, calculations)


@pytest.mark.parametrize('review_support', [True, False])
def test_repair_reuses_calculation_but_still_requires_independent_review(sources, review_support):
    review = approved(supported=review_support)
    review['claims'][0]['supported'] = review_support
    gateway = Responses(actions(sql(), search()), actions(calculation()),
        completed('优惠金额为6000.00元，中间值987元。'), completed(), review,
        *([] if review_support else [{}, {}, {}]))
    status, result = answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway,
        limits=ReasoningLimits(max_no_progress_rounds=2))
    repair_payload = gateway.calls[3]['payload']
    feedback = repair_payload['tool_errors'][-1]['claim_failures'][0]['calculation_repair']
    assert feedback['calculations'][0]['display_value'] == '6000.00'
    assert [c['purpose'] for c in gateway.calls[:5]] == ['table', 'table', 'table', 'table', 'verify']
    assert (status == 'completed') is review_support
    if review_support:
        assert len(gateway.calls) == 5
        assert result['claims'][0]['text'] == '优惠金额为6000.00元。'
        assert len(result['calculations']) == 1
    else:
        assert result['claims'] == []


def test_guidance_preserves_requested_derivation_and_scope():
    assert CALC_PUBLICATION_GUIDANCE in PLANNER
    assert '输入、公式、时间、NULL口径和来源仍必须完整回答' in CALC_PUBLICATION_GUIDANCE
    assert '不能为了通过检查删去必答内容' in CALC_PUBLICATION_GUIDANCE
    assert '不为重新表述而重复相同SQL或calculate' in CALC_PUBLICATION_GUIDANCE
