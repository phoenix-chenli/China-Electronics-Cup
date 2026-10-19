"""Executed calendar filters only authorize their label occurrence."""
import copy
import pytest
from backend.query_time_annotations import full_year_filter,time_annotations
from backend.query_result_context import build_query_result_context,check_query_bindings
from backend.query_fact_renderer import expand_candidate_claims
from backend.cross_source_engine import _claims,CrossSourceError


def evidence(where="status=? AND day >= '2025-01-01' AND day < '2026-01-01'",parameters=None):
    item={'id':'E1','kind':'query','source_id':'s','sha256':'a'*64,'parser_version':'test',
          'query':{'sql':'SELECT SUM(amount) AS total,COUNT(*) AS count FROM orders WHERE '+where,'parameters':['完成'] if parameters is None else parameters},
          'query_result':{'columns':['total','count'],'rows':[[81000,9]],'truncated':False}}
    item['query_result_context']=build_query_result_context(item)
    return item


def candidate(item,text='按口径说明V1统计2025年已完成订单，合计81000分。'):
    context=item['query_result_context'];cell=context['rows'][0]['cells'][0]
    return {'text':text,'evidence_ids':['E1'],'query_fact':{'evidence_id':'E1','context_id':context['context_alias'],'cell_id':cell['cell_id'],'entity_cells':[]}}


def test_full_year_literal_and_parameter_ranges_are_proved_without_execution():
    item=evidence();before=copy.deepcopy(item)
    expected={'column':'day','year':2025,'start_inclusive':'2025-01-01','end_exclusive':'2026-01-01'}
    assert full_year_filter(item)==expected
    assert full_year_filter(evidence('status=? AND (day >= ? AND day < ?)',['完成','2025-01-01','2026-01-01']))==expected
    assert item==before


@pytest.mark.parametrize('where',[
    "day >= '2025-02-01' AND day < '2026-01-01'",
    "day >= '2025-01-01' AND day < '2025-12-31'",
    "day > '2025-01-01' AND day < '2026-01-01'",
    "day >= '2025-01-01' AND day <= '2026-01-01'",
    "day >= '2025-01-01' OR day < '2026-01-01'",
    "day >= '2025-01-01' AND day < '2026-01-01' AND (status='完成' OR status='退款')",
    "day >= '2025-01-01' AND day < '2026-01-01' AND day >= '2025-07-01'",
    "day >= '2025-01-01' AND day < '2026-01-01' AND strftime('%m',day)='02'",
    "day >= '2025-01-01' AND other_day < '2026-01-01'",
    "day >= '2025-01-01' AND day < '2027-01-01'",
    "day >= '2025-01-01' AND day < '2026-01-01' AND due >= '2025-01-01' AND due < '2026-01-01'",
    "day BETWEEN '2025-01-01' AND '2025-12-31'",
])
def test_partial_wrong_operator_or_ambiguous_range_cannot_prove_full_year(where):
    item=evidence(where,[])
    assert full_year_filter(item) is None
    with pytest.raises(ValueError,match='还有未由本格'):expand_candidate_claims([candidate(item)],[item])


@pytest.mark.parametrize('text',[
    '按2024年统计合计81000分。','按2025年1月统计合计81000分。',
    '截至2025年合计81000分。','设备寿命为2025年，合计81000分。',
    '按2025年上半年统计合计81000分。','并非2025年，合计81000分。',
    '按2025年以来统计合计81000分。','按2025年之后统计合计81000分。',
    '按2025年至2026年统计合计81000分。','不按2025年统计合计81000分。',
    '统计结果为2025年，合计81000分。','在样本中寿命为2025年，合计81000分。',
])
def test_only_equivalent_annual_scope_label_can_be_annotated(text):
    assert time_annotations(text,evidence())==[]


def test_statement_binding_masks_year_occurrence_only_and_retains_semantic_review():
    item=evidence();text='按2025年统计已完成订单，合计81000分。';out=expand_candidate_claims([candidate(item,text)],[item])
    claim=out['claims'][0];checked=check_query_bindings(text,claim['query_bindings'],[item])
    assert checked['valid'] and len(checked['numeric_text'])==len(text)
    assert '2025年' not in checked['numeric_text']
    annotation=next(a for a in checked['context_annotations'] if a.get('kind')=='executed_query_time_filter')
    assert annotation['origin']=='executed_sql_where' and annotation['semantic_review_required']
    assert annotation['column']=='day' and annotation['end_exclusive']=='2026-01-01'
    assert _claims(out,{'E1':item},{})[0]['text']==text
    for false_fact in ['另有2025元。','另有2026笔。']:
        with pytest.raises((ValueError,CrossSourceError)):
            changed=expand_candidate_claims([candidate(item,text+false_fact)],[item]);_claims(changed,{'E1':item},{})


def test_repeated_year_as_business_value_and_unknown_parameter_bindings_not_authorized():
    item=evidence();text='按2025年统计，合计81000分，另有2025元。'
    with pytest.raises(ValueError,match='还有未由本格'):expand_candidate_claims([candidate(item,text)],[item])
    assert full_year_filter(evidence('day >= ? AND day < ?',['2025-01-01'])) is None
    item=evidence();item['query']['sql'] += " UNION SELECT 1,1"
    assert full_year_filter(item) is None


def test_only_one_actual_statement_and_parameter_token_positions_are_accepted():
    item=evidence();item['query']['sql'] += "; SELECT 1"
    assert full_year_filter(item) is None
    item=evidence("status='?' AND day >= ? AND day < ?",['2025-01-01','2026-01-01'])
    assert full_year_filter(item)['year']==2025
    item=evidence("day >= ?1 AND day < ?2",['2025-01-01','2026-01-01'])
    assert full_year_filter(item) is None

def test_rule_version_still_requires_its_own_evidence_reference():
    item=evidence();claim=candidate(item)
    out=expand_candidate_claims([claim],[item])
    with pytest.raises(CrossSourceError,match='未由所引证据'):_claims(out,{'E1':item},{})
    rule={'id':'E2','kind':'text','quote':'口径说明V1'}
    claim['evidence_ids'].append('E2')
    out=expand_candidate_claims([claim],[item,rule])
    assert _claims(out,{'E1':item,'E2':rule},{})[0]['text']==claim['text']


@pytest.mark.parametrize('intro',['按口径说明V3，','依据技术说明，','根据业务规范：'])
def test_year_at_start_of_source_qualified_clause_preserves_only_year_occurrence(intro):
    text=intro+'2025年登记金额合计81000分。'
    notes=time_annotations(text,evidence())
    assert len(notes)==1 and notes[0]['display_text']=='2025年'
    assert notes[0]['display_kind']=='full_calendar_year'
    assert text[notes[0]['statement_start']:notes[0]['statement_end']]=='2025年'


@pytest.mark.parametrize('display',[
    '日期按orders.day取2025-01-01至2026-01-01之前',
    '日期按day取2025-01-01至2026-01-01之前',
    '日期范围为2025-01-01（含）至2026-01-01（不含）',
    '期间为2025-01-01 到 2026-01-01以前',
])
def test_exact_executed_half_open_iso_interval_passes_without_authorizing_other_numbers(display):
    item=evidence();text=display+'，登记金额合计81000分。'
    out=expand_candidate_claims([candidate(item,text)],[item])
    note=out['claims'][0]['query_bindings'][0]['context_annotations'][0]
    assert note['display_kind']=='half_open_iso_date_range'
    assert note['start_inclusive']=='2025-01-01' and note['end_exclusive']=='2026-01-01'
    assert _claims(out,{'E1':item},{})[0]['text']==text
    with pytest.raises(ValueError,match='还有未由本格'):
        expand_candidate_claims([candidate(item,text+'另有2025元。')],[item])


@pytest.mark.parametrize('display',[
    '日期按orders.day取2024-01-01至2026-01-01之前',
    '日期按orders.day取2025-02-01至2026-01-01之前',
    '日期按orders.day取2025-01-01至2025-12-31之前',
    '日期按orders.day取2025-01-01至2026-01-01',
    '日期按orders.day取2025-01-01至2026-01-01（含）',
    '日期按orders.day取2025-01-01（不含）至2026-01-01之前',
    '日期按payments.day取2025-01-01至2026-01-01之前',
    '日期按orders.created取2025-01-01至2026-01-01之前',
    '日期范围不是2025-01-01至2026-01-01之前',
    '排除日期按orders.day取2025-01-01至2026-01-01之前',
    '金额范围为2025-01-01至2026-01-01之前',
    'payments.day的日期范围为2025-01-01至2026-01-01之前',
    '日期按orders.day取2025-01-01至2026-01-01之前以外',
    '日期范围为2025-01-01至2026-01-01之前并含截止日',
])
def test_inexact_ambiguous_excluded_or_wrong_field_interval_is_not_masked(display):
    item=evidence();text=display+'，登记金额合计81000分。'
    assert time_annotations(text,item)==[]
    with pytest.raises(ValueError,match='还有未由本格'):
        expand_candidate_claims([candidate(item,text)],[item])


def test_partial_interval_can_be_stated_exactly_but_cannot_be_promoted_to_annual_scope():
    item=evidence("day >= '2031-04-03' AND day < '2031-08-01'",[])
    exact='日期按orders.day取2031-04-03至2031-08-01之前，合计81000分。'
    notes=time_annotations(exact,item)
    assert len(notes)==1 and notes[0]['display_kind']=='half_open_iso_date_range'
    assert full_year_filter(item) is None
    assert _claims(expand_candidate_claims([candidate(item,exact)],[item]),{'E1':item},{})
    with pytest.raises(ValueError,match='还有未由本格'):
        expand_candidate_claims([candidate(item,'根据口径说明，2031年合计81000分。')],[item])


def test_iso_annotation_uses_actual_single_column_predicates_and_parameter_values():
    item=evidence('status=? AND o.day >= ? AND o.day < ?',['完成','2025-01-01','2026-01-01'])
    item['query']['sql']=item['query']['sql'].replace('FROM orders','FROM orders AS o')
    text='日期按o.day取2025-01-01至2026-01-01之前，合计81000分。'
    assert len(time_annotations(text,item))==1
    assert not time_annotations(text.replace('o.day','orders.created'),item)
    for where in ["day >= '2025-01-01' OR day < '2026-01-01'", "day >= '2025-01-01' AND other_day < '2026-01-01'"]:
        assert not time_annotations(text,evidence(where,[]))


@pytest.mark.parametrize('intro',[
    '同一查询中纳入统计的','同一查询中','本次查询中','该查询结果中',
    '同一查询中，','本次查询中, ','该查询结果中：',
])
def test_query_reference_year_preserves_actual_count_binding_and_narrative(intro):
    item=evidence();context=item['query_result_context'];cell=context['rows'][0]['cells'][1]
    text=intro+'2025年已完成订单行数为9。'
    claim={'text':text,'query_fact':{'evidence_id':'E1','context_id':context['context_alias'],
           'cell_id':cell['cell_id'],'entity_cells':[]}}
    out=expand_candidate_claims([claim],[item]);bound=out['claims'][0]['query_bindings'][0]
    note=bound['context_annotations'][0]
    assert note['display_text']=='2025年' and note['column']=='day'
    assert note['start_inclusive']=='2025-01-01' and note['end_exclusive']=='2026-01-01'
    assert note['semantic_review_required'] is True
    assert _claims(out,{'E1':item},{})[0]['text']==text
    assert bound['value']==9 and bound['cell_id']==cell['cell_id']
    for changed in [text.replace('为9','为8'),text+'另有2025条记录。',text.replace('2025年','2024年')]:
        with pytest.raises(ValueError):expand_candidate_claims([{**claim,'text':changed}],[item])


@pytest.mark.parametrize('intro',[
    '不是同一查询中','不在同一查询中','同一查询中未纳入统计的',
    '同一查询中排除的','设备寿命为','其他查询中',
    '不是同一查询中，','同一查询中未纳入统计的，',
])
def test_query_reference_cannot_authorize_negation_or_unrelated_year_context(intro):
    assert time_annotations(intro+'2025年已完成订单行数为9。',evidence())==[]


@pytest.mark.parametrize('suffix',['以外的','之外的','外的','1月的','上半年的','至2026年的'])
def test_query_reference_cannot_change_the_executed_year_scope(suffix):
    assert time_annotations('同一查询中2025年'+suffix+'订单行数为9。',evidence())==[]
    assert time_annotations('同一查询中，2025年'+suffix+'订单行数为9。',evidence())==[]


@pytest.mark.parametrize('where',[
    "day >= '2025-02-01' AND day < '2026-01-01'",
    "day >= '2025-01-01' AND other_day < '2026-01-01'",
    "day >= '2025-01-01' OR day < '2026-01-01'",
])
def test_query_reference_requires_actual_conjunctive_full_year_same_column(where):
    assert time_annotations('同一查询中纳入统计的2025年订单行数为9。',evidence(where,[]))==[]
    assert time_annotations('同一查询中，2025年订单行数为9。',evidence(where,[]))==[]


@pytest.mark.parametrize('separator',['','，',', ',': ','：'])
def test_query_reference_null_count_zero_uses_its_own_selected_output_cell(separator):
    item=evidence();item['query']['sql']=item['query']['sql'].replace('COUNT(*) AS count',
        'SUM(CASE WHEN amount IS NULL THEN 1 ELSE 0 END) AS missing_count')
    item['query_result']={'columns':['total','missing_count'],'rows':[[81000,0]],'truncated':False}
    item['query_result_context']=build_query_result_context(item);context=item['query_result_context']
    text='同一查询中'+separator+'2025年已完成订单里amount为NULL的行数为0。'
    claim={'text':text,'query_fact':{'evidence_id':'E1','context_id':context['context_alias'],
           'cell_id':context['rows'][0]['cells'][1]['cell_id'],'entity_cells':[]}}
    out=expand_candidate_claims([claim],[item])
    assert _claims(out,{'E1':item},{})[0]['text']==text
    wrong=copy.deepcopy(claim);wrong['query_fact']['cell_id']=context['rows'][0]['cells'][0]['cell_id']
    with pytest.raises(ValueError):expand_candidate_claims([wrong],[item])
