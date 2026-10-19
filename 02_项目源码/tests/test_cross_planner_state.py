"""Controller execution state and bounded planner recovery; no model API."""
import copy

import pytest

from backend import cross_source_engine as engine
from backend.reasoning_tools import ReasoningLimits
from test_cross_source_engine import sources, actions, search, approved
from test_cross_protocol_recovery import Scripted
from test_cross_tool_resources import candidate


class ReasoningScripted(Scripted):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.methods = []

    def __call__(self, messages, purpose):
        self.methods.append((purpose, '__call__', None))
        return super().__call__(messages, purpose)

    def call_with_reasoning(self, messages, purpose, effort='high'):
        self.methods.append((purpose, 'call_with_reasoning', effort))
        return super().__call__(messages, purpose)


def image_action(identifier='Image'):
    return {'id': identifier, 'kind': 'read_image', 'source_id': 'rule', 'page': 1,
            'depends_on': ['S1']}


def image_source(sources):
    sources[1]['format'] = 'pdf'
    sources[1]['metadata']['page_unit'] = 'physical_page'


def test_original_page_state_survives_alias_without_claiming_semantic_approval(sources, monkeypatch):
    image_source(sources)
    before = copy.deepcopy(sources)
    reads = []
    def pixels(source, page):
        reads.append((source['id'], page))
        return [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    monkeypatch.setattr('backend.visual_context.page_content', pixels)
    gateway = Scripted(actions(search('S1')), actions(image_action()),
                       actions(image_action('Alias')), candidate(), approved(visual_supported=True))
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed' and reads == [('rule', 1)] and sources == before
    assert gateway.calls[0]['payload']['read_pages'] == []
    original = gateway.calls[2]['payload']['read_pages']
    assert original == [{'source_id': 'rule', 'page': 1, 'pixels_available': True,
                         'attached_to_current_planner_request': True,
                         'semantic_review_status': 'pending_candidate_review'}]
    assert gateway.calls[3]['payload']['read_pages'] == original
    for step in result['steps'][1:]:
        assert step['kind'] == 'search' and step['actual_kind'] == 'read_image'
    feedback = next(row for row in gateway.calls[3]['payload']['protocol_feedback']
                    if row['kind'] == 'image_page_reused')
    assert feedback['original_step_id'] == 'Image' and feedback['pixels_available']
    assert feedback['action_budget_charged'] == 0 and not feedback['result_reexecuted']
    assert 'requires_review' in feedback['message']
    assert gateway.calls[-1]['purpose'] == 'verify'
    assert result['verification']['tool_actions'] == 2


@pytest.mark.parametrize('failure', ['missing_pixels', 'unreadable'])
def test_failed_image_load_never_enters_read_pages(sources, monkeypatch, failure):
    image_source(sources)
    def read(*_):
        if failure == 'unreadable':
            raise OSError('unreadable synthetic original')
        return [{'type': 'text', 'text': 'OCR only'}]
    monkeypatch.setattr('backend.visual_context.page_content', read)
    # The unrelated plain paragraph may still be reviewed; no image claim is made.
    gateway = Scripted(actions(search('S1')), actions(image_action()), candidate(), approved())
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed'
    assert gateway.calls[2]['payload']['read_pages'] == []
    assert all(step['actual_kind'] != 'read_image' for step in result['steps'])


def test_replayed_image_does_not_reset_three_round_no_progress(sources, monkeypatch):
    image_source(sources)
    reads = []
    def read(*_):
        reads.append(1)
        return [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    monkeypatch.setattr('backend.visual_context.page_content', read)
    gateway = ReasoningScripted(actions(search('S1')), *[
        actions(image_action(f'Image{i}')) for i in range(4)])
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'insufficient_evidence' and result['verification']['reason'] == 'no_progress'
    assert result['verification']['planning_progress']['consecutive_no_progress'] == 3
    assert reads == [1] and result['verification']['tool_actions'] == 2
    assert gateway.methods[-2:] == [('table', 'call_with_reasoning', 'high')] * 2


def test_reasoning_recovery_returns_default_after_real_progress_and_review_stays_default(sources):
    gateway = ReasoningScripted({}, actions(search('S1')), candidate(), approved())
    audit = []
    status, result = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway, audit=audit.append)
    assert status == 'completed'
    assert gateway.methods == [('table', '__call__', None),
                               ('table', 'call_with_reasoning', 'high'),
                               ('table', '__call__', None), ('verify', '__call__', None)]
    events = audit[-1]['audit_history']
    escalation = [row['record'] for row in events if row['kind'] == 'planner_reasoning_escalated']
    assert len(escalation) == 1 and escalation[0]['consecutive_no_progress'] == 1
    restores = [row['record'] for row in events if row['kind'] == 'planner_reasoning_restored_default']
    assert len(restores) == 1 and restores[0]['advanced_dimensions'] == ['evidence']
    assert restores[0]['review_settings_changed'] is False
    dispatch = [row['record'] for row in events if row['kind'] == 'planner_gateway_dispatch']
    assert [row['method'] for row in dispatch] == ['__call__', 'call_with_reasoning', '__call__']
    assert all(row['settings_authority'] == 'gateway_request_settings' for row in dispatch)
    assert result['verification']['planning_progress']['consecutive_no_progress'] == 0


def test_productive_planner_path_keeps_default_gateway(sources):
    gateway = ReasoningScripted(actions(search('S1')), candidate(), approved())
    status, _ = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway)
    assert status == 'completed'
    assert gateway.methods == [('table', '__call__', None), ('table', '__call__', None), ('verify', '__call__', None)]


def test_recovery_without_reasoning_method_uses_ordinary_gateway_and_reports_fallback(sources):
    gateway = Scripted({}, actions(search('S1')), candidate(), approved())
    audit = []
    status, _ = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway, audit=audit.append)
    assert status == 'completed'
    dispatch = [row['record'] for row in audit[-1]['audit_history'] if row['kind'] == 'planner_gateway_dispatch']
    assert dispatch[1]['requested_reasoning_effort'] == 'high'
    assert dispatch[1]['method'] == '__call__' and not dispatch[1]['reasoning_method_available']


@pytest.mark.parametrize('limits, expected', [(None, 32), (ReasoningLimits(max_actions=1), 1)])
def test_prompt_refers_to_actual_limits_including_explicit_low_limit(sources, limits, expected):
    assert '最多10个' not in engine.PLANNER and 'tool_limits.max_actions' in engine.PLANNER
    assert '无论沿用id还是换id' in engine.PLANNER
    gateway = Scripted(actions(search('S1')), candidate(), approved())
    status, _ = engine.answer_cross_source(sources, '林禾是否适用优惠规则？', [], gateway, limits=limits)
    assert status == 'completed'
    assert gateway.calls[0]['payload']['tool_limits']['max_actions'] == expected
    assert gateway.calls[1]['payload']['remaining_actions'] == expected - 1


def test_local_context_stop_has_no_dispatch_or_reasoning_request(monkeypatch):
    monkeypatch.setattr(engine, 'MAX_CONTEXT', 1)
    gateway = ReasoningScripted()
    dispatch = []
    with pytest.raises(engine.CrossSourceError):
        engine._ask(gateway, '', {'question': 'long'}, 'table', [], lambda: False,
                    reasoning_effort='high', call_audit=dispatch.append)
    assert not dispatch and not gateway.methods
