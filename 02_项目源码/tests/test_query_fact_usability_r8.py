"""Offline protocol checks; saved R7 projections are not full raw responses."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from backend import query_fact_renderer as renderer, candidate_audit
from backend.cross_source_engine import _claims, answer_cross_source, CrossSourceError
from backend.query_result_context import build_query_result_context, check_query_bindings
from backend.reasoning_tools import ReasoningLimits
from test_query_fact_renderer import source as evidence_source, choice
from test_cross_query_fact_renderer import Gateway, candidate, review
from test_cross_business_repair import source, sql


def historical(case_id):
    cases = json.loads((Path(__file__).parent / 'fixtures/r7_query_fact_saved_projections.json').read_text())['cases']
    return next(c for c in cases if c['id'] == case_id)


def typed_projection(attempt):
    # Restore only allowed saved fields. The audit also has candidate_index and
    # empty evidence arrays which are projection metadata, not original input.
    # Projected null cannot prove whether the original text key was absent or
    # non-string. These selector probes make no claim to reconstruct that key.
    return [{'query_fact': copy.deepcopy(c['query_fact']),
             **({'text': c['text']} if isinstance(c.get('text'), str) else {})}
            for c in attempt['claims'] if c.get('query_fact')]


@pytest.mark.parametrize('case_id', ['cross-005', 'cross-008'])
def test_received_valid_cell_choice_preserves_supported_outer_text_under_current_contract(case_id):
    case = historical(case_id); before = copy.deepcopy(case)
    attempt = next(a for a in case['saved_candidate_attempts'] if '不能混入' in a['reason'])
    projected = typed_projection(attempt)
    assert projected and all('text' in c for c in projected)
    expanded = renderer.expand_candidate_claims(projected, case['evidence'])
    claims = _claims(expanded, {e['id']: e for e in case['evidence']}, {}, question=case['question'])
    assert len(claims) == len(projected)
    for raw, claim, audit in zip(projected, claims, expanded['render_audit']):
        assert claim['text'] == raw['text']
        assert claim['query_bindings'][0]['statement'] == claim['text']
        assert check_query_bindings(claim['text'], claim['query_bindings'], case['evidence'])['valid']
        assert audit['narrative_preserved'] is True
        assert 'ignored_model_text' not in audit
    assert case['status'] != 'completed' and not case['saved_claims']
    assert case == before  # This is not a new end-to-end model score.


def test_saved_one_character_wrong_id_is_still_rejected_new_explicit_alias_is_separate_choice():
    case = historical('cross-005')
    attempt = next(a for a in case['saved_candidate_attempts'] if '上下文不可用' in a['reason'])
    original = typed_projection(attempt)
    evidence = copy.deepcopy(case['evidence'])
    for e in evidence:
        if e.get('query_result_context'): e['query_result_context'] = build_query_result_context(e)
    with pytest.raises(ValueError, match='标识未精确匹配'):
        renderer.expand_candidate_claims(original, evidence)
    explicit_new = copy.deepcopy(original)
    for item in explicit_new:
        target = next(e for e in evidence if e['id'] == item['query_fact']['evidence_id'])
        item['query_fact']['context_id'] = target['query_result_context']['context_alias']
    expanded = renderer.expand_candidate_claims(explicit_new, evidence)
    for raw, new, audit in zip(original, expanded['claims'], expanded['render_audit']):
        assert len(raw['query_fact']['context_id']) == 63
        assert len(new['query_bindings'][0]['context_id']) == 64
        assert audit['context_reference_kind'] == 'alias'
        assert audit['selection']['context_id'].startswith('QC')
    receipt = candidate_audit.capture_candidate({'claims': original}, {e['id']: e for e in evidence}, {},
        round_number=1, stage='candidate_validation', reason='wrong exact id')
    assert receipt['claims'][0]['query_fact']['context_id'] == original[0]['query_fact']['context_id']


def test_ambiguity_does_not_become_approved_sum_after_ignoring_outer_text():
    case = historical('clarify-002')
    projected = typed_projection(case['saved_candidate_attempts'][0])
    # The actual old JOIN has no controller direct-column provenance. Its unit
    # selection remains invalid; the protocol must not invent a mapping.
    with pytest.raises(ValueError, match='同一真实原表'):
        renderer.expand_candidate_claims(projected, case['evidence'])
    # A separate adversarial unitless choice still cannot publish the ambiguous
    # sum. This removal is test input, never an automatic controller correction.
    projected[0]['query_fact'].pop('unit_cell_id')
    expanded = renderer.expand_candidate_claims(projected, case['evidence'])
    with pytest.raises(CrossSourceError, match='同名'):
        _claims(expanded, {e['id']: e for e in case['evidence']}, {}, question=case['question'])


def test_saved_cross007_keeps_unit_provenance_failure_after_format_is_tolerated():
    case = historical('cross-007')
    projected = typed_projection(case['saved_candidate_attempts'][0])
    with pytest.raises(ValueError, match='同一真实原表'):
        renderer.expand_candidate_claims(projected, case['evidence'])
    assert not next(e for e in case['evidence'] if e['id']=='E7')['direct_columns']


def alias_choice(e):
    selected = choice(e); selected['context_id'] = e['query_result_context']['context_alias']; return selected


@pytest.mark.parametrize('mutation', ['unknown_alias', 'different_context', 'unadvertised_alias',
    'stale_source', 'colliding_alias', 'wrong_evidence', 'fake_cell', 'wrong_row', 'unit_other_row',
    'nonunique_entity', 'unit_cross_table', 'hidden_projection', 'value_extra'])
def test_alias_never_repairs_source_cells_entities_or_units(mutation):
    e = evidence_source(); selection = alias_choice(e); evidence = [e]
    other = build_query_result_context(e)['rows'][1]['cells']
    if mutation == 'unknown_alias': selection['context_id'] += 'x'
    elif mutation == 'different_context': selection['context_id'] = 'QC' + '0' * 12
    elif mutation == 'unadvertised_alias': e['query_result_context'].pop('context_alias')
    elif mutation == 'stale_source': e['sha256'] = 'b' * 64
    elif mutation == 'colliding_alias':
        clone = copy.deepcopy(e); clone['id'] = 'E2'; clone['query_result_context']['context_id'] = 'b' * 64; evidence.append(clone)
    elif mutation == 'wrong_evidence': selection['evidence_id'] = 'E9'
    elif mutation == 'fake_cell': selection['cell_id'] = 'r1c3'
    elif mutation == 'wrong_row': selection['entity_cells'][1] = other[1]['cell_id']
    elif mutation == 'unit_other_row': selection['unit_cell_id'] = other[3]['cell_id']
    elif mutation == 'nonunique_entity': selection['entity_cells'] = selection['entity_cells'][:1]
    elif mutation == 'unit_cross_table':
        e['direct_columns']['unit'] = 'other.unit'; e['query_result_context'] = build_query_result_context(e); selection = alias_choice(e)
    elif mutation == 'hidden_projection': e['query_result_context']['rows'] = []
    elif mutation == 'value_extra': selection['value'] = 999
    with pytest.raises(ValueError): renderer.render_query_fact(selection, evidence)


def test_alias_deterministic_and_in_original_context_budget():
    e = evidence_source(); first = build_query_result_context(e)
    assert first['context_alias'] == build_query_result_context(copy.deepcopy(e))['context_alias']
    assert len(first['context_alias']) == 14
    tiny = build_query_result_context(e, max_chars=256)
    assert not tiny['usable'] and not tiny['rows']
    actual = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    bounded = build_query_result_context(e, max_chars=len(actual)-1)
    assert len(json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(',', ':'))) <= len(actual)-1
    assert bounded['projection_truncated']


@pytest.mark.parametrize('value', [False, 0, '0'])
def test_alias_preserves_types_and_does_not_invent_unit(value):
    e = evidence_source([['2025-02','1号站',value,'kWh']]); s = alias_choice(e); s.pop('unit_cell_id')
    c = renderer.render_query_fact(s, [e])['claim']
    assert type(c['query_bindings'][0]['value']) is type(value)
    assert c['query_bindings'][0]['value'] == value and 'unit' not in c['query_bindings'][0]


@pytest.mark.parametrize('value', [None, ''])
def test_alias_does_not_invent_null_or_empty_value(value):
    e = evidence_source([['2025-02','1号站',value,'kWh']])
    with pytest.raises(ValueError): renderer.render_query_fact(alias_choice(e), [e])


def test_overlong_text_is_rejected_and_received_projection_is_redacted():
    e = evidence_source(); spec = alias_choice(e)
    secret = 'DUMMY_PRIVATE_WITH_SPACES do not save'
    text = f'未经支持99999元；api_key="{secret}" endpoint="https://private.invalid/v1" /Users/person/private ' + '长正文'*3000
    response = [{'query_fact':spec, 'text':text}]; before = copy.deepcopy(response)
    with pytest.raises(ValueError,match='1800'):renderer.expand_candidate_claims(response,[e])
    record=renderer.received_audit(response,round_number=1);serialized=json.dumps(record,ensure_ascii=False)
    assert all(v not in serialized for v in [secret,'private.invalid','/Users/person'])
    assert record['response']['truncated'] and record['publication_status']=='not_adopted'
    assert response==before


@pytest.mark.parametrize('extra', [{'analysis':'HIDDEN'}, {'evidence_ids':['E999']}, {'calculation_ids':['K1']}, {'query_bindings':[]}])
def test_private_unknown_citation_nonempty_calculation_and_mixed_binding_rejected(extra):
    e = evidence_source()
    with pytest.raises(ValueError): renderer.expand_candidate_claims([{'query_fact':alias_choice(e),**extra}],[e])


def test_nested_private_text_is_rejected_not_traversed_into_audit():
    e = evidence_source(); raw = {'query_fact':alias_choice(e),'text':{'hidden_analysis':'PRIVATE_HIDDEN'}}
    with pytest.raises(ValueError, match='字符串'): renderer.expand_candidate_claims([raw],[e])
    receipt = candidate_audit.capture_candidate({'claims':[raw]},{'E1':e},{},round_number=1,stage='candidate_validation',reason='bad text')
    assert 'PRIVATE_HIDDEN' not in json.dumps(receipt)


def canonical_candidate(payload):
    out = candidate(payload)
    e = next(e for e in payload['evidence'] if e.get('query_result_context'))
    out['claims'][0]['query_fact']['context_id'] = e['query_result_context']['context_alias']
    out['claims'][0]['text'] = '2025-07-01全站用电量为2610kWh。'
    return out


@pytest.mark.parametrize('query_ok', [True,False,None,1])
def test_controller_reviews_actual_preserved_text_still_requires_query_relations(source,query_ok):
    def check(p):
        assert all('99999' not in c['text'] for c in p['candidate_claims'])
        return review(p, query=query_ok)
    g=Gateway([sql(),canonical_candidate,check])
    status,result=answer_cross_source([source],'查全站用电量',[],g,limits=ReasoningLimits(max_no_progress_rounds=1))
    assert (status=='completed') is (query_ok is True)
    assert '99999' not in result['text']
    events=[e for e in result['diagnostics']['audit_history'] if e['kind']=='query_fact_rendered']
    assert len(events)==1 and events[0]['record']['narrative_preserved'] is True
    if status=='completed':assert result['claims'][0]['text']=='2025-07-01全站用电量为2610kWh。'


def test_preserved_narrative_cannot_complete_missing_coverage(source):
    def unknown(p):
        r=review(p);r.pop('coverage');return r
    g=Gateway([sql(),canonical_candidate,unknown])
    status,result=answer_cross_source([source],'查用电量并解释与另一份资料的差异',[],g)
    assert status=='insufficient_evidence' and not result['completeness']['complete']
    assert result['verification']['reason']=='coverage_unverified'


@pytest.mark.parametrize('error',[InterruptedError('cancelled'),TimeoutError('unknown POST')])
def test_alias_rendered_audit_survives_transport_or_cancel_without_resend(source,error):
    audit=[];g=Gateway([sql(),canonical_candidate,error])
    with pytest.raises(type(error)):answer_cross_source([source],'查全站用电量',[],g,audit=audit.append)
    assert len(g.calls)==3 and not g.values
    assert any(e['kind']=='query_fact_rendered' for e in audit[-1]['audit_history'])


def test_negative_validation_now_persists_actual_bounded_redacted_candidate(source):
    rawtext='未支持99999元，api_key="DUMMY_NEGATIVE_SECRET with spaces" /Users/person/private '
    def bad(p):
        return {'status':'insufficient_evidence','claims':[{'text':rawtext,'evidence_ids':['E1'],
            'analysis':'HIDDEN_EXTRA','authorization_header':'PRIVATE_EXTRA'}]}
    audit=[];g=Gateway([sql(),bad,bad,bad])
    status,result=answer_cross_source([source],'核对用电量',[],g,audit=audit.append)
    assert status!='completed' and not result['claims']
    records=[e['record'] for e in result['diagnostics']['audit_history'] if e['kind']=='candidate_rejected']
    assert len(records)==3 and all(r['stage']=='negative_validation' for r in records)
    assert all(r['claims'][0]['evidence_ids']==['E1'] for r in records)
    assert all('99999' in r['claims'][0]['text'] for r in records)
    text=json.dumps(records,ensure_ascii=False)
    assert all(s not in text for s in ['DUMMY_NEGATIVE_SECRET','/Users/person','HIDDEN_EXTRA','PRIVATE_EXTRA'])
    assert all(len(json.dumps(r,ensure_ascii=False))<=3980 for r in records)
    assert all(r['publication_status']=='not_adopted' for r in records)
    assert not any(role=='verify' for role,_,_ in g.calls)
    assert audit[-1]['candidate_audit_count']==3
