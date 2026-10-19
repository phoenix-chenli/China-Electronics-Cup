"""Offline integration: current raw requests survive normalized followups."""
from backend import completeness
from backend.cross_source_engine import answer_cross_source
from test_cross_source_engine import sources, Responses, actions, search, approved, completion_coverage


def assert_requests(calls, normalized, actual):
    for call in calls:
        payload = call['payload']
        assert payload['question'] == normalized
        assert payload['actual_user_question'] == actual
        assert payload['question_parts'] == completeness.question_parts(actual)


def test_actual_new_request_survives_generation_review_and_completion_repair(sources):
    sources[1]['pages'] = [{'number': 1, 'text': '甲项目预算10万元。乙项目工期20天。'}]
    normalized = '甲项目预算多少？'
    actual = '甲项目预算多少？并说明乙项目工期。'
    first = {'status': 'completed', 'claims': [{'text': '甲项目预算10万元。', 'evidence_ids': ['E1']}]}
    second = {'status': 'completed', 'claims': [{'text': '乙项目工期20天。', 'evidence_ids': ['E1']}]}
    gateway = Responses(actions(search()), first,
        approved(answer_complete=False, coverage=completion_coverage()), second,
        approved(claims=[{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': True}], coverage=completion_coverage(final=True)))
    status, result = answer_cross_source(sources, normalized, [], gateway, actual_user_question=actual)
    assert status == 'completed' and len(result['claims']) == 2
    assert len(gateway.calls) == 5 and len(result['steps']) == 1
    assert result['completion_repair']['resolved'] is True
    assert_requests(gateway.calls, normalized, actual)


def test_old_normalized_only_coverage_cannot_release_missing_current_requirement(sources):
    sources[1]['pages'] = [{'number': 1, 'text': '甲项目预算10万元。乙项目工期20天。'}]
    normalized = '甲项目预算多少？'
    actual = '甲项目预算多少？并说明乙项目工期。'
    first = {'status': 'completed', 'claims': [{'text': '甲项目预算10万元。', 'evidence_ids': ['E1']}]}
    gateway = Responses(actions(search()), first, approved())
    status, result = answer_cross_source(sources, normalized, [], gateway, actual_user_question=actual)
    assert status != 'completed'
    assert result['completeness']['complete'] is not True
    assert result['verification']['reason'] == 'coverage_unverified'
    assert len(result['completeness']['items']) == 2
    assert_requests(gateway.calls, normalized, actual)


def test_omitted_raw_input_retains_existing_single_question_contract(sources):
    question = '林禾是否适用优惠规则？'
    answer = {'status': 'completed', 'claims': [{'text': '林禾适用优惠规则。', 'evidence_ids': ['E1']}]}
    gateway = Responses(actions(search()), answer, approved())
    status, result = answer_cross_source(sources, question, [], gateway)
    assert status == 'completed'
    assert_requests(gateway.calls, question, question)
