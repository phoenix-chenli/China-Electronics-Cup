"""Offline protocol recovery contracts: fake model responses, real local tools.

Synthetic fixtures are explicit; these tests make no remote model/OCR calls.
"""
import copy
import json

import pytest

from backend import cross_source_engine as engine
from backend.model_gateway import ModelError
from test_cross_source_engine import sources, actions, search, sql, approved
from test_chart_integration import chart_source, make_binding, CHART_QUESTION, POINT_TEXT


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, purpose):
        raw = messages[-1]['content']
        payload = json.loads(raw[0]['text'] if isinstance(raw, list) else raw)
        self.calls.append({'purpose': purpose, 'payload': copy.deepcopy(payload)})
        assert self.responses, 'Unexpected extra model request'
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value(payload, purpose) if callable(value) else copy.deepcopy(value)


def candidate(*, text='林禾适用优惠规则。', evidence_id='E1', **updates):
    return {'claims': [{'text': text, 'evidence_ids': [evidence_id], 'supported': True}],
            'supported': True, **updates}


def test_missing_status_is_only_candidate_and_separate_verifier_is_required(sources):
    gateway = Scripted(actions(search()), candidate(), approved())
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed'
    assert [call['purpose'] for call in gateway.calls] == ['table', 'table', 'verify']
    assert result['verification']['semantic_review'] is True
    assert result['diagnostics']['protocol_recoveries'][0]['kind'] == 'missing_status'
    assert 'supported' not in result['claims'][0]


def test_self_approved_missing_status_cannot_override_failed_verifier(sources):
    refused = approved(supported=False, answer_complete=False,
                       claims=[{'id': 'C1', 'supported': False}])
    gateway = Scripted(actions(search()), candidate(), refused, candidate(), refused)
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status != 'completed' and result['claims'] == []
    assert [call['purpose'] for call in gateway.calls].count('verify') == 2


def test_actions_take_precedence_over_statusless_claims(sources):
    gateway = Scripted({**candidate(text='不可信的提前结论。'), **actions(search())}, candidate(), approved())
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed'
    assert result['text'] == '林禾适用优惠规则。'
    assert len(result['steps']) == 1
    assert [call['purpose'] for call in gateway.calls] == ['table', 'table', 'verify']


@pytest.mark.parametrize('bad_reply', [[], {}, {'status': None, 'claims': [{'text': '不能猜', 'evidence_ids': []}]},
                                     {'status': 'approved', 'supported': True}, {'actions': 'search'}])
def test_received_invalid_protocol_gets_explicit_bounded_replanning(sources, bad_reply):
    gateway = Scripted(bad_reply, actions(search()), candidate(), approved())
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed'
    feedback = gateway.calls[1]['payload']['tool_errors'][-1]
    assert feedback['kind'] == 'protocol'
    assert 'actions' in feedback['message']
    assert result['verification']['tool_actions'] == 1
    assert result['verification']['planning_progress']['recovery_rounds'] == 1


@pytest.mark.parametrize('failure', [ModelError('unknown remote outcome'), TimeoutError('unknown timeout'),
                                   InterruptedError('cancelled')])
@pytest.mark.parametrize('during_verify', [False, True])
def test_transport_or_cancellation_is_never_replanned_or_resent(sources, failure, during_verify):
    sequence = [actions(search()), candidate(), failure] if during_verify else [failure]
    gateway = Scripted(*sequence)
    with pytest.raises(type(failure), match=str(failure)):
        engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert len(gateway.calls) == (3 if during_verify else 1)


@pytest.mark.parametrize('same_batch', [False, True])
def test_identical_sql_id_reuses_output_without_query_or_budget_charge(sources, monkeypatch, same_batch):
    original = engine.execute_sql
    executions = []

    def execute(*args, **kwargs):
        executions.append(copy.deepcopy(args[1:3]))
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, 'execute_sql', execute)
    monkeypatch.setattr(engine, 'MAX_ACTIONS', 1)
    tool = sql()
    sequence = [actions(*([tool] * 10))] if same_batch else [actions(tool), actions(copy.deepcopy(tool))]
    gateway = Scripted(*sequence, candidate(text='客户为林禾。'), approved())
    status, result = engine.answer_cross_source(sources, '该订单客户是谁？', [], gateway)
    assert status == 'completed' and len(executions) == 1
    assert len(result['steps']) == len(result['evidence']) == 1
    assert result['evidence'][0]['query_result']['rows'] == [['林禾', '12']]
    assert result['verification']['tool_actions'] == 1
    assert gateway.calls[-2]['payload']['remaining_actions'] == 0
    assert any(row['kind'] == 'readonly_reused' for row in result['diagnostics']['protocol_recoveries'])


def test_same_id_changed_sql_is_rejected_without_replacing_original_output(sources, monkeypatch):
    original = engine.execute_sql
    executions = []

    def execute(*args, **kwargs):
        executions.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, 'execute_sql', execute)
    gateway = Scripted(actions(sql()), actions(sql(parameters=[2])), candidate(text='客户为林禾。'), approved())
    status, result = engine.answer_cross_source(sources, '该订单客户是谁？', [], gateway)
    assert status == 'completed' and executions == [[1]]
    assert len(result['steps']) == 1 and len(result['evidence']) == 1
    assert '动作内容不同' in gateway.calls[2]['payload']['tool_errors'][-1]['message']
    assert result['evidence'][0]['query_result']['rows'] == [['林禾', '12']]


def test_same_id_reused_output_remains_available_to_dependent_query(sources, monkeypatch):
    original = engine.execute_sql
    executions = []

    def execute(*args, **kwargs):
        executions.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, 'execute_sql', execute)
    dependent = sql('S2', sql='SELECT amount FROM orders WHERE customer=?',
                    parameters=[{'from_step': 'S1', 'row': 0, 'column': 'customer'}], depends_on=['S1'])
    gateway = Scripted(actions(sql()), actions(sql(), dependent), candidate(text='客户为林禾。'), approved())
    status, result = engine.answer_cross_source(sources, '该订单客户是谁？', [], gateway)
    assert status == 'completed' and executions == [[1], ['林禾']]
    assert result['steps'][1]['query']['parameter_bindings'][0]['resolved_value'] == '林禾'
    assert result['verification']['tool_actions'] == 2


def test_repeated_image_id_does_not_reload_pixels_or_rerun_chart_ocr(chart_source, monkeypatch):
    from backend import visual_context
    source = chart_source()
    original = visual_context.page_content
    loads, observations = [], []

    def load(*args):
        loads.append(args[1])
        return original(*args)

    def observe(*args, **kwargs):
        observations.append(args[1])
        return 0.0  # Explicit fake OCR hook; native synthetic labels already exist.

    monkeypatch.setattr(visual_context, 'page_content', load)
    monkeypatch.setattr(engine, 'observe_chart_page', observe)
    image = {'id': 'I1', 'kind': 'read_image', 'source_id': source['id'], 'page': 1, 'depends_on': []}

    def chart_candidate(payload, purpose):
        record = payload['evidence'][0]
        return candidate(text=POINT_TEXT, chart_relations=[{'id': 'C1', 'supported': True}]) | {
            'claims': [{'text': POINT_TEXT, 'evidence_ids': ['E1'], 'supported': True,
                        'chart_bindings': [make_binding(record['chart_context'], POINT_TEXT, evidence_id='E1')]}]}

    gateway = Scripted(actions(image), actions(image), chart_candidate,
                       approved(visual_supported=True, chart_relations=[{'id': 'C1', 'supported': True}]))
    status, result = engine.answer_cross_source([source], CHART_QUESTION, [], gateway)
    assert status == 'completed' and loads == observations == [1]
    assert result['verification']['tool_actions'] == 1
    assert len(result['steps']) == len(result['evidence']) == 1
    assert gateway.calls[-1]['purpose'] == 'verify'


def test_continued_new_evidence_passes_eight_rounds_without_spending_recovery_budget(sources):
    sources[1]['pages'] = [{'number': i, 'text': f'条目{i}包含已核验前的原文，末项适用规则。'} for i in range(1, 10)]
    sequence = []
    for i in range(1, 10):
        sequence.extend([{'supported': True}, actions(search(f'S{i}', page=i))])
    gateway = Scripted(*sequence, candidate(text='末项适用规则。', evidence_id='E9'), approved())
    status, result = engine.answer_cross_source(sources, '末项是否适用规则？', [], gateway)
    assert status == 'completed' and len(gateway.calls) == 20
    assert result['verification']['tool_actions'] == 9
    assert result['verification']['planner_rounds'] == 19
    assert result['verification']['planning_progress']['recovery_rounds'] == 9
    assert len(result['evidence']) == 9


@pytest.mark.parametrize('kind', ['invalid_protocol', 'same_id', 'new_id_same_evidence'])
def test_three_consecutive_rounds_without_progress_stop_without_fabricated_answer(sources, monkeypatch, kind):
    original = engine._retrieve
    retrievals = []

    def retrieve(*args, **kwargs):
        retrievals.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, '_retrieve', retrieve)
    sequence = ([{}, {}, {}] if kind == 'invalid_protocol' else
                [actions(search('S1'))] + [actions(search('S1' if kind == 'same_id' else f'S{i}')) for i in range(2, 5)])
    gateway = Scripted(*sequence)
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'insufficient_evidence' and result['claims'] == []
    assert result['verification']['reason'] == 'no_progress'
    assert result['verification']['planning_progress']['consecutive_no_progress'] == 3
    assert len(gateway.calls) == (3 if kind == 'invalid_protocol' else 4)
    assert len(retrievals) == {'invalid_protocol': 0, 'same_id': 1, 'new_id_same_evidence': 1}[kind]


def test_actual_action_budget_is_still_enforced(sources):
    from backend.reasoning_tools import ReasoningLimits
    gateway = Scripted(actions(*[{**search(f'S{i}'), 'query': f'规则{i}'} for i in range(11)]))
    status, result = engine.answer_cross_source(sources, '规则是什么？', [], gateway, limits=ReasoningLimits(max_actions=10))
    assert status == 'insufficient_evidence' and result['verification']['reason'] == 'budget'
    assert len(gateway.calls) == 1 and not result['claims']


def test_ninth_attempt_then_same_batch_repeated_failure_never_exceeds_budget(sources, monkeypatch):
    original = engine.execute_sql
    executions = []

    def execute(*args, **kwargs):
        executions.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, 'execute_sql', execute)
    bad = sql('S10', sql='SELECT missing_column FROM orders', parameters=[])
    # Distinct executed queries are needed here: changing only a step ID now
    # reuses the same SQL fingerprint and correctly consumes one action.
    queries = [sql(f'S{i}', sql='SELECT customer,amount FROM orders WHERE id=? LIMIT ?',
                   parameters=[1, i]) for i in range(1, 10)]
    gateway = Scripted(actions(*queries),
                       actions(bad, bad, bad, bad), actions(bad))
    from backend.reasoning_tools import ReasoningLimits
    status, result = engine.answer_cross_source(sources, '该订单客户是谁？', [], gateway, limits=ReasoningLimits(max_actions=10))
    assert status == 'insufficient_evidence' and result['verification']['reason'] == 'budget'
    assert result['verification']['tool_actions'] == len(executions) == 10
    assert executions.count(bad['sql']) == 1  # The failed batch stops immediately.
    assert len(result['steps']) == 9 and not result['claims']


@pytest.mark.parametrize('code,bad_response', [
    ('schema_failed', {'choices': [{'finish_reason': 'stop', 'message': {'content': '[]'}}]}),
    ('content_json_failed', {'choices': [{'finish_reason': 'stop', 'message': {'content': 'PRIVATE_RAW_NOT_JSON'}}]}),
    ('output_truncated', {'choices': [{'finish_reason': 'length', 'message': {'content': '{"claims":['}}]}),
])
@pytest.mark.parametrize('during_verify', [False, True])
def test_real_gateway_received_content_errors_replan_with_feedback(sources, monkeypatch, code, bad_response, during_verify):
    from backend.model_gateway import ModelGateway

    def response(value):
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value, ensure_ascii=False)}}]}

    sequence = ([response(actions(search())), response(candidate()), bad_response,
                 response(candidate()), response(approved())] if during_verify else
                [bad_response, response(actions(search())), response(candidate()), response(approved())])
    requests = []

    async def request(self, body):
        requests.append(copy.deepcopy(body))
        assert sequence, 'Unexpected extra request'
        return sequence.pop(0)

    monkeypatch.setattr(ModelGateway, '_request', request)
    gateway = ModelGateway({'models': {'table': 'explicit-test', 'document': 'explicit-test', 'verify': 'explicit-test'}})
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed' and len(requests) == (5 if during_verify else 4)
    failed_index, feedback_index = (2, 3) if during_verify else (0, 1)
    assert gateway.attempts[failed_index]['status'] == code
    assert requests[failed_index]['messages'] != requests[feedback_index]['messages']
    payload = json.loads(requests[feedback_index]['messages'][-1]['content'])
    error = payload['tool_errors'][-1]
    assert error['kind'] == 'protocol' and error['response_code'] == code
    assert 'PRIVATE_RAW_NOT_JSON' not in json.dumps(payload, ensure_ascii=False)
    assert result['verification']['semantic_review'] is True
    assert result['verification']['tool_actions'] == 1


@pytest.mark.parametrize('code', ['transport_failed', 'response_json_failed', 'timeout', 'http_failed', 'response_too_large'])
@pytest.mark.parametrize('during_verify', [False, True])
def test_gateway_unknown_or_transport_failure_codes_are_not_replanned(sources, code, during_verify):
    failure = ModelError('unknown remote outcome', code)
    gateway = Scripted(*([actions(search()), candidate(), failure] if during_verify else [failure]))
    with pytest.raises(ModelError) as caught:
        engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert caught.value is failure
    assert len(gateway.calls) == (3 if during_verify else 1)


def test_repeated_received_content_errors_stop_after_three_no_progress_rounds(sources):
    gateway = Scripted(*[ModelError('do not expose raw provider text', 'content_json_failed') for _ in range(3)])
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'insufficient_evidence' and result['verification']['reason'] == 'no_progress'
    assert len(gateway.calls) == 3 and result['claims'] == []
    assert result['verification']['tool_actions'] == 0
    assert 'raw provider' not in json.dumps(result)
