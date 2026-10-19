"""Scripted models and synthetic pixels test publication contracts, not accuracy."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from PIL import Image
import pytest
from fastapi.testclient import TestClient

from backend import chart_answer_engine as engine
from backend import completeness
from backend.main import create_app
from backend.model_gateway import ModelError


def payload(messages):
    content = messages[-1]['content']
    return json.loads(content[0]['text'] if isinstance(content, list) else content)


def pixels(messages):
    content = messages[-1]['content']
    return [p for p in content if p.get('type') == 'image_url'] if isinstance(content, list) else []


class Gateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls, self.attempts, self.usage, self.models = [], [], [], {}

    def __call__(self, messages, purpose):
        self.calls.append((copy.deepcopy(messages), purpose))
        self.models[purpose] = 'explicit-fake-' + purpose
        if not self.responses:
            raise AssertionError('Unexpected extra model call')
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value(messages, purpose) if callable(value) else copy.deepcopy(value)


@pytest.fixture
def source(tmp_path, monkeypatch):
    from backend import ocr_native
    monkeypatch.setattr(ocr_native, 'recognize', lambda *a, **k: pytest.fail('No native OCR in these tests'))
    path = tmp_path / 'synthetic-chart.png'
    Image.new('RGB', (200, 120), 'white').save(path)
    return {'id': 'source-1', 'name': '显式合成图表.png', 'format': 'png',
            'original_path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'parser_version': 'synthetic-chart-v1', 'metadata': {}, 'warnings': [], 'tables': [],
            'pages': [{'number': 1, 'text': '图1 预算收入', 'extraction': 'ocr',
                       'blocks': [{'text': '错误OCR 999', 'confidence': .2, 'start': 0, 'end': 9}]}]}


def reading(identifier='V1', value='73.3', year='2021'):
    return {'id': identifier, 'type': 'printed_value',
            'text': f'{year}年一般公共预算收入为{value}亿元。', 'bbox': [.1, .1, .9, .8],
            'value': value, 'unit': '亿元', 'category': f'{year}年', 'series': '一般公共预算收入'}


def candidate(text='2021年一般公共预算收入为73.3亿元。', refs=None):
    return {'id': 'C1', 'text': text, 'item_ids': refs or ['P1_V1']}


def review_for(messages, purpose, *, supported=True, coverage_status='answered'):
    assert purpose == 'verify'
    body = payload(messages)
    ids = [c['id'] for c in body['claims']]
    return {'claims': [{'id': i, 'supported': supported} for i in ids],
            'coverage': [{'question_id': p['id'], 'status': coverage_status,
                          'claim_ids': ids if supported and coverage_status == 'answered' else []}
                         for p in body['question_parts']]}


def responses(claim=None, *, review=review_for):
    return [{'items': [reading()]}, {'items': [{'id': 'V1', 'supported': True}]},
            {'claims': [claim or candidate()]}, review]


def test_answer_uses_two_distinct_visual_reviews_and_never_rewrites_ocr(source):
    before = copy.deepcopy(source)
    gateway = Gateway(*responses())
    result = engine.answer_chart(source, '第1页图1中2021年收入是多少？', gateway)
    assert result['status'] == 'completed' and len(result['claims']) == 1
    assert [p for _, p in gateway.calls] == ['document', 'verify', 'document', 'verify']
    assert pixels(gateway.calls[0][0]) == pixels(gateway.calls[1][0]) == pixels(gateway.calls[3][0])
    assert not pixels(gateway.calls[2][0])
    assert '错误OCR' not in str(gateway.calls) and '999' not in str(gateway.calls)
    assert source == before
    evidence = result['evidence'][0]
    assert evidence['extraction'] == 'independently_reviewed_visual_transcription'
    assert '非OCR逐字引文' in evidence['quote'] and 'start' not in evidence
    assert evidence['sha256'] == source['sha256'] and evidence['page'] == 1
    item = evidence['visual_reading']['items'][0]
    assert item['provenance'] == 'separate_visual_read_and_review' and 'confidence' not in item
    assert result['claims'][0]['chart_bindings'][0]['item_id'] == 'P1_V1'
    assert 'base64' not in json.dumps(result)
    assert result['diagnostics']['coverage_rechecks'] == []


@pytest.mark.parametrize('supported', [True, False])
def test_reader_region_drift_cannot_escape_as_evidence_or_binding_location(source, monkeypatch, supported):
    item = {'id': 'V1', 'type': 'visual_relation', 'text': '该柱低于0.20 mg/L参考线。',
            'bbox': [.65, .65, .85, .80], 'page': 1, 'provenance': 'separate_visual_read_and_review',
            'reference_labels': [{'text': '0.20参考线', 'value': '0.20', 'unit': 'mg/L',
                                  'bbox': [.65, .61, .85, .63]}]}
    reader_result = {'status': 'reviewed', 'version': 'test-reviewed-v1', 'items': [item],
                     'provenance': 'separate_visual_read_and_review',
                     'review': {'valid': True, 'supported_ids': ['V1'], 'rejected_ids': []},
                     'audit': {'original_page_image_sha256': 'a' * 64}}
    before = copy.deepcopy(reader_result)
    # Defense at the engine boundary is still required if an older reader or
    # injected adapter supplies unverified region suggestions in its items.
    monkeypatch.setattr(engine, 'read_chart_page', lambda *a, **k: reader_result)
    gateway = Gateway({'claims': [candidate(item['text'])]},
                      lambda m, p: review_for(m, p, supported=supported,
                          coverage_status='answered' if supported else 'missing_evidence'))
    result = engine.answer_chart(source, '第1页图1该柱是否低于参考线？', gateway)
    visual = result['evidence'][0]['visual_reading']
    assert visual['localization_scope'] == 'original_page'
    assert visual['precise_regions_verified'] is False
    for collection in (visual['items'], payload(gateway.calls[0][0])['reviewed_items'],
                       payload(gateway.calls[1][0])['reviewed_items']):
        for positioned in (collection[0], collection[0]['reference_labels'][0]):
            assert positioned['bbox'] == [0, 0, 1, 1]
            assert positioned['localization_scope'] == 'original_page'
    assert reader_result == before and len(gateway.calls) == 2
    assert pixels(gateway.calls[-1][0]) and gateway.calls[-1][1] == 'verify'
    assert result['verification']['chart_relations'] == [{'id': 'C1', 'supported': supported}]
    if supported:
        assert result['status'] == 'completed'
        binding = result['claims'][0]['chart_bindings'][0]
        assert binding['bbox'] == [0, 0, 1, 1] and binding['localization_scope'] == 'original_page'
        assert binding['page'] == 1 and binding['evidence_id'] == result['evidence'][0]['id']
    else:
        assert result['status'] == 'insufficient_evidence' and result['claims'] == []
        assert result['completeness']['complete'] is False


@pytest.mark.parametrize('text', ['2021年一般公共预算收入为73.8亿元。',
                                 '2021年一般公共预算收入为-73.3亿元。',
                                 '2021年一般公共预算收入为733亿元。',
                                 '2021年一般公共预算收入为73.3%亿元。'])
def test_new_value_sign_decimal_or_percent_never_reaches_final_verifier(source, text):
    bad = {'claims': [candidate(text)]}
    gateway = Gateway(*responses()[:2], bad, bad)
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'failed' and not result['claims']
    assert result['diagnostics']['protocol_failed'] is True
    assert [p for _, p in gateway.calls] == ['document', 'verify', 'document', 'document']
    assert 'protocol_correction' in payload(gateway.calls[-1][0])


@pytest.mark.parametrize('text', ['2021年一般公共预算收入为73.8亿元。',
                                 '2021年一般公共预算收入为73.3万元。',
                                 '2021年一般公共预算收入高于2022年。'])
def test_numeric_membership_does_not_override_negative_semantic_review(source, text):
    # Both numeric tokens exist. Wrong year/value, unit, or relation must still
    # be independently judged from pixels, and a negative verdict is final.
    gateway = Gateway({'items': [reading(), reading('V2', '73.8', '2022')]},
        {'items': [{'id': 'V1', 'supported': True}, {'id': 'V2', 'supported': True}]},
        {'claims': [candidate(text, ['P1_V1', 'P1_V2'])]},
        lambda m, p: review_for(m, p, supported=False, coverage_status='missing_evidence'))
    result = engine.answer_chart(source, '第1页图1的收入如何？', gateway)
    assert result['status'] == 'insufficient_evidence' and not result['claims']
    assert len(gateway.calls) == 4 and pixels(gateway.calls[-1][0])


def test_semantic_rejection_of_transcription_prevents_answer_generation(source):
    gateway = Gateway({'items': [reading()]}, {'items': [{'id': 'V1', 'supported': False}]})
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert not result['claims'] and not result['evidence']
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 2
    assert result['verification']['visual_review'] is False


@pytest.mark.parametrize('bad', [None, [], [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C99']}],
                               [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': []}]])
def test_unknown_or_invalid_coverage_cannot_complete_or_publish_candidate(source, bad):
    review = {'claims': [{'id': 'C1', 'supported': True}], 'coverage': bad}
    gateway = Gateway(*responses()[:3], review, review)
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'failed' and not result['claims']
    assert result['diagnostics']['protocol_failed'] is True
    assert len(gateway.calls) == 5 and len(result['evidence']) == 1


@pytest.mark.parametrize('bad', [{'claims': []}, {'claims': [{'id': 'C1', 'supported': 'true'}]},
                               {'claims': [{'id': 'C2', 'supported': True}]}])
def test_final_review_must_cover_exact_boolean_claim_ids(source, bad):
    gateway = Gateway(*responses()[:3], bad, bad)
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert not result['claims'] and result['status'] == 'failed'
    assert len(gateway.calls) == 5


@pytest.mark.parametrize('asks_exact', [False, True])
def test_supported_precision_limit_is_completed_only_when_that_answers_the_question(source, asks_exact):
    text = '图中未印刷该断面的精确浓度，不能仅凭柱高给出三位小数测量值。'
    item = {'id': 'V1', 'type': 'missing_precision', 'text': text, 'bbox': [.1, .1, .9, .9]}
    gateway = Gateway({'items': [item]}, {'items': [{'id': 'V1', 'supported': True}]},
        {'claims': [candidate(text)]},
        lambda m, p: review_for(m, p, coverage_status='missing_evidence' if asks_exact else 'answered'))
    question = '第1页图1的精确浓度是多少？必须保留三位小数。' if asks_exact else '第1页图1能否给出三位小数测量值？'
    result = engine.answer_chart(source, question, gateway)
    assert result['status'] == ('insufficient_evidence' if asks_exact else 'completed')
    assert result['claims'][0]['text'] == text and len(gateway.calls) == 4


def _precision_refusal_responses():
    limit = '甲断面柱上没有印刷精确数值，不能给出精确到三位小数的实测浓度。'
    relation = '甲断面柱顶高于参考线0.10 mg/L，但无法给出更精确的单一数值。'
    missing = {'id': 'V1', 'type': 'missing_precision', 'text': limit, 'bbox': [.1, .1, .9, .9]}
    standard = {'id': 'V2', 'type': 'printed_value', 'text': '标准参考线标注0.10 mg/L。',
                'value': '0.10', 'unit': 'mg/L', 'series': '标准参考线', 'bbox': [.1, .1, .9, .9]}
    above = {'id': 'V3', 'type': 'visual_relation', 'text': '甲断面柱顶高于参考线0.10 mg/L。', 'bbox': [.1, .1, .9, .9],
             'reference_labels': [{'text': '标准参考线0.10 mg/L', 'value': '0.10', 'unit': 'mg/L', 'bbox': [.1, .1, .9, .9]}]}
    return [{'items': [missing, standard, above]},
            {'items': [{'id': v, 'supported': True} for v in ('V1', 'V2', 'V3')]},
            {'claims': [candidate(limit), {'id': 'C2', 'text': relation, 'item_ids': ['P1_V1', 'P1_V2', 'P1_V3']}]},
            review_for]


@pytest.mark.parametrize('verb', ['给出', '提供', '计算', '读出', '列出', '写出'])
def test_explicit_precision_request_cannot_complete_on_supported_refusal_or_reference_value(source, verb):
    gateway = Gateway(*_precision_refusal_responses())
    result = engine.answer_chart(source, f'第1页图1请{verb}甲断面实测浓度，精确到小数点后三位，不要用区间代替。', gateway)
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 4
    assert len(result['claims']) == 2 and len(result['evidence']) == 1
    assert result['completeness']['items'][0]['status'] == 'missing_evidence'
    guard = result['diagnostics']['precision_guards'][0]
    assert guard['original_coverage']['complete'] is True
    assert guard['original_coverage']['items'][0]['status'] == 'answered'
    assert guard['coverage']['complete'] is False
    assert guard['adjustments'][0]['limiting_claim_ids'] == ['C1', 'C2']
    assert guard['adjustments'][0]['missing_precision_item_ids'] == ['P1_V1']
    assert result['diagnostics']['coverage_rechecks'] == []
    assert result['diagnostics']['protocol_failed'] is False


@pytest.mark.parametrize('ability', ['能否', '可否', '能不能', '可不可以', '是否能', '是否可以'])
def test_precision_capability_question_can_complete_with_supported_cannot_answer(source, ability):
    gateway = Gateway(*_precision_refusal_responses())
    result = engine.answer_chart(source, f'第1页图1{ability}给出甲断面精确到三位小数的实测浓度？', gateway)
    assert result['status'] == 'completed' and len(gateway.calls) == 4
    assert result['diagnostics']['precision_guards'] == []


@pytest.mark.parametrize('question,explicit', [
    ('请给出甲断面三位小数实测浓度，并说明乙断面能否精确读取。', True),
    ('请给出甲断面三位小数实测浓度并说明乙断面能否精确读取。', True),
    ('请说明乙断面能否精确读取，并给出甲断面三位小数实测浓度。', True),
    ('请给出甲断面实测浓度，要求精确到小数点后三位。', True),
    ('请给出甲断面实测浓度，精确到小数点后三位。', True),
    ('图中可以给出甲断面三位小数精确浓度吗？', False),
    ('图中能给出甲断面三位小数精确浓度吗？', False),
    ('请给出甲收入，并说明乙能否精确。', False),
    ('请给出甲收入并说明乙能否精确。', False),
    ('能否给出甲断面实测浓度，要求精确到小数点后三位？', False),
])
def test_precision_request_scope_keeps_ability_and_new_object_in_their_own_clauses(question, explicit):
    assert engine._explicit_precision_request(question) is explicit


@pytest.mark.parametrize('question,expected', [
    ('第1页图1请给出甲断面三位小数实测浓度，并说明乙断面能否精确读取。', 'insufficient_evidence'),
    ('第1页图1可以给出甲断面三位小数精确浓度吗？', 'completed'),
])
def test_precision_clause_scope_controls_final_status_without_extra_call(source, question, expected):
    gateway = Gateway(*_precision_refusal_responses())
    result = engine.answer_chart(source, question, gateway)
    assert result['status'] == expected and len(gateway.calls) == 4
    assert len(result['claims']) == 2
    assert bool(result['diagnostics']['precision_guards']) is (expected == 'insufficient_evidence')


def test_printed_exact_point_value_still_answers_explicit_precision_request(source):
    text = '甲断面实测浓度为0.123 mg/L。'
    item = {'id': 'V1', 'type': 'printed_value', 'text': text, 'value': '0.123', 'unit': 'mg/L',
            'category': '甲断面', 'series': '实测浓度', 'bbox': [.1, .1, .9, .9]}
    gateway = Gateway({'items': [item]}, {'items': [{'id': 'V1', 'supported': True}]},
                      {'claims': [candidate(text)]}, review_for)
    result = engine.answer_chart(source, '第1页图1请给出甲断面精确到三位小数的实测浓度。', gateway)
    assert result['status'] == 'completed' and result['claims'][0]['text'] == text
    assert len(gateway.calls) == 4 and result['diagnostics']['precision_guards'] == []


def test_precision_guard_only_downgrades_part_citing_actual_supported_limitation(source):
    limit = '甲断面没有印刷精确数值，不能给出三位小数浓度。'
    item = {'id': 'V2', 'type': 'missing_precision', 'text': limit, 'bbox': [.1, .1, .9, .9]}
    review = {'claims': [{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': True}], 'coverage': [
        {'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']},
        {'question_id': 'Q2', 'status': 'answered', 'claim_ids': ['C2']}]}
    gateway = Gateway({'items': [reading(), item]}, {'items': [{'id': 'V1', 'supported': True}, {'id': 'V2', 'supported': True}]},
        {'claims': [candidate(), {'id': 'C2', 'text': limit, 'item_ids': ['P1_V2']}]}, review)
    result = engine.answer_chart(source, '第1页图1请给出2021年收入的精确值？请给出甲断面三位小数浓度。', gateway)
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 4
    assert [r['status'] for r in result['completeness']['items']] == ['answered', 'missing_evidence']
    assert len(result['claims']) == 2
    guard = result['diagnostics']['precision_guards'][0]
    assert [r['question_id'] for r in guard['adjustments']] == ['Q2']
    assert all(r['status'] == 'answered' for r in guard['original_coverage']['items'])


def test_precision_guard_cannot_use_unreferenced_missing_precision_or_change_input():
    parts = completeness.question_parts('请给出甲断面三位小数精确浓度。')
    coverage = completeness.assess(parts, [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']}], ['C1'], [])
    before = copy.deepcopy(coverage)
    claims = [candidate('不能给出三位小数精确浓度。')]
    items = {'P1_V1': reading(), 'P1_V2': {'type': 'missing_precision', 'text': '不能给出三位小数精确浓度。'}}
    # This invalid candidate would already be rejected by _claims. The local
    # coverage guard must not silently repair its missing evidence binding.
    adjusted, changes = engine._precision_coverage(coverage, claims, items)
    assert adjusted == before and coverage == before and changes == []


@pytest.mark.parametrize('coverage_status', ['missing_evidence', 'conflict'])
def test_precision_guard_never_upgrades_existing_incomplete_or_conflicting_coverage(source, coverage_status):
    scripted = _precision_refusal_responses()
    scripted[-1] = lambda m, p: review_for(m, p, coverage_status=coverage_status)
    gateway = Gateway(*scripted)
    result = engine.answer_chart(source, '第1页图1请给出甲断面三位小数精确浓度。', gateway)
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 4
    assert result['completeness']['items'][0]['status'] == coverage_status
    assert result['completeness']['complete'] is False
    assert result['diagnostics']['precision_guards'] == []


@pytest.mark.parametrize('failure', [ModelError('timeout', 'timeout'),
                                    ModelError('unknown POST', 'transport_failed'), InterruptedError('cancelled')])
@pytest.mark.parametrize('stage', ['compose', 'review'])
def test_transport_or_cancel_is_never_reissued_by_answer_recovery(source, failure, stage):
    prefix = responses()[:2 if stage == 'compose' else 3]
    gateway = Gateway(*prefix, failure)
    with pytest.raises(type(failure)):
        engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert len(gateway.calls) == len(prefix) + 1


def test_answer_protocol_correction_is_only_once_then_stops(source):
    gateway = Gateway(*responses()[:2], {}, {})
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert not result['claims'] and len(gateway.calls) == 4
    assert result['status'] == 'failed' and '格式' in result['text']
    assert '不能完整满足问题' not in result['text']
    assert len(result['diagnostics']['answer_attempts']) == 2


@pytest.mark.parametrize('bad', [{}, {'items': 'not-an-array'}, {'items': [{'id': 'V1', 'type': 'printed_value'}]}])
def test_reader_protocol_failure_is_execution_failure_not_missing_source_evidence(source, bad):
    gateway = Gateway(bad, bad)
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'failed' and result['diagnostics']['protocol_failed'] is True
    assert '格式' in result['text'] and '不能完整满足问题' not in result['text']
    assert not result['claims'] and not result['evidence']
    assert result['verification']['visual_review'] is False
    assert [p for _, p in gateway.calls] == ['document', 'document']


def test_well_formed_empty_reading_is_insufficiency_not_protocol_failure(source):
    gateway = Gateway({'items': []})
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'insufficient_evidence'
    assert result['diagnostics']['protocol_failed'] is False
    assert not result['claims'] and len(gateway.calls) == 1


def test_new_page_reader_protocol_failure_preserves_old_reviewed_partial(source, monkeypatch):
    source['pages'].append({'number': 2, 'text': '图1 后续信息'})
    reads = []
    def read(s, n, *args):
        reads.append(n)
        if n == 2:
            return {'status': 'protocol_failed', 'items': []}
        return {'status': 'reviewed', 'version': 'test-reviewed-v1',
                'items': [{**reading(), 'page': 1, 'provenance': 'separate_visual_read_and_review'}],
                'provenance': 'separate_visual_read_and_review',
                'review': {'valid': True, 'supported_ids': ['V1'], 'rejected_ids': []},
                'audit': {'original_page_image_sha256': 'a' * 64}}
    monkeypatch.setattr(engine, 'read_chart_page', read)
    partial = {'claims': [{'id': 'C1', 'supported': True}], 'coverage': [
        {'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']},
        {'question_id': 'Q2', 'status': 'missing_evidence', 'claim_ids': []}]}
    gateway = Gateway({'claims': [candidate()]}, partial)
    result = engine.answer_chart(source, '图1收入是多少？利润是多少？', gateway)
    assert result['status'] == 'insufficient_evidence' and result['diagnostics']['protocol_failed'] is True
    assert result['claims'][0]['text'] == candidate()['text'] and '格式' in result['text']
    assert result['claims'][0]['evidence_ids'] == ['E1'] and len(result['evidence']) == 1
    assert reads == [1, 2] and len(gateway.calls) == 2


def test_initial_answer_budget_failure_does_not_invent_claim_or_visual_verdict(source, monkeypatch):
    monkeypatch.setattr(engine, 'MAX_CONTEXT', 1)
    gateway = Gateway(*responses()[:2])
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'insufficient_evidence' and not result['claims']
    assert len(result['evidence']) == 1 and len(gateway.calls) == 2
    assert result['verification']['visual_review'] is False
    assert any('超过' in a.get('local_error', '') for a in result['diagnostics']['answer_attempts'])


@pytest.mark.parametrize('failure_stage', ['read', 'compose', 'review'])
def test_local_recovery_budget_failure_retains_only_previous_verified_facts(source, monkeypatch, failure_stage):
    # Read stage is a fake boundary; both answer phases and the first actual
    # original-page render still run. A local budget failure cannot erase a
    # previously independently reviewed fact or publish the new candidate.
    source['pages'].append({'number': 2, 'text': '图1 后续信息'})
    question = '图1收入是多少？利润是多少？'
    render = engine.page_content
    monkeypatch.setattr(engine, 'page_content', lambda s, n: render(s, 1))
    reads = []
    def read(s, n, *args):
        reads.append(n)
        if n == 2 and failure_stage == 'read':
            raise ValueError('本页图表读取或复核上下文超过预算')
        item = {**reading(), 'page': n, 'provenance': 'separate_visual_read_and_review'}
        return {'status': 'reviewed', 'version': 'test-reviewed-v1', 'items': [item],
                'provenance': 'separate_visual_read_and_review',
                'review': {'valid': True, 'supported_ids': ['V1'], 'rejected_ids': []},
                'audit': {'original_page_image_sha256': 'a' * 64}}
    monkeypatch.setattr(engine, 'read_chart_page', read)
    original_ask = engine._ask
    def ask(gateway, purpose, system, body, *args, **kwargs):
        if len(body['reviewed_items']) > 1 and (
                failure_stage == 'compose' and purpose == 'document'
                or failure_stage == 'review' and purpose == 'verify'):
            raise ValueError('图表问答上下文超过结构预算，请缩小问题范围')
        return original_ask(gateway, purpose, system, body, *args, **kwargs)
    monkeypatch.setattr(engine, '_ask', ask)
    partial = {'claims': [{'id': 'C1', 'supported': True}], 'coverage': [
        {'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']},
        {'question_id': 'Q2', 'status': 'missing_evidence', 'claim_ids': []}]}
    first = candidate()
    later = candidate(refs=['P2_V1'])
    gateway = Gateway({'claims': [first]}, partial, *([{'claims': [later]}] if failure_stage == 'review' else []))
    result = engine.answer_chart(source, question, gateway)
    assert result['status'] == 'insufficient_evidence'
    assert result['claims'][0]['text'] == first['text']
    assert result['claims'][0]['evidence_ids'] == ['E1']
    assert result['claims'][0]['chart_bindings'][0]['item_id'] == 'P1_V1'
    assert result['completeness']['items'][0]['status'] == 'answered'
    assert result['completeness']['items'][1]['status'] == 'missing_evidence'
    assert reads == [1, 2] and len(gateway.calls) == (3 if failure_stage == 'review' else 2)
    assert result['verification']['visual_review'] is True


def test_page_selection_prefers_actual_requested_figure_over_same_page_numbers(source):
    source['pages'] = [{'number': 1, 'text': '第9页目录 图4 可支配收入'},
                       {'number': 5, 'text': '图2 一般公共预算收入情况'},
                       {'number': 9, 'text': '图4 可支配收入 图5 居民人均消费支出构成'}]
    assert engine.select_pages(source, '图5居住占比是多少？') == [9]
    assert engine.select_pages(source, '第5页图2收入是多少？') == [5]
    assert engine.select_pages(source, '第99页图2收入是多少？') == []
    assert not engine.can_answer([source, copy.deepcopy(source)], '图5居住占比是多少？')
    source['original_path'] += '.missing'
    assert not engine.can_answer([source], '图5居住占比是多少？')


@pytest.mark.parametrize('question', ['第1页表1中收入占总收入的比例是多少？',
                                    '第1页居民消费占比是多少？', '第1页收入增速是多少？',
                                    '第1页表1计算甲收入占总收入的比例。',
                                    '第1页图1计算甲收入占总收入的比例。',
                                    '第1页图1请给出两年收入相差多少。',
                                    '第1页图1求差值。', '第1页图1请换算为万元。'])
def test_plain_statistic_words_and_derived_calculations_keep_existing_route(source, question):
    source['pages'][0]['text'] = '表1 收入统计 甲收入25万元 总收入100万元'
    assert engine.can_answer([source], question) is False


@pytest.mark.parametrize('question', ['第1页图1居住占比是多少？', '第1页图1收入增速是多少？',
                                    '第1页图中的收入印刷值是多少？', '第1页柱状图甲柱比乙柱高还是低？',
                                    '第1页图1不要计算，只读取印刷值。'])
def test_printed_chart_values_and_ordinary_comparisons_remain_chart_questions(source, question):
    assert engine.can_answer([source], question) is True


def test_matching_figure_in_independent_ocr_observations_can_select_page(source):
    source['pages'] = [{'number': 1, 'text': '无关正文'},
                       {'number': 2, 'text': '正文未提取到图名', 'chart_observations': [{'text': '图2-8 珠江干流'}]}]
    assert engine.select_pages(source, '图2－8的TP关系如何？') == [2]


@pytest.mark.parametrize('statement', ['第9页图5中，2024年居住占比为38.4%。',
                                      '第9页的图5显示，2024年居住占比为38.4%。'])
def test_bound_page_and_real_figure_annotations_do_not_become_business_values(source, statement):
    source['pages'] = [{'number': 9, 'text': '图5 2024年居民人均消费支出构成'}]
    item = {'id': 'P9_V1', 'page': 9, 'text': '2024年居住占比为38.4%。',
            'value': '38.4%', 'period': '2024年', 'category': '居住'}
    claims, errors = engine._claims({'claims': [candidate(statement, ['P9_V1'])]}, {'P9_V1': item}, source)
    assert not errors and claims[0]['text'] == statement


@pytest.mark.parametrize('statement', ['第8页图5中，2024年居住占比为38.4%。',
                                      '第2024页图5中，2024年居住占比为38.4%。',
                                      '第9页图6中，2024年居住占比为38.4%。',
                                      '第9页图5中，2024年居住占比为9%。',
                                      '第9页图5中，2024年居住支出为9亿元。'])
def test_unknown_location_or_page_numeral_reused_as_business_value_is_rejected(source, statement):
    source['pages'] = [{'number': 9, 'text': '图5 2024年居民人均消费支出构成'}]
    item = {'id': 'P9_V1', 'page': 9, 'text': '2024年居住占比为38.4%。',
            'value': '38.4%', 'period': '2024年', 'category': '居住'}
    claims, errors = engine._claims({'claims': [candidate(statement, ['P9_V1'])]}, {'P9_V1': item}, source)
    assert errors and not claims


def test_figure_numeral_in_reviewed_text_does_not_authorize_business_amount(source):
    source['pages'] = [{'number': 9, 'text': '图5 2024年居民人均消费支出构成'}]
    item = {'id': 'P9_V1', 'page': 9, 'text': '图5中，2024年居住占比为38.4%。',
            'value': '38.4%', 'period': '2024年', 'category': '居住',
            'context_annotations': [{'kind': 'figure_label', 'text': '图5', 'start': 0, 'end': 2}]}
    claims, errors = engine._claims({'claims': [candidate('2024年居住支出为5亿元。', ['P9_V1'])]},
                                   {'P9_V1': item}, source)
    assert errors and not claims


@pytest.mark.parametrize('incorrect_point_claim', [False, True])
def test_reference_line_values_are_available_but_cannot_override_negative_point_semantics(source, incorrect_point_claim):
    item = {'id': 'V1', 'type': 'visual_relation', 'text': '长虹桥的TP高于II类线且低于III类线。',
            'bbox': [.1, .1, .9, .9], 'category': '长虹桥', 'series': 'TP',
            'reference_labels': [
                {'text': '国家II类标准0.10', 'value': '0.10', 'unit': 'mg/L', 'bbox': [.7, .4, .9, .5]},
                {'text': '国家III类标准0.20', 'value': '0.20', 'unit': 'mg/L', 'bbox': [.7, .2, .9, .3]}]}
    statement = ('长虹桥TP实测浓度为0.10 mg/L。' if incorrect_point_claim else
                 '长虹桥TP高于国家II类0.10 mg/L线且低于国家III类0.20 mg/L线。')
    gateway = Gateway({'items': [item]}, {'items': [{'id': 'V1', 'supported': True}]},
        {'claims': [candidate(statement)]},
        lambda m, p: review_for(m, p, supported=not incorrect_point_claim,
                                coverage_status='missing_evidence' if incorrect_point_claim else 'answered'))
    result = engine.answer_chart(source, '第1页图1长虹桥TP与参考线是什么关系？', gateway)
    assert len(gateway.calls) == 4 and pixels(gateway.calls[-1][0])
    if incorrect_point_claim:
        assert result['status'] == 'insufficient_evidence' and not result['claims']
    else:
        assert result['status'] == 'completed' and result['claims'][0]['text'] == statement


@pytest.mark.parametrize('text', ['该图没有为长虹桥柱印刷精确数值标签。',
                                 '不能依据该图把长虹桥总磷浓度写到三位小数。',
                                 '无法仅凭柱高给出长虹桥的精确浓度。'])
def test_relation_item_alone_cannot_support_absence_of_precise_labels(source, text):
    relation = {'id': 'P1_V1', 'page': 1, 'type': 'visual_relation',
                'text': '长虹桥柱高于II类线且低于III类线。'}
    missing = {'id': 'P1_V2', 'page': 1, 'type': 'missing_precision',
               'text': '图中没有长虹桥柱的精确数值标签，无法读取三位小数浓度。'}
    # A globally available but unreferenced precision observation must not fill
    # a hole in this claim's own references.
    items = {'P1_V1': relation, 'P1_V2': missing}
    claims, errors = engine._claims({'claims': [candidate(text)]}, items, source)
    assert errors and not claims
    claims, errors = engine._claims({'claims': [candidate(text, ['P1_V1', 'P1_V2'])]}, items, source)
    assert not errors and claims[0]['text'] == text


def test_ordinary_negation_is_not_mistaken_for_missing_precision(source):
    item = {'id': 'P1_V1', 'page': 1, 'type': 'visual_relation', 'text': '收入柱高于上一年。'}
    claims, errors = engine._claims({'claims': [candidate('不能说收入下降，收入比上一年增加。')]},
                                   {'P1_V1': item}, source)
    assert not errors and claims


@pytest.mark.parametrize('supported', [True, False])
def test_last_compose_attempt_sends_only_valid_unique_subset_to_independent_review(source, supported):
    good = {**candidate(), 'id': 'C2'}
    bad = candidate('2025年一般公共预算收入为999亿元。')
    body = {'claims': [bad, good]}
    def verify(messages, purpose):
        assert payload(messages)['claims'] == [good]
        assert pixels(messages)
        return review_for(messages, purpose, supported=supported,
                          coverage_status='answered' if supported else 'missing_evidence')
    gateway = Gateway(*responses()[:2], body, body, verify)
    result = engine.answer_chart(source, '第1页图1中2021年收入是多少？', gateway)
    assert len(gateway.calls) == 5
    assert result['diagnostics']['answer_attempts'][1]['partial_candidates_sent_to_review'] == ['C2']
    assert '999' not in result['text'] and '2025' not in result['text']
    assert [c['id'] for c in result['claims']] == (['C2'] if supported else [])
    assert result['status'] == ('completed' if supported else 'insufficient_evidence')


def test_duplicate_claim_ids_disable_partial_candidate_recovery(source):
    body = {'claims': [candidate(), candidate('2025年收入为999亿元。')]}
    gateway = Gateway(*responses()[:2], body, body)
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'failed' and not result['claims']
    assert len(gateway.calls) == 4
    assert not any(a.get('partial_candidates_sent_to_review') for a in result['diagnostics']['answer_attempts'])


def test_empty_compose_for_unobserved_period_is_honest_insufficiency_without_retries(source):
    gateway = Gateway(*responses()[:2], {'claims': []})
    result = engine.answer_chart(source, '第1页图1中2025年收入是多少？', gateway)
    assert result['status'] == 'insufficient_evidence' and not result['claims']
    assert result['diagnostics']['protocol_failed'] is False and len(gateway.calls) == 3
    assert result['verification']['visual_review'] is False
    assert '73.3' not in result['text'] and '依据' in result['text']


@pytest.mark.parametrize('detail_status', ['reviewed', 'unavailable'])
def test_final_review_gets_same_source_detail_pixels_only_if_previously_reviewed(source, monkeypatch, detail_status):
    bbox = [.1, .1, .9, .9]
    detail = {'status': detail_status, 'bbox': bbox, 'source_id': source['id']}
    def read(*args):
        return {'status': 'reviewed', 'version': 'test-reviewed-v1',
                'items': [{**reading(), 'page': 1, 'provenance': 'separate_visual_read_and_review'}],
                'provenance': 'separate_visual_read_and_review', 'reading': {'detail_review': detail},
                'review': {'valid': True, 'supported_ids': ['V1'], 'rejected_ids': []},
                'audit': {'original_page_image_sha256': 'a' * 64}}
    monkeypatch.setattr(engine, 'read_chart_page', read)
    renders = []
    original = engine.page_content
    def render(s, n, box=None):
        renders.append((s['id'], s['original_path'], n, box))
        return original(s, n, box)
    monkeypatch.setattr(engine, 'page_content', render)
    gateway = Gateway({'claims': [candidate()]}, review_for)
    result = engine.answer_chart(source, '第1页图1收入是多少？', gateway)
    assert result['status'] == 'completed' and len(gateway.calls) == 2
    expected = [(source['id'], source['original_path'], 1, None)]
    if detail_status == 'reviewed':
        expected.append((source['id'], source['original_path'], 1, bbox))
    assert renders == expected and len(pixels(gateway.calls[-1][0])) == len(expected)
    assert result['evidence'][0]['visual_reading']['detail_review'] == detail


def _rejection_with_false_complete():
    # Each statement passes the local numeric gate. Original-page review then
    # rejects a wrong relation while mistakenly counting the remainder as full.
    claims = [candidate(), {'id': 'C2', 'text': '2021年收入高于2022年。',
                           'item_ids': ['P1_V1', 'P1_V2']}]
    return [{'items': [reading(), reading('V2', '73.8', '2022')]},
            {'items': [{'id': 'V1', 'supported': True}, {'id': 'V2', 'supported': True}]},
            {'claims': claims}, {'claims': [{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': False}],
                                'coverage': [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']}]}]


def _text_coverage(status='missing_evidence'):
    return {'coverage': [{'question_id': 'Q1', 'status': status, 'claim_ids': ['C1'], 'evidence_ids': ['E1']}]}


@pytest.mark.parametrize('complete', [False, True])
def test_rejected_candidate_requires_independent_coverage_of_only_accepted_text(source, complete):
    question = '第1页图1中2021年收入是多少？' if complete else '第1页图1中2021年收入是多少、比2022年高还是低？'
    def check_remaining(messages, purpose):
        body = payload(messages)
        assert purpose == 'verify' and not pixels(messages)
        assert set(body) == {'accepted_claims', 'question_parts', 'evidence_ids'}
        assert body['accepted_claims'] == [{'id': 'C1', 'text': candidate()['text']}]
        assert body['question_parts'] == completeness.question_parts(question)
        assert body['evidence_ids'] == ['E1']
        # Neither a discarded sentence nor the correct other-year source value
        # may help this call fill an unwritten answer.
        sent = json.dumps(messages, ensure_ascii=False)
        assert '2021年收入高于2022年。' not in sent and '73.8' not in sent
        assert source['sha256'] not in sent and 'reviewed_items' not in sent
        return _text_coverage('answered' if complete else 'missing_evidence')
    gateway = Gateway(*_rejection_with_false_complete(), check_remaining)
    result = engine.answer_chart(source, question, gateway)
    assert result['status'] == ('completed' if complete else 'insufficient_evidence')
    assert [c['id'] for c in result['claims']] == ['C1']
    assert result['claims'][0]['text'] == candidate()['text']
    assert len(gateway.calls) == 5
    checked = result['diagnostics']['coverage_rechecks'][0]
    assert checked['status'] == 'assessed' and checked['accepted_claim_ids'] == ['C1']
    assert checked['coverage']['complete'] is complete
    assert result['verification']['chart_relations'][-1] == {'id': 'C2', 'supported': False}
    review_prompt = gateway.calls[3][0][0]['content']
    assert '不要求每一条单独回答整个问题' in review_prompt
    assert 'bbox是包含上下文的来源区域' in review_prompt
    assert '不等于“等于”' in review_prompt


@pytest.mark.parametrize('failure', [ModelError('timeout', 'timeout'),
                                    ModelError('unknown POST', 'transport_failed'), InterruptedError('cancelled')])
def test_accepted_coverage_transport_unknown_or_cancellation_is_never_resent(source, failure):
    gateway = Gateway(*_rejection_with_false_complete(), failure)
    with pytest.raises(type(failure)):
        engine.answer_chart(source, '第1页图1收入及其变化方向？', gateway)
    assert len(gateway.calls) == 5


@pytest.mark.parametrize('bad', [{}, _text_coverage('evidence_available'),
                               {'coverage': [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C2']}]},
                               {'coverage': [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1'], 'evidence_ids': ['E9']}]}])
def test_accepted_coverage_protocol_failure_retains_facts_but_cannot_keep_old_complete(source, bad):
    gateway = Gateway(*_rejection_with_false_complete(), bad, bad)
    result = engine.answer_chart(source, '第1页图1收入及其变化方向？', gateway)
    assert result['status'] == 'insufficient_evidence'
    assert [c['id'] for c in result['claims']] == ['C1'] and len(result['evidence']) == 1
    assert result['completeness']['complete'] is not True
    assert result['diagnostics']['protocol_failed'] is True
    assert result['diagnostics']['coverage_rechecks'][0]['status'] == 'protocol_failed'
    assert len(gateway.calls) == 6 and 'protocol_correction' in payload(gateway.calls[-1][0])
    assert not pixels(gateway.calls[-1][0])


def test_accepted_coverage_protocol_correction_can_recover_once(source):
    gateway = Gateway(*_rejection_with_false_complete(), {}, _text_coverage('answered'))
    result = engine.answer_chart(source, '第1页图1中2021年收入是多少？', gateway)
    assert result['status'] == 'completed' and len(gateway.calls) == 6
    assert result['diagnostics']['protocol_failed'] is False
    assert result['diagnostics']['coverage_rechecks'][0]['coverage']['complete'] is True


def test_accepted_coverage_local_budget_failure_preserves_reviewed_fact(source, monkeypatch):
    original = engine._ask
    def bounded(gateway, purpose, system, body, *args, **kwargs):
        if 'accepted_claims' in body:
            raise ValueError('本地覆盖上下文超过预算')
        return original(gateway, purpose, system, body, *args, **kwargs)
    monkeypatch.setattr(engine, '_ask', bounded)
    gateway = Gateway(*_rejection_with_false_complete())
    result = engine.answer_chart(source, '第1页图1收入及其变化方向？', gateway)
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 4
    assert result['claims'][0]['text'] == candidate()['text'] and result['evidence']
    assert result['diagnostics']['coverage_rechecks'][0]['status'] == 'local_error'
    assert result['completeness']['complete'] is not True


@pytest.fixture
def detailed_source(source):
    from PIL import ImageDraw
    image = Image.new('RGB', (3200, 2400), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((960, 800, 1152, 1600), fill='blue')
    draw.line((320, 720, 2880, 720), fill='red', width=8)
    image.save(source['original_path'])
    from pathlib import Path
    source['sha256'] = hashlib.sha256(Path(source['original_path']).read_bytes()).hexdigest()
    return source


def _detail_relation(identifier='V1', text='甲柱低于0.20 mg/L参考线。'):
    return {'id': identifier, 'type': 'visual_relation', 'text': text,
            'bbox': [.30, .30, .36, .45],
            'reference_labels': [{'text': '0.20参考线', 'value': '0.20', 'unit': 'mg/L',
                                  'bbox': [.10, .30, .90, .32]}]}


def _final_denial_prefix():
    item = _detail_relation()
    return [{'items': [item]}, {'items': [{'id': 'V1', 'supported': True}]},
            {'claims': [candidate(item['text'])]},
            lambda m, p: review_for(m, p, supported=False, coverage_status='missing_evidence')]


@pytest.mark.parametrize('supported', [True, False])
def test_final_false_can_only_recover_with_new_original_pixels_and_accepted_text_coverage(detailed_source, supported):
    def detail_review(messages, purpose):
        body = payload(messages)
        assert purpose == 'verify' and len(pixels(messages)) == 2
        assert body['claims'] == [candidate(_detail_relation()['text'])]
        assert body['initial_verdicts'] == [{'id': 'C1', 'supported': False}]
        assert body['detail_view']['pixel_density_ratio'] >= 1.15
        assert body['detail_view']['target_localization_verified'] is False
        assert body['detail_view']['detail_image_sha256'] != body['detail_view']['original_page_image_sha256']
        assert body['reviewed_items'][0]['bbox'] == [0, 0, 1, 1]
        return {'claims': [{'id': 'C1', 'supported': supported}]}
    def accepted_only(messages, purpose):
        body = payload(messages)
        assert purpose == 'verify' and not pixels(messages)
        assert set(body) == {'accepted_claims', 'question_parts', 'evidence_ids'}
        assert body['accepted_claims'] == [{'id': 'C1', 'text': _detail_relation()['text']}]
        return _text_coverage('answered')
    gateway = Gateway(*_final_denial_prefix(), detail_review, *([accepted_only] if supported else []))
    result = engine.answer_chart(detailed_source, '第1页图1甲柱与参考线关系如何？', gateway)
    assert result['status'] == ('completed' if supported else 'insufficient_evidence')
    assert len(gateway.calls) == (6 if supported else 5)
    detail = result['diagnostics']['final_detail_reviews'][0]
    assert detail['status'] == 'reviewed' and detail['request_sent'] is True
    assert detail['bbox'] == [.075, .275, .925, .475]
    assert detail['initial_verdicts'] == [{'id': 'C1', 'supported': False}]
    assert detail['verdicts'] == [{'id': 'C1', 'supported': supported}]
    assert detail['original_coverage']['complete'] is False
    assert result['verification']['initial_chart_relations'] == [{'id': 'C1', 'supported': False}]
    assert result['verification']['chart_relations'] == [{'id': 'C1', 'supported': supported}]
    assert result['verification']['final_chart_relations'] == [{'id': 'C1', 'supported': supported}]
    assert result['verification']['detail_recovered_claim_ids'] == (['C1'] if supported else [])
    if supported:
        assert result['claims'][0]['chart_bindings'][0]['bbox'] == [0, 0, 1, 1]
        assert result['diagnostics']['coverage_rechecks'][0]['detail_recovered_ids'] == ['C1']
    else:
        assert not result['claims'] and result['diagnostics']['coverage_rechecks'] == []
        assert '本次独立核验未能确认全部回答，原件可能仍包含相关依据' in result['text']


def test_final_recheck_never_reuses_a_crop_already_sent_to_reader_review(detailed_source):
    item = _detail_relation()
    gateway = Gateway({'items': [item]}, {'items': [{'id': 'V1', 'supported': False}]},
                      {'items': [{'id': 'V1', 'supported': True}]},
                      {'claims': [candidate(item['text'])]},
                      lambda m, p: review_for(m, p, supported=False, coverage_status='missing_evidence'))
    result = engine.answer_chart(detailed_source, '第1页图1甲柱关系如何？', gateway)
    assert len(gateway.calls) == 5 and result['claims'] == []
    old = result['diagnostics']['readings'][0]['reading']['detail_review']
    detail = result['diagnostics']['final_detail_reviews'][0]
    assert old['status'] == 'reviewed'
    assert detail['detail_image_sha256'] == old['detail_image_sha256']
    assert detail['reason'] == 'no_new_higher_density_pixels' and detail['request_sent'] is False


def test_final_recheck_never_posts_without_higher_density_pixels(source):
    gateway = Gateway(*_final_denial_prefix())
    result = engine.answer_chart(source, '第1页图1甲柱关系如何？', gateway)
    assert len(gateway.calls) == 4 and result['claims'] == []
    detail = result['diagnostics']['final_detail_reviews'][0]
    assert detail['reason'] == 'no_new_higher_density_pixels' and detail['request_sent'] is False


@pytest.mark.parametrize('failure', [ModelError('unknown POST', 'transport_failed'),
                                    ModelError('timeout', 'timeout'), InterruptedError('cancelled')])
def test_final_detail_uncertain_transport_or_cancel_is_never_reissued(detailed_source, failure):
    gateway = Gateway(*_final_denial_prefix(), failure)
    with pytest.raises(type(failure)):
        engine.answer_chart(detailed_source, '第1页图1甲柱关系如何？', gateway)
    assert len(gateway.calls) == 5


def test_final_recovery_coverage_cannot_see_still_rejected_claims_or_images(detailed_source):
    first, second = _detail_relation(), _detail_relation('V2', '乙柱低于0.20 mg/L参考线。')
    claims = [candidate(first['text']), {'id': 'C2', 'text': second['text'], 'item_ids': ['P1_V2']}]
    def accepted_only(messages, purpose):
        body = payload(messages)
        assert not pixels(messages) and set(body) == {'accepted_claims', 'question_parts', 'evidence_ids'}
        assert body['accepted_claims'] == [{'id': 'C1', 'text': first['text']}]
        assert second['text'] not in json.dumps(body, ensure_ascii=False)
        return {'coverage': [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']},
                             {'question_id': 'Q2', 'status': 'missing_evidence', 'claim_ids': []}]}
    gateway = Gateway({'items': [first, second]}, {'items': [{'id': 'V1', 'supported': True}, {'id': 'V2', 'supported': True}]},
                      {'claims': claims}, lambda m, p: review_for(m, p, supported=False, coverage_status='missing_evidence'),
                      {'claims': [{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': False}]}, accepted_only)
    result = engine.answer_chart(detailed_source, '第1页图1甲柱关系如何？乙柱关系如何？', gateway)
    assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 6
    assert [c['id'] for c in result['claims']] == ['C1']
    assert [r['status'] for r in result['completeness']['items']] == ['answered', 'missing_evidence']


@pytest.mark.parametrize('source_conflict', [False, True, None, 'false'])
def test_saved_threshold_conflict_requires_explicit_resolution_on_new_pixels(detailed_source, source_conflict):
    # Exact failed final-review shape from live-page-variants threshold_v1:
    # Q1 conflict references rejected C1. The fixture uses synthetic pixels and
    # scripted verdicts, never supplies an oracle or claims visual accuracy.
    relation = _detail_relation()
    missing = {'id': 'V2', 'type': 'missing_precision',
               'text': '该柱未印刷精确数值，不能支持三位小数。', 'bbox': [.30, .30, .36, .45]}
    final = {'claims': [{'id': 'C1', 'supported': False}, {'id': 'C2', 'supported': True}],
             'coverage': [{'question_id': 'Q1', 'status': 'conflict', 'claim_ids': ['C1'], 'evidence_ids': ['E1']},
                          {'question_id': 'Q2', 'status': 'answered', 'claim_ids': ['C2'], 'evidence_ids': ['E1']}]}
    def detail(messages, purpose):
        body = payload(messages)
        assert body['initial_coverage']['status'] == 'conflict' and len(pixels(messages)) == 2
        assert body['claims'] == [candidate(relation['text'])]
        result = {'claims': [{'id': 'C1', 'supported': True}]}
        if source_conflict is not None:
            result['source_conflict'] = source_conflict
        return result
    coverage = {'coverage': [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']},
                             {'question_id': 'Q2', 'status': 'answered', 'claim_ids': ['C2']}]}
    gateway = Gateway({'items': [relation, missing]}, {'items': [{'id': 'V1', 'supported': True}, {'id': 'V2', 'supported': True}]},
                      {'claims': [candidate(relation['text']), {'id': 'C2', 'text': missing['text'], 'item_ids': ['P1_V2']}]},
                      final, detail, *([coverage] if source_conflict is False else []))
    result = engine.answer_chart(detailed_source, '第1页图1柱与参考线关系如何？这张图能否支持三位小数？', gateway)
    audit = result['diagnostics']['final_detail_reviews'][0]
    assert audit['original_coverage']['status'] == 'conflict'
    assert audit['initial_verdicts'] == [{'id': 'C1', 'supported': False}]
    assert result['verification']['initial_chart_relations'] == final['claims']
    if source_conflict is False:
        assert result['status'] == 'completed' and len(gateway.calls) == 6
        assert audit['source_conflict'] is False and audit['recovered_ids'] == ['C1']
        assert all(r['supported'] for r in result['verification']['chart_relations'])
    else:
        assert result['status'] == 'insufficient_evidence' and len(gateway.calls) == 5
        assert result['completeness']['status'] == 'conflict' and audit['recovered_ids'] == []
        assert [c['id'] for c in result['claims']] == ['C2']


def test_single_page_crop_cannot_resolve_a_conflict_after_multiple_pages():
    coverage = {'status': 'conflict', 'complete': False, 'items': []}
    gateway = Gateway()
    result = engine._final_detail_review({}, [], [], coverage,
              {'P1_V1': {'page': 1}, 'P2_V1': {'page': 2}}, [], gateway, lambda: False, lambda _: None)
    assert result['reason'] == 'cross_page_conflict_requires_full_source_review'
    assert result['request_sent'] is False and result['recovered_ids'] == [] and gateway.calls == []


def test_final_detail_bad_protocol_does_not_retry_or_change_old_false(detailed_source):
    gateway = Gateway(*_final_denial_prefix(), {'claims': [{'id': 'C99', 'supported': True}]})
    result = engine.answer_chart(detailed_source, '第1页图1甲柱关系如何？', gateway)
    assert len(gateway.calls) == 5 and result['claims'] == []
    assert result['diagnostics']['final_detail_reviews'][0]['status'] == 'protocol_failed'
    assert result['verification']['chart_relations'] == [{'id': 'C1', 'supported': False}]


@pytest.mark.parametrize('valid_replacement', [False, True])
def test_final_crop_hint_comes_from_selected_read_attempt_and_exact_candidate(detailed_source, valid_replacement):
    first = _detail_relation()
    second = copy.deepcopy(first)
    second['bbox'] = [.65, .65, .85, .80]
    second['text'] = '甲柱高于0.20 mg/L参考线。' if valid_replacement else '甲柱为999 mg/L。'
    chosen = second if valid_replacement else first
    invalid = {'id': 'V2', 'type': 'bad'}
    gateway = Gateway({'items': [first, invalid]}, {'items': [second]},
                      {'items': [{'id': 'V1', 'supported': True}]},
                      {'claims': [candidate(chosen['text'])]},
                      lambda m, p: review_for(m, p, supported=False, coverage_status='missing_evidence'),
                      {'claims': [{'id': 'C1', 'supported': False}]})
    result = engine.answer_chart(detailed_source, '第1页图1甲柱关系如何？', gateway)
    detail = result['diagnostics']['final_detail_reviews'][0]
    expected = [.075, .275, .925, .825] if valid_replacement else [.075, .275, .925, .475]
    assert detail['bbox'] == expected and detail['request_sent'] is True
    assert detail['claims'] == [candidate(chosen['text'])]
    item = result['evidence'][0]['visual_reading']['items'][0]
    assert item['read_attempt'] == (2 if valid_replacement else 1)
    assert item['text'] == chosen['text'] and item['bbox'] == [0, 0, 1, 1]
    assert result['claims'] == [] and len(gateway.calls) == 6


def _terminal(client, pid, aid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f'/api/projects/{pid}/answers/{aid}').json()
        if result['status'] in {'completed', 'insufficient_evidence', 'failed', 'cancelled', 'needs_clarification'}:
            return result
        time.sleep(.01)
    pytest.fail('Answer did not finish')


@pytest.fixture
def api(tmp_path, monkeypatch):
    import pymupdf
    from backend import ocr_native
    monkeypatch.setattr(ocr_native, 'recognize', lambda *a, **k: pytest.fail('No native OCR in route test'))
    root = tmp_path / 'runtime'; root.mkdir()
    (root / 'model.json').write_text(json.dumps({'provider': 'explicit fake', 'endpoint': 'http://127.0.0.1:1/v1',
        'api_key': 'FAKE_ONLY', 'models': {'table': 'fake', 'document': 'fake', 'verify': 'fake'}}))
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_text((40, 40), '图1 预算收入', fontname='china-s', fontsize=14)
    pdf = doc.tobytes(); doc.close()
    app = create_app(data_dir=root, samples_dir=tmp_path/'samples', enable_workers=False,
                     answer_gateway_factory=lambda config, cancelled: Gateway())
    with TestClient(app) as client:
        pid = client.post('/api/projects', json={'name': 'chart route fixture'}).json()['id']
        def upload(name='chart.pdf'):
            with pymupdf.open(stream=pdf, filetype='pdf') as distinct:
                distinct.set_metadata({'title': name})
                content = distinct.tobytes()
            response = client.post(f'/api/projects/{pid}/import', files=[('files', (name, content))])
            assert response.status_code == 200, response.text
            for job in response.json()['jobs']:
                app.state.service.process_job(job['id'])
            sid = response.json()['sources'][0]['id']
            detail = client.get(f'/api/sources/{sid}').json()
            assert detail['source']['usable'], detail
            return sid
        yield client, pid, upload


def _stub_result(marker):
    return {'status': 'completed', 'text': marker, 'claims': [], 'evidence': [], 'notes': [],
            'verification': {'status': 'passed'}, 'diagnostics': {'test_route': marker}}


@pytest.mark.parametrize('mode', ['document', 'multi_source'])
def test_api_routes_single_source_no_history_without_reparse(api, monkeypatch, mode):
    client, pid, upload = api
    sid = upload(); calls = []
    before = client.app.state.service.store.get('sources', sid)
    def chart(source, question, *args, audit=None):
        assert callable(audit)
        calls.append((source['id'], question))
        return _stub_result('chart-route')
    monkeypatch.setattr(engine, 'answer_chart', chart)
    response = client.post(f'/api/projects/{pid}/answers', json={'question': '图1收入是多少？',
        'source_id': sid, 'source_ids': [sid], 'mode': mode, 'request_id': str(uuid.uuid4())})
    assert response.status_code == 202, response.text
    record = _terminal(client, pid, response.json()['id'])
    assert record['status'] == 'completed' and record['result']['text'] == 'chart-route', record
    assert calls == [(sid, '图1收入是多少？')]
    assert client.app.state.service.store.get('sources', sid) == before
    assert 'query' not in record['result'] and record['result']['trace'][-1]['stage'] == '回答完成'


@pytest.mark.parametrize('mode', ['document', 'multi_source'])
@pytest.mark.parametrize('question', ['根据第1页表1计算甲收入占总收入的比例。',
                                    '第1页图1两年收入相差多少？'])
def test_api_single_pdf_table_or_derived_calculation_uses_tools(api, monkeypatch, mode, question):
    from backend import document_engine, cross_source_engine
    client, pid, upload = api
    sid = upload(); calls = []
    # Both document entry modes must use evidence-bound calculation tools;
    # the chart reader may transcribe values but must not invent arithmetic.
    monkeypatch.setattr(engine, 'answer_chart', lambda *a, **k: pytest.fail('Calculation intercepted by chart reader'))
    def document(source, received, *args, retriever=None):
        assert retriever is not None
        calls.append(('document', source['id'], received))
        return _stub_result('previous-document-route')
    def cross(sources, received, history, *args, retrieval_factory=None, audit=None, actual_user_question=None):
        assert callable(retrieval_factory) and callable(audit)
        assert actual_user_question == question
        calls.append(('multi_source', sources[0]['id'], received))
        result = _stub_result('previous-cross-route'); status = result.pop('status')
        return status, result
    monkeypatch.setattr(document_engine, 'answer_document', document)
    monkeypatch.setattr(cross_source_engine, 'answer_cross_source', cross)
    response = client.post(f'/api/projects/{pid}/answers', json={'question': question,
        'source_id': sid, 'source_ids': [sid], 'mode': mode, 'request_id': str(uuid.uuid4())})
    assert response.status_code == 202, response.text
    record = _terminal(client, pid, response.json()['id'])
    assert record['status'] == 'completed', record
    assert calls == [('multi_source', sid, question)]
    assert record['result']['text'] == 'previous-cross-route'


@pytest.mark.parametrize('with_history', [False, True])
def test_api_multi_source_or_history_keeps_existing_cross_source_route(api, monkeypatch, with_history):
    from backend import cross_source_engine
    client, pid, upload = api
    sid = upload(); calls = []; resolutions = []
    def resolve(item, history, source, gateway, *, history_loader=None):
        assert history and callable(history_loader)
        resolutions.append((item['question'], history[0]['answer_id'], source['id']))
        return {'status':'ready','normalized_question':item['question'],'intent':{}}
    monkeypatch.setattr('backend.query_planner.resolve_followup', resolve)
    def parent_chart(*args, audit=None):
        assert callable(audit)
        return _stub_result('parent-chart')
    monkeypatch.setattr(engine, 'answer_chart', parent_chart)
    def cross(sources, question, history, *args, retrieval_factory=None, audit=None, actual_user_question=None):
        assert callable(retrieval_factory) and callable(audit)
        assert actual_user_question == question
        calls.append((len(sources), bool(history)))
        result = _stub_result('cross-route'); status = result.pop('status')
        return status, result
    monkeypatch.setattr(cross_source_engine, 'answer_cross_source', cross)
    body = {'question': '图1收入是多少？', 'source_id': sid, 'source_ids': [sid],
            'mode': 'multi_source', 'request_id': str(uuid.uuid4())}
    if with_history:
        first = client.post(f'/api/projects/{pid}/answers', json=body)
        parent = _terminal(client, pid, first.json()['id'])
        assert parent['status'] == 'completed'
        body.update(parent_answer_id=parent['id'], request_id=str(uuid.uuid4()))
    else:
        body['source_ids'].append(upload('second-chart.pdf'))
    monkeypatch.setattr(engine, 'answer_chart', lambda *args, **kwargs: pytest.fail('Unsupported scope routed to chart engine'))
    response = client.post(f'/api/projects/{pid}/answers', json=body)
    assert response.status_code == 202, response.text
    record = _terminal(client, pid, response.json()['id'])
    assert record['status'] == 'completed' and record['result']['text'] == 'cross-route', record
    assert calls == [(1 if with_history else 2, with_history)]
    assert resolutions == ([(body['question'], parent['id'], sid)] if with_history else [])
