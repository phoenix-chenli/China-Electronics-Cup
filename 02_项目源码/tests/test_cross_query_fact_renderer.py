"""Scripted controller integration. No real model or oracle is called."""
import copy
import json
import sqlite3

import pytest

from backend import candidate_audit
from backend.cross_source_engine import answer_cross_source, _claims, CrossSourceError
from backend.model_gateway import ModelError
from backend.parsers import sqlite_tables
from backend.reasoning_tools import ReasoningLimits
from test_cross_business_repair import source, sql, calculation as calc


def fact(record, column='quantity'):
    context = record['query_result_context']; cells = context['rows'][0]['cells']
    selected = next(c for c in cells if c['column_label'] == column)
    result = {'evidence_id': record['id'], 'context_id': context['context_id'],
              'cell_id': selected['cell_id'],
              'entity_cells': [c['cell_id'] for c in cells if c['column_label'] in ('period','object')]}
    unit_column = {'quantity': 'quantity_unit', 'price': 'price_unit'}.get(column)
    unit = next((c for c in cells if c['column_label'] == unit_column), None)
    if unit: result['unit_cell_id'] = unit['cell_id']
    return {'query_fact': result}


class Gateway:
    def __init__(self, values): self.values = list(values); self.calls = []
    def __call__(self, messages, role):
        payload = json.loads(messages[-1]['content']); self.calls.append((role, payload, messages))
        assert self.values, 'Unexpected extra call'
        value = self.values.pop(0)
        if isinstance(value, BaseException): raise value
        return value(payload) if callable(value) else copy.deepcopy(value)


def candidate(payload, columns=('quantity',)):
    record = next(e for e in payload['evidence'] if e.get('query_result_context'))
    return {'status': 'completed', 'claims': [fact(record, c) for c in columns]}


def review(payload, *, query=True, complete=True, operation=True):
    claims = payload['candidate_claims']; ids = [c['id'] for c in claims]
    value = {'supported': True, 'answer_complete': complete, 'conflict': False,
             'claims': [{'id': c, 'supported': True} for c in ids],
             'coverage': [{'question_id': part['id'], 'status': 'answered', 'claim_ids': ids}
                          for part in payload['question_parts']]}
    if query is not None:
        value['query_relations'] = [{'id': c, 'supported': query} for c in payload['query_relation_claim_ids']]
    if payload.get('user_operation_calculation_ids'):
        value['operation_relations'] = [{'id': c, 'intent_supported': True,
            'parameter_roles_supported': True, 'scope_supported': True, 'pricing_supported': operation}
            for c in payload['user_operation_calculation_ids']]
    return value


@pytest.mark.parametrize('verdict', [True, False, None, 1])
def test_actual_query_fact_must_pass_independent_relations(source, verdict):
    g = Gateway([sql(), candidate, lambda p: review(p, query=verdict)])
    status, result = answer_cross_source([source], '查全站用电量', [], g,
        limits=ReasoningLimits(max_no_progress_rounds=1))
    assert (status == 'completed') is (verdict is True)
    if verdict is not True: assert result['claims'] == []
    else:
        c = result['claims'][0]
        assert c['query_bindings'][0]['statement'] == c['text']
        assert c['query_bindings'][0]['localization_scope'] == 'query_output_not_original_cell'
    events = [e for e in result['diagnostics']['audit_history'] if e['kind'] == 'query_fact_rendered']
    assert len(events) == 1 and events[0]['record']['publication_status'] == 'not_adopted'
    assert events[0]['record']['source_binding']['sha256'] == source['sha256']
    assert events[0]['record']['claims'][0]['query_fact']['cell_id']


def test_valid_selection_is_not_completed_when_coverage_unknown(source):
    def missing(p):
        r = review(p); r.pop('coverage'); return r
    g = Gateway([sql(), candidate, missing])
    status, result = answer_cross_source([source], '查全站用电量', [], g)
    assert status == 'insufficient_evidence'
    assert result['completeness']['complete'] is not True
    assert result['verification']['reason'] == 'coverage_unverified'


def test_expand_before_retaining_old_fact_and_recheck_both(source):
    def partial(p):
        r = review(p, complete=False)
        r['coverage'] = [{'question_id': 'Q1', 'status': 'evidence_available', 'evidence_ids': ['E1']}]
        return r
    g = Gateway([sql(), candidate, partial,
        lambda p: candidate(p, ('price',)), lambda p: review(p)])
    status, result = answer_cross_source([source], '说明全站用电量和电价', [], g)
    assert status == 'completed'
    assert [c['query_bindings'][0]['column_label'] for c in result['claims']] == ['quantity','price']
    assert len(g.calls[-1][1]['candidate_claims']) == 2
    assert result['completion_repair']['resolved'] is True


def test_query_fact_negative_does_not_prove_nonexistence(source):
    def negative(p): return {**candidate(p), 'status': 'insufficient_evidence', 'reason': 'missing_evidence'}
    g = Gateway([sql(), negative, negative, negative])
    status, result = answer_cross_source([source], '是不是完全没有用电量', [], g)
    assert status != 'completed' and result['claims'] == []
    assert not any(role == 'verify' for role, _, _ in g.calls)
    assert '不能自动证明资料缺失' in result['diagnostics']['tool_errors'][-1]['message']


def test_invalid_then_valid_choices_are_protocol_progress_without_extra_action(source):
    def invalid(p):
        c = candidate(p); c['claims'][0]['query_fact']['cell_id'] = 'fake'; return c
    g = Gateway([sql(), invalid, candidate, review])
    status, result = answer_cross_source([source], '查全站用电量', [], g)
    assert status == 'completed'
    assert result['verification']['tool_actions'] == 1
    assert result['verification']['planning_progress']['mechanical_repair_advances'] > 0
    receipt = result['diagnostics']['candidate_attempts'][0]
    assert receipt['claims'][0]['query_fact']['cell_id'] == 'fake'
    assert receipt['publication_status'] == 'not_adopted'


def test_serialization_budget_is_not_repaired_by_dropping_fact(source, monkeypatch):
    from backend import query_fact_renderer
    original = query_fact_renderer.expand_candidate_claims
    monkeypatch.setattr(query_fact_renderer, 'expand_candidate_claims', lambda c,e: original(c,e,max_chars=80))
    g = Gateway([sql(), candidate, candidate, candidate])
    status, result = answer_cross_source([source], '查全站用电量', [], g)
    assert status != 'completed' and not result['claims']
    assert not any(role == 'verify' for role,_,_ in g.calls)
    assert '预算' in result['diagnostics']['tool_errors'][-1]['message']


@pytest.mark.parametrize('exception', [InterruptedError('cancelled'), TimeoutError('unknown POST')])
def test_cancel_or_unknown_post_propagates_without_resend_after_render(source, exception):
    events = []
    g = Gateway([sql(), candidate, exception])
    with pytest.raises(type(exception)):
        answer_cross_source([source], '查全站用电量', [], g, audit=events.append)
    assert len(g.calls) == 3 and not g.values
    assert any(e['kind'] == 'query_fact_rendered' for e in events[-1]['audit_history'])


@pytest.mark.parametrize('operation_ok', [True, False])
def test_separate_calculation_and_query_facts_still_require_four_operation_checks(source, operation_ok):
    def both(p):
        out = candidate(p, ('quantity','price'))
        out['claims'].append({'text': '全站不含税电费为1696.50元。', 'calculation_ids': ['K1']})
        return out
    g = Gateway([sql(), calc(), both, lambda p: review(p, operation=operation_ok)])
    status, result = answer_cross_source([source], '按对应电价计算全站不含税电费并说明参数', [], g,
        limits=ReasoningLimits(max_no_progress_rounds=1))
    assert (status == 'completed') is operation_ok
    assert g.calls[-1][1]['user_operation_calculation_ids'] == ['K1']
    assert len(g.calls[-1][1]['query_relation_claim_ids']) == 2
    if operation_ok: assert result['calculations'][0]['display_value'] == '1696.50'
    else: assert not result['claims'] and not result['calculations']


def test_query_selection_projection_ignores_private_extras_and_redacts_strings():
    secret = 'SYNTHETIC_PRIVATE_DO_NOT_RETAIN'
    raw = {'claims': [{'query_fact': {'evidence_id': 'E1', 'context_id': 'ctx', 'cell_id': 'bad',
            'entity_cells': [], 'analysis': secret, 'authorization_header': secret,
            'unit_cell_id': 'api_key="' + secret + ' with spaces"'}}]}
    record = candidate_audit.capture_candidate(raw, {'E1':{}}, {}, round_number=1,
        stage='candidate_validation',reason='bad shape')
    assert secret not in json.dumps(record,ensure_ascii=False)
    q = record['claims'][0]['query_fact']
    assert q['omitted_fields_count'] == 2 and record['redacted'] is True
    assert len(json.dumps(record,ensure_ascii=False)) <= candidate_audit.MAX_ATTEMPT_CHARACTERS
