"""Offline regression: actual R9 failure stays rejected; authored repair is labeled."""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.cross_source_engine import _claims, _numbers, CrossSourceError, answer_cross_source
from backend.reasoning_tools import ReasoningLimits
from test_cross_business_repair import source, sql
from test_cross_query_fact_renderer import Gateway, review, fact


def saved():
    return json.loads((Path(__file__).parent / 'fixtures/r9_query_unbound_repeat.json').read_text())


def test_actual_r9_failure_is_not_silently_accepted_or_rewritten():
    record = saved(); before = copy.deepcopy(record)
    with pytest.raises(CrossSourceError) as caught:
        _claims({'claims': [record['candidate']]}, {e['id']: e for e in record['evidence']}, {},
                question=record['question'])
    details = caught.value.details
    assert details['unverified_tokens'] == ['30', '42']
    failure = details['claim_failures'][0]
    assert '青湾港42次巡检和东岸同名站30次不属于同一个站' in failure['unbound_query_text']
    # Source IDs may help repair, but do not authorize repeated unbound values.
    assert failure['matching_evidence_candidates']['42'] == ['E2']
    assert '仅供查找' in failure['repair_guidance']
    assert 'not_evidence' in failure['unbound_text_scope']
    assert len(failure['unbound_query_text']) <= 1800
    assert record == before


def authored_repair():
    record = saved(); claim = copy.deepcopy(record['candidate'])
    claim['text'] = claim['text'].replace(
        '青湾港42次巡检和东岸同名站30次不属于同一个站。',
        '青湾港和东岸同名站不属于同一个站。', 1)
    return record, claim


def test_authored_repair_keeps_relation_and_all_four_exact_facts():
    record, claim = authored_repair()
    checked = _claims({'claims': [claim]}, {e['id']: e for e in record['evidence']}, {},
                      question=record['question'])
    assert checked[0]['text'] == claim['text']
    assert '不属于同一个站' in checked[0]['text']
    assert len(checked[0]['query_bindings']) == 4
    assert all(b['semantic_review_required'] for b in checked[0]['query_bindings'])


@pytest.mark.parametrize('mutation', ['invented', 'repeated', 'wrong_sign', 'wrong_id', 'wrong_unit'])
def test_repair_does_not_authorize_unbound_or_wrong_facts(mutation):
    record, claim = authored_repair()
    if mutation == 'invented': claim['text'] += '额外收入为999元。'
    elif mutation == 'repeated': claim['text'] += '额外收入为42元。'
    elif mutation == 'wrong_sign': claim['text'] = claim['text'].replace('巡检42次', '巡检-42次')
    elif mutation == 'wrong_id': claim['query_bindings'][0]['value'] = 'A01'
    elif mutation == 'wrong_unit': claim['query_bindings'][2]['unit'] = '元'
    with pytest.raises(CrossSourceError):
        _claims({'claims': [claim]}, {e['id']: e for e in record['evidence']}, {},
                question=record['question'])


@pytest.mark.parametrize('text,expected', [
    ('它们是同一个站', set()), ('它们不是同一个对象', set()),
    ('同一个站有一个问题', {Decimal(1)}), ('同一个站有一百个问题', {Decimal(100)}),
    ('同一百个人', {Decimal(100)}), ('同一个站有1个问题', {Decimal(1)}),
    ('甲乙增幅相同，均为同一个百分点。', {Decimal(1)}),
    ('它们均发生在同一个月', {Decimal(1)}),
    ('它们使用同一个量纲', {Decimal(1)}),
    ('同一个站的预算为-1元', {Decimal(-1)}), ('同一个站的日期为2025年2月1日',
       {'date:2025-02-01', Decimal(2025)}),
])
def test_identity_word_is_not_a_global_one_whitelist(text, expected):
    assert _numbers(text) == expected


def test_unbound_feedback_redacts_private_text_without_changing_candidate():
    record = saved()
    record['candidate']['text'] += ' api_key="fake-secret-value" 路径 /Users/private_person/private/data.csv'
    before = copy.deepcopy(record)
    with pytest.raises(CrossSourceError) as caught:
        _claims({'claims': [record['candidate']]}, {e['id']: e for e in record['evidence']}, {},
                question=record['question'])
    failure = caught.value.details['claim_failures'][0]
    assert 'fake-secret-value' not in failure['unbound_query_text']
    assert '/Users/private_person' not in failure['unbound_query_text']
    assert failure['unbound_query_text_redacted'] is True
    assert failure['unbound_query_text_truncated'] is False
    assert failure['unverified_tokens'] == ['30', '42']
    assert record == before


def test_unbound_feedback_reports_diagnostic_truncation_only():
    record = saved()
    # A valid 1800-character candidate can expand under redaction. Only the
    # diagnostic copy is bounded; the candidate itself remains exact.
    record['candidate']['text'] += ' api_key=x '
    record['candidate']['text'] += '甲' * (1800 - len(record['candidate']['text']))
    before = copy.deepcopy(record)
    with pytest.raises(CrossSourceError) as caught:
        _claims({'claims': [record['candidate']]}, {e['id']: e for e in record['evidence']}, {},
                question=record['question'])
    failure = caught.value.details['claim_failures'][0]
    assert len(failure['unbound_query_text']) == 1800
    assert failure['unbound_query_text_truncated'] is True
    assert failure['unbound_query_text_redacted'] is True
    assert record == before


def proposal(payload, repeat=False):
    evidence = next(e for e in payload['evidence'] if e.get('query_result_context'))
    selection = fact(evidence)['query_fact']
    statement = '2025-07-01全站用电量为2610kWh'
    text = statement + '。这是同一个对象的结果。'
    if repeat: text += '另外用电量为2610kWh。'
    return {'status': 'completed', 'claims': [{'text': text,
        'query_facts': [{'selection': selection, 'statement': statement}], 'evidence_ids': ['E1']}]}


def test_feedback_reaches_next_planner_and_original_rejection_remains(source):
    def corrected(payload):
        errors = payload['tool_errors']
        failure = next(e for e in errors if e.get('claim_failures'))['claim_failures'][0]
        assert '另外用电量为2610kWh' in failure['unbound_query_text']
        assert '不要删除用户要求的关系或事实' in failure['repair_guidance']
        return proposal(payload)
    gateway = Gateway([sql(), lambda p: proposal(p, True), corrected, review])
    status, result = answer_cross_source([source], '说明全站用电量和对象范围', [], gateway)
    assert status == 'completed'
    assert len(gateway.calls) == 4
    assert '另外用电量' in result['diagnostics']['candidate_attempts'][0]['claims'][0]['text']
    assert '另外用电量' not in result['text']
    assert result['verification']['tool_actions'] == 1


@pytest.mark.parametrize('verdict', [False, None])
def test_identity_relation_still_requires_independent_review(source, verdict):
    gateway = Gateway([sql(), proposal, lambda p: review(p, query=verdict)])
    status, result = answer_cross_source([source], '说明全站用电量和对象范围', [], gateway,
                                      limits=ReasoningLimits(max_no_progress_rounds=1))
    assert status != 'completed' and result['claims'] == []


def test_repeated_unfixed_text_stops_without_publication(source):
    bad = lambda p: proposal(p, True)
    gateway = Gateway([sql(), bad, bad, bad])
    status, result = answer_cross_source([source], '说明全站用电量和对象范围', [], gateway)
    assert status != 'completed' and result['claims'] == []
    assert len(gateway.calls) == 4 and not any(role == 'verify' for role, _, _ in gateway.calls)
