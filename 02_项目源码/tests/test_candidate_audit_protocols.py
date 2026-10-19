"""Received-candidate projections only. All inputs are synthetic; no API calls."""
import copy
import json
import math

import pytest

from backend.candidate_audit import capture_candidate, review_feedback, with_latest_feedback
from test_cross_source_engine import sources


def project(claims, review=None, calculations=None, details=None):
    return capture_candidate({'claims': claims}, {'E1': {}, 'E2': {}}, calculations or {},
                             round_number=1, stage='candidate_validation', reason='待诊断候选被拒绝',
                             review=review, details=details)


def visual():
    return {'text': '甲类占比为30%。', 'evidence_ids': ['E1'], 'visual_bindings': [
        {'evidence_id': 'E1', 'item_id': 'V3', 'statement': '甲类占比为30%。'}]}


def query(value=-7.2):
    return {'text': '甲区2025年增长率为-7.2%。', 'evidence_ids': ['E1'], 'query_bindings': [{
        'evidence_id': 'E1', 'context_id': 'a' * 64, 'cell_id': 'Qaaaaaaaaaaaa:R1C3',
        'row_index': 0, 'column_index': 2, 'column_label': 'growth', 'value': value,
        'unit': '%', 'unit_column': 'unit', 'entity_cells': ['Qaaaaaaaaaaaa:R1C1', 'Qaaaaaaaaaaaa:R1C2'],
        'statement': '甲区2025年增长率为-7.2%。'}]}


def test_visual_original_binding_retained_without_copying_arbitrary_nested_payload():
    raw = visual()
    raw['visual_bindings'][0].update(source={'api_key': 'nested-secret'},
                                   image='data:image/png;base64,do-not-copy', bbox=[0, 0, 1, 1])
    original = copy.deepcopy(raw)
    record = project([raw])
    retained = record['claims'][0]['visual_bindings'][0]
    assert retained == {'binding_index': 1, 'evidence_id': 'E1', 'item_id': 'V3', 'statement': raw['text']}
    assert record['projection_version'] == 2
    assert record['publication_status'] == 'not_adopted' and raw == original
    assert not {'source', 'image', 'bbox'} & retained.keys()


@pytest.mark.parametrize('value', [-7.2, -0.0, 0, -3, 1e100, None, True, False, '0001', '-7.2%'])
def test_query_values_keep_exact_finite_json_types_and_ids(value):
    raw = query(value)
    original = copy.deepcopy(raw)
    record = project([raw])
    retained = record['claims'][0]['query_bindings'][0]
    expected = {'binding_index': 1, **raw['query_bindings'][0]}
    assert retained == expected and type(retained['value']) is type(value)
    if isinstance(value, float) and value == 0:
        assert math.copysign(1, retained['value']) == math.copysign(1, value)
    json.dumps(record, allow_nan=False)
    assert raw == original


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), {'secret': 'nested-secret'},
                                 ['nested-secret'], 10 ** 10000], ids=['nan', 'inf', 'negative_inf', 'dict', 'list', 'huge_int'])
def test_nonfinite_nested_and_oversize_numbers_are_explicitly_omitted(value):
    record = project([query(value)])
    binding = record['claims'][0]['query_bindings'][0]
    assert binding['value'] is None and binding['omitted_fields'] == ['value']
    assert record['truncated'] is True
    assert 'nested-secret' not in json.dumps(record, allow_nan=False)


def test_new_id_fields_arrays_and_units_use_the_existing_redaction_boundary():
    raw = query('password=never-copy')
    binding = raw['query_bindings'][0]
    binding.update(context_id='Bearer context-secret', cell_id='/Users/private/source.csv',
                   unit_column='api_key=unit-secret', entity_cells=[
                       'Q1:R1C1', {'password': 'nested-id-secret'}, 'Bearer id-secret'] + ['X'] * 20)
    visual_claim = visual()
    visual_claim['visual_bindings'][0]['item_id'] = 'https://user:pass@server/item'
    record = project([raw, visual_claim])
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
    for secret in ('never-copy', 'context-secret', '/Users/private', 'unit-secret',
                   'nested-id-secret', 'id-secret', 'user:pass@'):
        assert secret not in encoded
    item = record['claims'][0]['query_bindings'][0]
    assert item['invalid_entity_cells'] and item['omitted_entity_cells'] == 7
    assert len(item['entity_cells']) == 15
    assert record['redacted'] and record['truncated']


@pytest.mark.parametrize('name,factory', [('visual_bindings', visual), ('query_bindings', query)])
def test_unknown_and_outside_claim_evidence_never_become_authorized(name, factory):
    raw = factory()
    first = raw[name][0]
    first['evidence_id'] = 'E404'
    raw[name].append({**first, 'evidence_id': 'E2'})
    record = project([raw])
    unknown, outside = record['claims'][0][name]
    assert unknown['unresolved_evidence_id'] == 'E404' and 'evidence_id' not in unknown
    assert outside['evidence_id'] == 'E2' and outside['outside_claim_evidence'] is True
    assert record['claims'][0]['evidence_ids'] == ['E1']


def test_new_review_relations_are_boolean_only_and_bounded():
    raw = {'query_relations': [{'id': 'C2', 'supported': 'true', 'reason': 'secret'}],
           'operation_relations': [{'id': 'K1', 'intent_supported': True, 'parameter_roles_supported': False,
                                    'scope_supported': 1, 'pricing_supported': {'secret': 'nested'}}] * 25}
    before = copy.deepcopy(raw)
    review = review_feedback(raw)
    assert review['query_relations'] == [{'id': 'C2', 'supported': None}]
    assert review['operation_relations'][0] == {'id': 'K1', 'intent_supported': True,
        'parameter_roles_supported': False, 'scope_supported': None, 'pricing_supported': None}
    assert len(review['operation_relations']) == 16 and review['omitted_operation_relations'] == 9
    assert review['query_relations_present'] and review['operation_relations_present'] and review['truncated']
    assert 'secret' not in json.dumps(review) and raw == before


@pytest.mark.parametrize('kind,factory', [('chart_relations', visual), ('query_relations', query)])
@pytest.mark.parametrize('missing', [True, False])
def test_new_missing_or_rejected_relation_prioritizes_its_actual_claim(kind, factory, missing):
    claims = [{'text': '普通事实。', 'evidence_ids': ['E1']} for _ in range(3)] + [factory()]
    review = {} if missing else {kind: [{'id': 'C4', 'supported': False}]}
    record = project(claims, review)
    assert record['claim_index'] == 4 and record['claims'][0]['candidate_index'] == 4
    assert record['review'][kind + '_present'] is (not missing)
    assert record['claims'][0].get('visual_bindings') or record['claims'][0].get('query_bindings')


@pytest.mark.parametrize('missing', [True, False])
def test_operation_rejection_targets_dependent_calculation_claim_without_false_support(missing):
    claims = [{'text': '普通事实。', 'evidence_ids': ['E1']},
              {'text': '费用为12元。', 'calculation_ids': ['K2']}]
    calculations = {'K2': {'parameters': [{'binding': {'calculation_id': 'K1'}}]},
                    'K1': {'rule_origin': 'user_operation'}}
    review = {} if missing else {'operation_relations': [{'id': 'K1', 'intent_supported': True,
        'parameter_roles_supported': True, 'scope_supported': False, 'pricing_supported': True}]}
    record = project(claims, review, calculations)
    assert record['claim_index'] == 2 and record['claims'][0]['calculation_ids'] == ['K2']
    assert record['review']['operation_relations_present'] is (not missing)
    if not missing:
        assert record['review']['operation_relations'][0]['scope_supported'] is False


@pytest.mark.parametrize('name,factory', [('visual_bindings', visual), ('query_bindings', query)])
def test_new_binding_budget_trims_repeats_with_explicit_omission_and_preserves_inputs(name, factory):
    raw = factory()
    raw[name] *= 30
    raw[name][0]['statement'] = '待复核事实。' * 200
    before = copy.deepcopy(raw)
    record = project([raw])
    claim = record['claims'][0]
    assert len(json.dumps(record, ensure_ascii=False)) <= 3980
    assert record['truncated'] and claim['omitted_' + name] >= 26
    assert raw == before
    payload = {'evidence': [{'id': 'E1', 'quote': '真实原文'}], 'retained_claims': [{'id': 'C1', 'text': '已核验'}]}
    snapshot = copy.deepcopy(payload)
    budget = len(json.dumps(payload, ensure_ascii=False)) + 800
    feedback = with_latest_feedback(payload, [record], budget)
    assert len(json.dumps(feedback, ensure_ascii=False)) <= budget
    assert feedback['evidence'] == snapshot['evidence'] and feedback['retained_claims'] == snapshot['retained_claims']
    assert payload == snapshot


def test_invalid_binding_shapes_are_diagnostic_without_nested_copy_or_history_invention():
    raw = {'text': '未知候选', 'visual_bindings': {'secret': 'dont-copy'}, 'query_bindings': [123]}
    record = project([raw])
    assert record['claims'][0]['invalid_visual_bindings'] is True
    assert record['claims'][0]['query_bindings'] == [{'binding_index': 1, 'invalid_shape': True}]
    assert 'dont-copy' not in json.dumps(record)
    historical = {'text': '历史缺失投影'}
    projection = project([historical])
    assert 'visual_bindings' not in projection['claims'][0] and 'query_bindings' not in projection['claims'][0]
    assert historical == {'text': '历史缺失投影'}


@pytest.mark.parametrize('name,factory', [('visual_bindings', visual), ('query_bindings', query)])
def test_real_controller_retains_rejected_binding_and_does_not_retry_unknown_post(sources, name, factory):
    from backend.cross_source_engine import answer_cross_source
    from backend.model_gateway import ModelError
    from test_cross_audit_persistence import Script
    from test_cross_source_engine import actions, sql, search, calculation, completed
    bad = completed(**{name: factory()[name]})
    before = copy.deepcopy(bad)
    unknown = ModelError('Response outcome unknown', 'timeout')
    gateway = Script(actions(sql(), search()), actions(calculation()), bad, unknown)
    events = []
    with pytest.raises(ModelError) as caught:
        answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway, audit=events.append)
    assert caught.value is unknown and len(gateway.calls) == 4
    attempt = events[-1]['candidate_attempts'][-1]
    binding = attempt['claims'][0][name][0]
    assert binding['statement'] == bad['claims'][0][name][0]['statement']
    assert binding['evidence_id'] == 'E1'
    assert attempt['publication_status'] == 'not_adopted'
    assert gateway.calls[-1]['payload']['last_candidate_feedback']['claims'][0][name] == attempt['claims'][0][name]
    assert bad == before
