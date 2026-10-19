import copy
import hashlib
import json
from pathlib import Path

import pytest

from backend.query_result_context import (build_query_result_context, check_query_bindings,
                                         _calendar_variants, QUERY_BINDING_INSTRUCTIONS)
from test_query_result_context import evidence, bound

FIXTURE = Path(__file__).parent / 'fixtures/query-binding-original-cases.json'
RECORD = json.loads(FIXTURE.read_text())


def original(case_id, candidate_index=-1):
    record = next(c['record'] for c in RECORD['cases'] if c['id'] == case_id)
    attempt = record['result']['diagnostics']['candidate_attempts'][candidate_index]
    return record, copy.deepcopy(attempt['claims'])


def test_real_cross005_reports_exact_unit_and_missing_date_without_publishing():
    record, claims = original('cross-005')
    claim = next(c for c in claims if c['candidate_index'] == 2)
    before = copy.deepcopy(claim)
    result = check_query_bindings(claim['text'], claim['query_bindings'], record['result']['evidence'])
    assert not result['valid'] and not result['bindings']
    assert "unit='kWh'" in result['errors'][0]
    assert "unit_column='计量单位'" in result['errors'][0]
    assert '2025-07-01' in result['errors'][1] and '同一时段' in result['errors'][1]
    assert claim == before


def test_real_cross005_explicit_source_unit_and_full_period_repairs_protocol_only():
    record, claims = original('cross-005')
    claim = next(c for c in claims if c['candidate_index'] == 2)
    claim['text'] = claim['text'].replace('同一时段对象', '2025-07-01对象')
    for b in claim['query_bindings']:
        b.update(unit='kWh', unit_column='计量单位')
        b['statement'] = b['statement'].replace('同一时段对象', '2025-07-01对象')
    result = check_query_bindings(claim['text'], claim['query_bindings'], record['result']['evidence'])
    assert result['valid'], result['errors']
    assert [b['value'] for b in result['bindings']] == ['2610','180']
    assert all(b['semantic_review_required'] for b in result['bindings'])
    assert all(b['localization_scope'] == 'query_output_not_original_cell' for b in result['bindings'])


def test_real_cross008_month_display_keeps_canonical_value_and_exact_span():
    record, claims = original('cross-008')
    for claim in claims[:2]:
        before = copy.deepcopy(claim)
        result = check_query_bindings(claim['text'], claim['query_bindings'], record['result']['evidence'])
        assert result['valid'], result['errors']
        binding = result['bindings'][0]
        annotation = binding['context_annotations'][0]
        assert annotation['canonical_value'] == '2025-02'
        assert annotation['display_text'] == '2025年2月' and annotation['precision'] == 'month'
        assert claim['text'][annotation['claim_start']:annotation['claim_end']] == '2025年2月'
        assert binding['entity_values'][1]['value'] == '2025-02'
        assert claim == before
    # This does not excuse C3 lacking a unique row entity, or prove joins/semantics.
    last = claims[2]
    assert not check_query_bindings(last['text'],last['query_bindings'],record['result']['evidence'])['valid']


def test_real_cross008_unbound_period_is_not_automatically_added():
    record,claims=original('cross-008',-2)
    first=claims[0]
    result=check_query_bindings(first['text'],first['query_bindings'],record['result']['evidence'])
    assert not result['valid'] and '未由本格/实体格支持' in ' '.join(result['errors'])


def test_real_cross009_nonexact_statement_remains_rejected():
    record,claims=original('cross-009',-2)
    claim=claims[0];binding=claim['query_bindings'][2]
    assert binding['statement'] not in claim['text']
    result=check_query_bindings(claim['text'],[binding],record['result']['evidence'])
    assert not result['valid'] and '出现次数=0' in result['errors'][0]


def test_real_cross005_no_automatic_unit_rewrite_or_magnitude_repair():
    record,claims=original('cross-005');claim=next(c for c in claims if c['candidate_index']==2)
    b=claim['query_bindings'][0];b.update(unit='kW',unit_column='计量单位')
    assert not check_query_bindings(claim['text'],[b],record['result']['evidence'])['valid']
    b.update(unit='kWh');b['value']='261'
    assert not check_query_bindings(claim['text'],[b],record['result']['evidence'])['valid']


@pytest.mark.parametrize('canonical,display', [
    ('2025-02','2025年2月'), ('2025-02','2025年02月'),
    ('2024-02-29','2024年2月29日'), ('2025-07-01','2025年07月01日'),
    ('2025年2月','2025-02'), ('2024年2月29日','2024-02-29')])
def test_only_equal_calendar_precision_is_normalized(canonical,display):
    e=evidence([[canonical,'东岸',30,'次']]);b=bound(e,text=display+'东岸巡检30次。')
    r=check_query_bindings(b['statement'],[b],[e])
    assert r['valid'],r['errors']
    assert r['bindings'][0]['entity_values'][0]['value']==canonical
    assert r['bindings'][0]['context_annotations'][0]['display_text']==display


@pytest.mark.parametrize('canonical,display', [
    ('2025-02','2024年2月'), ('2025-02','2025年3月'), ('2025-02','2月'),
    ('2025-02','2025年2月1日'), ('2025-02','2025-02-01'),
    ('2025-02-01','2025年2月'), ('2025-02-01','2月1日'),
    ('2025-02-01','2025年2月2日'), ('2025-02-01','2025年2月1日08:00'),
    ('2025-02-01','2025-02-01T08:00Z'), ('2025-02-01','2025年2月1日 UTC+08:00'),
    ('2025-02','2025/02'), ('2025-02','12025年2月'), ('2025-02','2025年20月')])
def test_changed_or_inferred_calendar_precision_not_accepted(canonical,display):
    e=evidence([[canonical,'东岸',30,'次']]);b=bound(e,text=display+'东岸巡检30次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


@pytest.mark.parametrize('canonical,display', [
    ('2025-02','2025年2月一日'), ('2025-02','2025年2月初一'),
    ('2025-02','2025年2月三十一日'), ('2025-02','2025年2月二十九号'),
    ('2025-02','2025-02廿一日'), ('2025-02','2025年2月 一日'),
    ('2025-02','2025年2月九时'), ('2025-02','2025年2月每日八点'),
    ('2025-02','2025年2月上午九点'), ('2025-02-01','2025年2月1日九时'),
    ('2025-02-01','2025年2月1日 下午两点')])
def test_chinese_day_or_time_suffix_cannot_silently_expand_calendar_precision(canonical,display):
    e=evidence([[canonical,'东岸',30,'次']]);b=bound(e,text=display+'东岸巡检30次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


@pytest.mark.parametrize('suffix',['的东岸','东岸','东岸第一组','东岸工作日汇总'])
def test_ordinary_following_chinese_labels_are_not_blanket_blocked(suffix):
    e=evidence([['2025-02',suffix,30,'次']]);b=bound(e,text='2025年2月'+suffix+'巡检30次。')
    assert check_query_bindings(b['statement'],[b],[e])['valid']


@pytest.mark.parametrize('canonical',['2025-02-29','2025-13','0000-01','2024-04-31','2025-00-01'])
def test_illegal_calendar_values_do_not_gain_equivalent_forms(canonical):
    assert _calendar_variants(canonical)==[]


@pytest.mark.parametrize('direct', ['measurements.station_id', None, 'unrelated_alias'])
def test_calendar_shaped_identifier_or_unresolved_alias_not_normalized(direct):
    e=evidence()
    if direct is None:e['direct_columns'].pop('period')
    else:e['direct_columns']['period']=direct
    e['query_result_context']=build_query_result_context(e)
    b=bound(e,text='2025年2月东岸巡检30次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_calendar_display_does_not_authorize_business_year_or_unbound_extra_amount():
    e=evidence();b=bound(e,text='2025年2月东岸巡检30次，另有2025元。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']
    b=bound(e,text='2025年2月东岸巡检30次。')
    r=check_query_bindings(b['statement']+'另有2025元。',[b],[e])
    assert r['valid'] and '2025元' in r['numeric_text']


def test_calendar_alias_does_not_change_sign_row_or_unit_requirements():
    for text in ['2025年2月东岸巡检-30次。','2025年2月西岸巡检30次。','2025年2月东岸巡检30元。']:
        e=evidence();b=bound(e,text=text)
        assert not check_query_bindings(text,[b],[e])['valid']


def test_same_period_other_row_and_duplicate_entity_remain_ambiguous():
    e=evidence([['2025-02','东岸',30,'次'],['2025-02','东岸',40,'次']]);b=bound(e,text='2025年2月东岸巡检30次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_statement_feedback_does_not_auto_rewrite_candidate_or_its_source():
    e=evidence();b=bound(e);b['statement']='东岸巡检30次。';before=copy.deepcopy((e,b))
    r=check_query_bindings('东岸站巡检30次。',[b],[e])
    assert not r['valid'] and '出现次数=0' in r['errors'][0]
    assert '同步修改正文' in r['errors'][0] and (e,b)==before


def test_protocol_instructions_show_explicit_unit_and_complete_fact_clauses():
    assert 'unit_column' in QUERY_BINDING_INSTRUCTIONS and '同一时段' in QUERY_BINDING_INSTRUCTIONS
    assert '不能省年、省日或推断时区' in QUERY_BINDING_INSTRUCTIONS


def test_complete_day_followed_by_bound_numbered_site_is_not_extra_day_precision():
    e=evidence([['2025-02-01','1号站',30,'次']])
    b=bound(e,text='2025年2月1日1号站巡检30次。')
    before=copy.deepcopy((e,b))
    result=check_query_bindings(b['statement'],[b],[e])
    assert result['valid'],result['errors']
    assert [item['value'] for item in result['bindings'][0]['entity_values']]==['2025-02-01','1号站']
    assert result['bindings'][0]['context_annotations'][0]['display_text']=='2025年2月1日'
    assert (e,b)==before


def test_month_still_rejects_day_suffix_and_complete_day_still_rejects_time():
    for canonical,display in [('2025-02','2025年2月一日'),
                              ('2025-02','2025年2月初一'),
                              ('2025-02-01','2025年2月1日九时'),
                              ('2025-02-01','2025年2月1日08:00'),
                              ('2025-02-01','2025年2月1日 UTC+08:00')]:
        e=evidence([[canonical,'东岸',30,'次']])
        b=bound(e,text=display+'东岸巡检30次。')
        assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_unbound_numbered_site_cannot_borrow_day_number_authorization():
    e=evidence([['2025-02-01','1号站',30,'次']])
    b=bound(e,text='2025年2月1日1号站巡检30次。')
    b['entity_cells']=b['entity_cells'][:1]
    result=check_query_bindings(b['statement'],[b],[e])
    assert not result['valid']
    assert '未由本格/实体格支持' in ' '.join(result['errors'])
