"""One existing dispatch may stay default; no new call or weaker validation."""
import copy

from backend import cross_source_engine as engine
from backend.reasoning_tools import ReasoningLimits
from test_cross_source_engine import sources, actions, search, approved
from test_cross_tool_resources import candidate
from test_cross_planner_state import ReasoningScripted


def test_one_precise_numeric_repair_uses_default_then_still_requires_review(sources):
    gateway = ReasoningScripted(actions(search()), candidate('林禾的优惠比例为7%。'),
        candidate('林禾的优惠比例为5%。'), approved())
    audit = []
    status, result = engine.answer_cross_source(sources, '林禾的优惠比例是多少？', [], gateway, audit=audit.append)
    assert status == 'completed'
    assert gateway.methods == [('table', '__call__', None)] * 3 + [('verify', '__call__', None)]
    events = audit[-1]['audit_history']
    attempts = [e for e in events if e['kind'] == 'planner_default_mechanical_repair']
    assert len(attempts) == 1 and attempts[0]['record']['no_progress_counter_reset'] is False
    assert result['verification']['planning_progress']['default_mechanical_repair_used'] is True
    assert result['claims'][0]['text'] == '林禾的优惠比例为5%。'


def test_repeated_precise_error_gets_only_one_default_then_high_and_stops(sources):
    invalid = candidate('林禾的优惠比例为7%。')
    gateway = ReasoningScripted(actions(search()), *[copy.deepcopy(invalid) for _ in range(3)])
    status, result = engine.answer_cross_source(sources, '林禾的优惠比例是多少？', [], gateway)
    assert status == 'insufficient_evidence' and result['claims'] == []
    assert len(gateway.methods) == 4
    assert gateway.methods[:3] == [('table', '__call__', None)] * 3
    assert gateway.methods[-1] == ('table', 'call_with_reasoning', 'high')
    assert result['verification']['planning_progress']['default_mechanical_repair_used'] is True


def test_explicit_one_failure_limit_still_stops_before_repair_request(sources):
    gateway = ReasoningScripted(actions(search()), candidate('林禾的优惠比例为7%。'))
    status, result = engine.answer_cross_source(sources, '林禾的优惠比例是多少？', [], gateway,
        limits=ReasoningLimits(max_no_progress_rounds=1))
    assert status == 'insufficient_evidence' and len(gateway.methods) == 2
    assert result['verification']['planning_progress']['default_mechanical_repair_used'] is False


def test_only_current_complete_located_feedback_qualifies():
    feedback = {'scope': 'rejected_candidate_occurrences_not_evidence_or_semantic_approval',
        'unverified_occurrences': [{'start': 1, 'end': 2, 'text': '7'}], 'unlocated_tokens': [],
        'unbound_numeric_text_truncated': False, 'unbound_numeric_text_redacted': False, 'omitted_occurrences': 0}
    detail = {'claim_failures': [{'numeric_occurrence_feedback': feedback}]}
    assert engine._precise_mechanical_repair_reason(detail, None) == 'located_unbound_numeric_occurrences'
    for field, value in [('unlocated_tokens', ['8']), ('unbound_numeric_text_truncated', True),
                         ('unbound_numeric_text_redacted', True), ('omitted_occurrences', 1)]:
        changed = copy.deepcopy(detail); changed['claim_failures'][0]['numeric_occurrence_feedback'][field] = value
        assert engine._precise_mechanical_repair_reason(changed, None) is None
    stale = {'binding_diagnostics': [{'source_label_contract': {
        'origin': 'mechanically_checked_current_selected_source_cell', 'missing_column_labels': ['Average']}}]}
    assert engine._precise_mechanical_repair_reason({}, stale) is None
    assert engine._precise_mechanical_repair_reason({'kind': 'table_binding'}, stale) == 'source_cell_literal_labels'


def test_precise_opportunity_after_real_progress_does_not_keep_stale_high(sources):
    gateway = ReasoningScripted({}, actions(search()), candidate('林禾的优惠比例为7%。'),
        candidate('林禾的优惠比例为5%。'), approved())
    status, result = engine.answer_cross_source(sources, '林禾的优惠比例是多少？', [], gateway)
    assert status == 'completed'
    assert gateway.methods == [('table', '__call__', None), ('table', 'call_with_reasoning', 'high'),
                               ('table', '__call__', None), ('table', '__call__', None), ('verify', '__call__', None)]
    assert result['verification']['planning_progress']['default_mechanical_repair_used'] is True
