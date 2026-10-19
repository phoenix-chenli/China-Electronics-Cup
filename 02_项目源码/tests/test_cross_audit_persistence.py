"""Synthetic received responses only: no network, provider, or model calls."""
import copy
import hashlib
import json

import pytest

from backend.cross_source_engine import answer_cross_source
from backend.model_gateway import ModelError
from test_cross_source_engine import (
    Responses, actions, approved, calculation, completed, search, sources, sql,
)


class Script(Responses):
    def __call__(self, messages, purpose):
        response = super().__call__(messages, purpose)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response()
        return response


def rejection(**extra):
    return approved(supported=False, claims=[{'id': 'C1', 'supported': False}], **extra)


def prepared(*tail):
    return Script(actions(sql(), search()), actions(calculation()), completed(), *tail)


def persisted_result_history(history):
    return [event for event in history if event['kind'] in {
        'calculation_mechanically_executed', 'independent_review_received', 'candidate_rejected'}]


def assert_planner_dispatch_history(history, gateway):
    """New execution events are audited independently of candidate persistence."""
    assert [event['sequence'] for event in history] == list(range(1, len(history) + 1))
    dispatches = [event['record'] for event in history if event['kind'] == 'planner_gateway_dispatch']
    planner_calls = [call for call in gateway.calls if call['purpose'] == 'table']
    assert len(dispatches) == len(planner_calls)
    assert [row['planner_round'] for row in dispatches] == list(range(1, len(planner_calls) + 1))
    assert all(row['method'] == '__call__' and not row['reasoning_method_available']
               and row['dispatch_attempted'] and row['purpose'] == 'table'
               and row['settings_authority'] == 'gateway_request_settings' for row in dispatches)
    efforts = [row['requested_reasoning_effort'] for row in dispatches]
    assert efforts == [call['payload']['planning_progress']['requested_planner_reasoning_effort']
                       for call in planner_calls]
    escalations = [event['record'] for event in history if event['kind'] == 'planner_reasoning_escalated']
    assert len(escalations) == int('high' in efforts)
    if escalations:
        assert escalations[0]['requested_effort'] == 'high'
        assert escalations[0]['consecutive_no_progress'] == 1
        assert not escalations[0]['review_settings_changed']
        first_high = efforts.index('high')
        assert efforts[first_high:] == ['high'] * (len(efforts) - first_high)


def test_received_rejection_survives_next_unknown_post_without_retry(sources):
    before = copy.deepcopy(sources)
    events = []
    unknown = ModelError('Response outcome unknown', 'timeout')
    gateway = prepared(rejection(), unknown)
    with pytest.raises(ModelError) as caught:
        answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway, audit=events.append)
    assert caught.value is unknown
    assert len(gateway.calls) == 5
    assert [c['purpose'] for c in gateway.calls][-2:] == ['verify', 'table']
    last = events[-1]
    assert last['audit_sequence'] == len(events) == len(last['audit_history'])
    assert last['candidate_audit_count'] == last['received_review_count'] == 1
    assert last['candidate_attempts'][0]['round'] == last['received_reviews'][0]['round'] == 3
    assert last['candidate_attempts'][0]['review']['supported'] is False
    assert last['received_reviews'][0]['review']['claims'] == [{'id': 'C1', 'supported': False}]
    assert last['received_reviews'][0]['publication_status'] == 'audit_only'
    persisted = persisted_result_history(last['audit_history'])
    assert [v['kind'] for v in persisted] == ['calculation_mechanically_executed', 'independent_review_received', 'candidate_rejected']
    assert_planner_dispatch_history(last['audit_history'], gateway)
    assert persisted[-1]['sequence'] < last['audit_history'][-1]['sequence']
    assert last['audit_history'][-1]['kind'] == 'planner_gateway_dispatch'
    assert not {'claims', 'text', 'evidence'}.intersection(last)
    manifest = last['evidence_manifest']
    assert manifest['audit_only'] is True
    assert manifest['publication_status'] == 'not_published_or_verified'
    actual = {entry['id']: entry for entry in gateway.calls[-1]['payload']['evidence']}
    assert manifest['registry_count'] == len(manifest['entries']) == len(actual)
    assert manifest['omitted_entries'] == 0
    for entry in manifest['entries']:
        original = actual[entry['id']]
        for field in ('source_id', 'sha256', 'parser_version', 'page', 'start', 'end'):
            assert entry[field] == original.get(field)
        if isinstance(original.get('quote'), str):
            assert entry['quote_sha256'] == hashlib.sha256(original['quote'].encode()).hexdigest()
        assert not {'quote', 'query', 'text', 'image_url'}.intersection(entry)
    assert not last['candidate_history_truncated'] and not last['review_history_truncated']
    assert sources == before


def test_cancellation_after_received_candidate_does_not_start_next_post(sources):
    events, state = [], {'cancelled': False}
    def persist(event):
        events.append(event)
        if event['candidate_audit_count']:
            state['cancelled'] = True
    gateway = prepared(rejection())
    with pytest.raises(InterruptedError):
        answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway,
                            cancelled=lambda: state['cancelled'], audit=persist)
    assert len(gateway.calls) == 4
    assert events[-1]['candidate_audit_count'] == events[-1]['received_review_count'] == 1


def test_review_that_arrives_during_cancellation_is_audit_only(sources):
    events, state = [], {'cancelled': False}
    def late_received_review():
        state['cancelled'] = True
        return rejection()
    gateway = prepared(late_received_review)
    with pytest.raises(InterruptedError):
        answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway,
                            cancelled=lambda: state['cancelled'], audit=events.append)
    assert len(gateway.calls) == 4
    persisted = persisted_result_history(events[-1]['audit_history'])
    assert [row['kind'] for row in persisted] == ['calculation_mechanically_executed', 'independent_review_received']
    assert_planner_dispatch_history(events[-1]['audit_history'], gateway)
    assert events[-1]['audit_history'][-1]['kind'] == 'independent_review_received'
    assert events[0]['candidate_attempts'] == []
    assert events[0]['candidate_audit_count'] == 0
    assert events[-1]['received_review_count'] == 1
    assert events[-1]['received_reviews'][0]['validation_status'] == 'independent_review_received'
    assert events[-1]['received_reviews'][0]['review']['supported'] is False


def test_normal_result_unchanged_and_callbacks_cannot_mutate_live_state(sources):
    ordinary = answer_cross_source(sources, '林禾的优惠金额是多少元？', [], prepared(approved()))
    events = []
    def hostile_callback(event):
        events.append(copy.deepcopy(event))
        if not event['received_reviews']:
            for row in event['audit_history']:
                if row['kind'] == 'calculation_mechanically_executed':
                    row['record']['semantic_status']='forged'
            return
        event['received_reviews'][0]['claims'][0]['text'] = '被外部回调改写'
        event['received_reviews'][0]['review']['supported'] = False
        event['received_reviews'].clear()
    gateway = prepared(approved())
    actual = answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway, audit=hostile_callback)
    assert actual == ordinary
    assert actual[0] == 'completed' and len(gateway.calls) == 4
    assert actual[1]['text'] == '优惠金额为6000.00元。'
    assert actual[1]['diagnostics']['received_reviews'][0]['review']['supported'] is True
    assert actual[1]['diagnostics']['received_reviews'][0]['publication_status'] == 'audit_only'
    assert actual[1]['diagnostics']['audit_history'] == events[-1]['audit_history']
    assert events[0]['candidate_attempts'] == []


def test_received_review_projects_only_bounded_verdicts_and_candidate_references(sources):
    secret = 'super-secret-provider-value'
    response = rejection(reason='api_key=' + secret, arbitrary={'secret': secret},
                         table_relations=[{'id': 'C1', 'supported': False, 'secret': secret},
                                          {'id': secret, 'supported': True}],
                         chart_relations=[{'id': 'C1', 'supported': False, 'nested': {'token': secret}}])
    events = []
    gateway = prepared(response, ModelError('unknown', 'transport_failed'))
    with pytest.raises(ModelError):
        answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway, audit=events.append)
    encoded = json.dumps(events[-1], ensure_ascii=False)
    assert secret not in encoded and 'arbitrary' not in encoded
    assert len(encoded) < 25000
    review = events[-1]['received_reviews'][0]['review']
    assert review['table_relations'] == review['chart_relations'] == [{'id': 'C1', 'supported': False}]
    assert response['arbitrary']['secret'] == secret


def test_local_candidate_failure_is_persisted_before_any_review_post(sources):
    events = []
    raw = completed('99999元。api_key=very-secret-value /Users/private-user/secret.txt ' + '长文本' * 2000)
    raw['claims'][0]['evidence_ids'] = ['E404']
    gateway = Script(actions(sql(), search()), raw, ModelError('unknown', 'timeout'))
    with pytest.raises(ModelError):
        answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway, audit=events.append)
    assert len(gateway.calls) == 3
    record = events[-1]['candidate_attempts'][0]
    assert record['truncated'] and record['redacted']
    assert record['claims'][0]['unresolved_evidence_ids'] == ['E404']
    assert record['claims'][0]['evidence_ids'] == []
    assert events[-1]['received_reviews'] == []
    encoded = json.dumps(events[-1], ensure_ascii=False)
    assert 'very-secret-value' not in encoded and '/Users/' not in encoded
    assert len(json.dumps(record, ensure_ascii=False)) <= 3980


def test_negative_review_is_audited_without_changing_rejection(sources):
    events = []
    negative = {'status': 'insufficient_evidence', 'claims': [
        {'text': '资料只说明优惠比例，不能确定客户年龄。', 'evidence_ids': ['E1']}]}
    gateway = Script(actions(search()), negative,
                     {'negative_supported': False, 'claims': [{'id': 'C1', 'supported': False}]})
    status, result = answer_cross_source(sources, '客户年龄是多少？', [], gateway, audit=events.append)
    assert status == 'failed' and result['claims'] == []
    assert len(gateway.calls) == 3
    assert events[-1]['received_reviews'][0]['review']['negative_supported'] is False
    assert result['diagnostics']['received_review_count'] == 1


def test_clarification_review_is_audited_without_publishing_facts(sources):
    events = []
    clarification = {'status': 'needs_clarification', 'clarification': {
        'question': '选择哪位客户？', 'field': 'id', 'options': ['林禾', '陈安']}}
    gateway = Script(actions(sql(sql='SELECT id,customer FROM orders', parameters=[])),
                     clarification, {'clarification_supported': True, 'question_answerable': True, 'requested_information_explicit': True, 'options_supported': True})
    status, result = answer_cross_source(sources, '客户金额', [], gateway, audit=events.append)
    assert status == 'needs_clarification' and result['claims'] == []
    assert events[-1]['received_reviews'][0]['review']['clarification_supported'] is True
    assert events[-1]['received_reviews'][0]['claims'] == []
    assert len(gateway.calls) == 3


def test_local_history_keeps_first_rejection_when_prompt_feedback_rolls_over(sources):
    events = []
    invalid = completed('优惠金额为99999元。')
    gateway = Script(actions(sql(), search()), actions(calculation()),
                     invalid, completed(), rejection(),
                     actions(sql('S4', parameters=[2])),
                     invalid, completed(), rejection())
    status, result = answer_cross_source(sources, '林禾的优惠金额是多少元？', [], gateway, audit=events.append)
    assert status == 'failed' and result['claims'] == []
    diagnostics = result['diagnostics']
    assert diagnostics['candidate_audit_count'] == 4
    assert len(diagnostics['candidate_attempts']) == 3
    assert diagnostics['candidate_history_truncated'] is True
    history = diagnostics['audit_history']
    persisted = persisted_result_history(history)
    assert len(persisted) == 7
    assert [row['kind'] for row in persisted] == ['calculation_mechanically_executed',
        'candidate_rejected', 'independent_review_received', 'candidate_rejected',
        'candidate_rejected', 'independent_review_received', 'candidate_rejected']
    assert persisted[1]['record']['stage'] == 'candidate_validation'
    assert persisted[1]['record']['round'] == 3
    assert_planner_dispatch_history(history, gateway)
    assert history == events[-1]['audit_history']
    assert all(len(json.dumps(v['record'], ensure_ascii=False)) <= 3980 for v in history)
    # Full local history never becomes a new planner payload or feedback source.
    assert all('audit_history' not in call['payload'] for call in gateway.calls)
    assert len(gateway.calls) == 9
