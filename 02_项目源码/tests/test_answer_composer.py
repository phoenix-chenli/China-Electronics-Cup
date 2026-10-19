"""Exact presentation checks using explicit synthetic verified-result fixtures."""
import copy
import json

import pytest

from backend.answer_composer import compose_answer


def result(*texts):
    return {'text':'\n\n'.join(texts),'claims':[{'id':f'C{i}','text':t,'evidence_ids':['E1']} for i,t in enumerate(texts,1)],
            'evidence':[{'id':'E1','source_id':'s','sha256':'sha','page':2,'localization_scope':'original_page','bbox':[0,0,1,1]}],
            'verification':{'status':'passed','checks':['explicit test fixture']}}


@pytest.mark.parametrize('text',[
    '2024年工业增加值为30.7亿元，同比下降7.2%，占比2.3%。',
    '该方案尚处于征求意见阶段，不能认定已经实施。',
    '该柱接近但未达到标准线；原图没有标注精确值。',
    '仅限甲地区2023年已登记样本，不代表全年总体。',
    '此处“0”是原始值，空白表示未记录，不能合并为零。',
])
def test_direct_fact_keeps_numbers_entities_years_negation_and_qualifiers_exact(text):
    source=result(text);before=copy.deepcopy(source)
    projected=compose_answer(source,'completed','事实是什么？')
    assert projected['answer_plan']['kind']=='direct_fact'
    assert len(projected['display_blocks'])==1
    fact=projected['display_blocks'][0]
    assert fact['text']==text and fact['evidence_ids']==['E1']
    assert fact['citation_placement']=='immediately_after_fact'
    assert all(projected['composition_audit']['checks'].values())
    assert source==before and projected['composition_audit']['original_text']==text
    assert projected['composition_audit']['new_model_calls']==0


def test_comparison_reorders_by_question_parts_without_deriving_a_difference():
    source=result('甲为10万元。','乙为7万元。')
    source['completeness']={'status':'assessed','items':[{'id':'Q1','text':'先乙后甲','status':'answered','claim_ids':['C2','C1']}]}
    projected=compose_answer(source,'completed','比较乙和甲')
    assert projected['answer_plan']['kind']=='comparison'
    assert [b['text'] for b in projected['display_blocks']]==['乙为7万元。','甲为10万元。']
    assert '3万元' not in json.dumps(projected,ensure_ascii=False)
    assert projected['composition_audit']['original_text']=='甲为10万元。\n\n乙为7万元。'


def test_partial_answer_retains_verified_fact_then_concrete_question_gap():
    source=result('甲为10万元。');source['verification']['status']='limited'
    source['completeness']={'items':[{'id':'Q1','text':'甲是多少','status':'answered','claim_ids':['C1']},
                                    {'id':'Q2','text':'乙是多少','status':'missing_evidence','claim_ids':[]}]}
    projected=compose_answer(source,'insufficient_evidence')
    assert projected['answer_plan']['kind']=='partial_answer'
    blocks=projected['display_blocks'];assert [b['kind'] for b in blocks]==['scope_notice','fact','coverage_gap']
    assert blocks[1]['text']=='甲为10万元。'
    assert blocks[-1]['items'][0]['question_text']=='乙是多少'
    assert blocks[-1]['original_source_absence_claimed'] is False


def test_calculation_copies_actual_parameters_formula_units_and_rounding_only():
    source=result('计算结果为2.50元。');source['claims'][0]['calculation_ids']=['K1']
    calc={'id':'K1','expression':'a/b','parameters':[{'name':'a','value':'5','evidence_ids':['E1']},{'name':'b','value':'2','evidence_ids':['E1']}],
          'value':'2.5','display_value':'2.50','unit':'元','precision':2,'evidence_ids':['E1'],
          'rule_origin':'source','rule_evidence_ids':['E1'],'numeric_policy':'ROUND_HALF_UP',
          'internal_debug':'not presentation data'}
    source['calculations']=[calc]
    projected=compose_answer(source,'completed')
    assert projected['answer_plan']['kind']=='calculation'
    block=next(b for b in projected['display_blocks'] if b['kind']=='calculation')
    assert block['calculation']['parameters']==calc['parameters']
    assert block['calculation']['numeric_policy']=='ROUND_HALF_UP'
    assert block['recomputed_by_composer'] is False
    assert 'internal_debug' not in block['calculation']


def test_rejected_execution_and_model_audit_are_never_displayed():
    source=result('已经核对的事实。')
    source['query']={'executed_result':{'rows':[[999999]]}}
    source['diagnostics']={'candidate_attempts':[{'text':'错误秘密候选999999'}]}
    projected=compose_answer(source,'insufficient_evidence')
    assert '999999' not in json.dumps(projected,ensure_ascii=False)
    failed=compose_answer(source,'failed')
    assert not any(b['kind']=='fact' for b in failed['display_blocks'])


def test_verified_table_keeps_full_rows_and_display_rounding_without_recalculation():
    source=result('已核对结果见表。')
    source.update(columns=['地区','值'],rows=[['甲','1.234'],['乙',None]],display_rows=[['甲','1.23'],['乙',None]])
    projected=compose_answer(source,'completed','比较地区')
    table=next(b for b in projected['display_blocks'] if b['kind']=='result_table')
    assert table['rows']==source['rows'] and table['display_rows']==source['display_rows']
    assert not any(b['kind']=='result_table' for b in compose_answer(source,'insufficient_evidence')['display_blocks'])


@pytest.mark.parametrize('mutation',[
    lambda r:r['claims'][0].update(evidence_ids=['missing']),
    lambda r:r['claims'][0].update(supported=False),
    lambda r:r['evidence'].append(copy.deepcopy(r['evidence'][0])),
    lambda r:r.update(verification={}),
])
def test_missing_or_ambiguous_review_provenance_never_becomes_polished_fact(mutation):
    source=result('原候选事实');mutation(source)
    projected=compose_answer(source,'completed')
    assert projected['display_blocks']==[] and projected['composition_audit']['status']=='unavailable'


def test_budget_keeps_original_and_does_not_silently_cut_qualifiers():
    source=result('仅限测试范围。' * 30000)
    projected=compose_answer(source,'completed')
    assert not projected['display_blocks']
    assert projected['composition_audit']['original_text']==source['text']
    assert 'display_budget_exceeded_without_truncating_facts' in projected['composition_audit']['issues']


def test_clarification_preserves_real_options_without_selecting_for_user():
    source={'text':'请选择客户','clarification':{'question':'需要哪位客户？','options':['编号1','编号2'],'field':'id'}}
    block=compose_answer(source,'needs_clarification')['display_blocks'][0]
    assert block['text']=='需要哪位客户？' and block['options']==['编号1','编号2'] and block['requires_user_input']
