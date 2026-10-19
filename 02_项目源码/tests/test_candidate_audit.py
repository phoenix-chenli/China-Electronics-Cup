"""Rejected proposals remain bounded diagnostic data, never new evidence."""
import copy
import json

import pytest

from backend.candidate_audit import (append_attempt, capture_candidate,
                                     review_feedback, with_latest_feedback)


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def candidate(text='甲金额为12万元。'):
    return {'status': 'completed', 'claims': [{'text': text, 'evidence_ids': ['E1'],
        'table_bindings': [{'evidence_id': 'E1', 'table_name': 'table_1', 'cell_id': 'R2C3',
                            'value': '12', 'statement': text, 'column_label': '金额（万元）',
                            'row_label': '甲', 'unit': '万元'}]}]}


def capture(response=None, **kwargs):
    return capture_candidate(response or candidate(), {'E1': {'id': 'E1', 'source_id': 'source'}}, {},
                             round_number=kwargs.pop('round_number', 2), stage='candidate_validation',
                             reason=kwargs.pop('reason', '绑定事实必须是回答中可定位的精确子句'), **kwargs)


def test_audit_redacts_before_storage_and_ignores_extra_nested_payloads():
    secrets = 'Bearer secret-token /Users/person/private.docx password=secret-pass https://user:pass@host/path "api_key":"quoted-key" Basic c2VjcmV0 data:image/png;base64,binary-private'
    raw = candidate(secrets)
    raw.update(api_key='root-key', messages=[{'content': 'private-prompt'}], image='data:image/png;base64,private-image')
    raw['claims'][0]['table_bindings'][0].update(config={'api_key': 'nested-key'}, cell={'text': 'unexpected-cell-copy'})
    before = copy.deepcopy(raw)
    record = capture(raw, reason='读取 /tmp/private-file 失败，api_key=reason-key')
    serialized = json.dumps(record, ensure_ascii=False)
    for forbidden in ('secret-token', '/Users/person', 'secret-pass', 'user:pass@', 'root-key',
                      'private-prompt', 'private-image', 'nested-key', 'unexpected-cell-copy', '/tmp/private-file', 'reason-key',
                      'quoted-key', 'c2VjcmV0', 'binary-private'):
        assert forbidden not in serialized
    assert record['redacted'] and record['publication_status'] == 'not_adopted'
    assert record['scope'] == 'candidate_attempt' and raw == before


def test_large_candidate_prioritizes_failing_claim_and_records_truncation():
    raw = candidate('甲' * 10000)
    raw['claims'] = [copy.deepcopy(raw['claims'][0]) for _ in range(16)]
    for claim in raw['claims']:
        claim['table_bindings'] *= 16
    record = capture(raw, details={'claim_index': 13, 'kind': 'table_binding'})
    assert size(record) <= 3980
    assert record['claims'][0]['candidate_index'] == 13
    assert record['truncated'] and record['omitted_claims'] > 0
    assert record['claims'][0]['omitted_bindings'] > 0
    assert record['original_lengths']
    attempts = []
    for number in range(1, 9):
        attempts = append_attempt(attempts, capture(raw, round_number=number, details={'claim_index': 13}))
    assert [item['round'] for item in attempts] == [6, 7, 8]
    assert size(attempts) <= 12000


def test_audit_separates_unknown_references_and_preserves_real_identity():
    raw = candidate()
    raw['claims'][0]['evidence_ids'] += ['E404', {'id': 'E1'}]
    raw['claims'][0]['calculation_ids'] = ['K404']
    raw['claims'][0]['table_bindings'][0]['evidence_id'] = 'E404'
    record = capture(raw)
    claim = record['claims'][0]
    assert claim['evidence_ids'] == ['E1']
    assert claim['unresolved_evidence_ids'] == ['E404']
    assert claim['calculation_ids'] == [] and claim['unresolved_calculation_ids'] == ['K404']
    binding = claim['table_bindings'][0]
    assert 'evidence_id' not in binding and binding['unresolved_evidence_id'] == 'E404'


@pytest.mark.parametrize('spare', [0, 200, 500, 4000, 10000])
def test_feedback_only_uses_spare_space_without_evicting_evidence_or_retained_facts(spare):
    payload = {'evidence': [{'id': 'E1', 'quote': '原文' * 1000}],
               'retained_claims': [{'id': 'C1', 'text': '先前已核验事实。'}]}
    before = copy.deepcopy(payload)
    record = capture(candidate('甲' * 1800))
    result = with_latest_feedback(payload, [record], size(payload) + spare)
    assert size(result) <= size(payload) + spare
    assert payload == before
    assert result['evidence'] == before['evidence'] and result['retained_claims'] == before['retained_claims']
    if 'last_candidate_feedback' in result:
        assert size(result['last_candidate_feedback']) <= 4000
    if spare == 0:
        assert result == payload
    if spare == 10000:
        assert result['last_candidate_feedback']['attempt_id'] == 'A2'


def test_review_feedback_preserves_missing_or_false_table_verdict_without_extra_prose():
    review = {'supported': True, 'answer_complete': True,
              'claims': [{'id': 'C1', 'supported': True}],
              'table_relations': [{'id': 'C1', 'supported': False}],
              'coverage': [{'question_id': 'Q1', 'status': 'answered'}], 'api_key': 'do-not-copy'}
    projected = review_feedback(review)
    assert projected['table_relations'] == [{'id': 'C1', 'supported': False}]
    assert projected['coverage_statuses'] == ['answered']
    assert 'api_key' not in projected
    missing = review_feedback({'supported': True})
    assert missing['table_relations'] == [] and missing['table_relations_present'] is False
    record = capture(review=review)
    assert record['review'] == projected


def chart_candidate(text='甲类为10元。'):
    return {'status': 'completed', 'claims': [{'text': text, 'evidence_ids': ['E1'],
        'chart_bindings': [{'kind': 'printed_point', 'evidence_id': 'E1', 'statement': text,
                            'category': '甲类', 'value': '10', 'unit': '元', 'association': 'same_line',
                            'category_observation_id': 'P1B2', 'value_observation_id': 'P1B2',
                            'unit_observation_id': 'P1B2'}]}]}


def test_chart_audit_whitelists_scalar_fields_and_redacts_before_retention():
    raw = chart_candidate()
    binding = raw['claims'][0]['chart_bindings'][0]
    binding.update(title='Bearer hidden-token /Users/person/chart.png',
                   series={'api_key': 'nested-secret'},
                   field_observations={'value': {'text': 'unexpected-source-copy'}},
                   source_observations=[{'api_key': 'observation-secret'}],
                   image='data:image/png;base64,private-image',
                   observation_ids=['P1B2', {'config': 'private-object'}, 'api_key=id-secret'])
    before = copy.deepcopy(raw)
    record = capture(raw, details={'kind': 'chart_binding'})
    retained = record['claims'][0]['chart_bindings'][0]
    assert retained['category_observation_id'] == 'P1B2' and retained['series'] is None
    assert retained['observation_ids'][0] == 'P1B2' and retained['invalid_observation_ids'] is True
    serialized = json.dumps(record, ensure_ascii=False)
    for forbidden in ('hidden-token', '/Users/person', 'nested-secret', 'unexpected-source-copy',
                      'observation-secret', 'private-image', 'private-object', 'id-secret'):
        assert forbidden not in serialized
    assert 'field_observations' not in retained and 'source_observations' not in retained
    assert record['redacted'] and record['error_kind'] == 'chart_binding' and raw == before


def test_chart_binding_unknown_and_outside_claim_evidence_remain_diagnostic_only():
    raw = chart_candidate()
    raw['claims'][0]['evidence_ids'].append('E404')
    first = raw['claims'][0]['chart_bindings'][0]
    first['evidence_id'] = 'E404'
    raw['claims'][0]['chart_bindings'].append({**first, 'evidence_id': 'E2'})
    record = capture_candidate(raw, {'E1': {}, 'E2': {}}, {}, round_number=1,
                               stage='candidate_validation', reason='图表引用不合法')
    claim = record['claims'][0]
    assert claim['evidence_ids'] == ['E1'] and claim['unresolved_evidence_ids'] == ['E404']
    unknown, outside = claim['chart_bindings']
    assert 'evidence_id' not in unknown and unknown['unresolved_evidence_id'] == 'E404'
    assert outside['evidence_id'] == 'E2' and outside['outside_claim_evidence'] is True
    assert record['publication_status'] == 'not_adopted'


@pytest.mark.parametrize('missing', [False, True])
def test_chart_review_feedback_targets_its_failed_claim_not_the_first_text_claim(missing):
    raw = chart_candidate()
    raw['claims'].insert(0, {'text': '普通文字已得到支持。', 'evidence_ids': ['E1']})
    review = {'supported': False, 'claims': [{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': True}],
              'visual_supported': True}
    if not missing:
        review['chart_relations'] = [{'id': 'C2', 'supported': False, 'reason': 'api_key=hidden-review-secret'},
                                     {'id': 'C999', 'supported': True, 'nested': {'private': 'ignored'}}]
    before = copy.deepcopy(review)
    record = capture(raw, review=review)
    assert record['claim_index'] == 2 and record['claims'][0]['candidate_index'] == 2
    feedback = record['review']
    assert feedback['chart_relations_present'] is (not missing)
    assert feedback['chart_relations'] == ([] if missing else [{'id': 'C2', 'supported': False}])
    assert 'hidden-review-secret' not in json.dumps(record) and review == before


def test_chart_binding_budget_is_trimmed_before_discarding_failing_claim_text():
    text = '待核验图表事实。' * 80
    raw = chart_candidate(text)
    raw['claims'][0]['chart_bindings'] *= 16
    before = copy.deepcopy(raw)
    record = capture(raw, details={'claim_index': 1, 'kind': 'chart_binding'})
    assert size(record) <= 3980 and record['truncated'] is True
    claim = record['claims'][0]
    assert claim['text'] == text
    assert 0 < len(claim['chart_bindings']) < 4 and claim['omitted_chart_bindings'] > 12
    assert raw == before
    payload = {'evidence': [{'id': 'E1', 'quote': '原始资料完整保留。'}],
               'retained_claims': [{'id': 'C1', 'text': '已验证事实。'}]}
    projected = with_latest_feedback(payload, [record], size(payload) + 600)
    assert size(projected) <= size(payload) + 600
    assert projected['evidence'] == payload['evidence'] and projected['retained_claims'] == payload['retained_claims']
    assert size(append_attempt([], record)) <= 12000


def test_chart_observation_lists_are_bounded_without_copying_nested_objects():
    raw = chart_candidate()
    binding = raw['claims'][0]['chart_bindings'][0]
    binding['observation_ids'] = ['P1B2'] * 40 + [{'password': 'discarded-nested-secret'}]
    record = capture(raw)
    retained = record['claims'][0]['chart_bindings'][0]
    assert len(retained['observation_ids']) == 16
    assert retained['omitted_observation_ids'] == 25 and record['truncated']
    assert 'discarded-nested-secret' not in json.dumps(record)
