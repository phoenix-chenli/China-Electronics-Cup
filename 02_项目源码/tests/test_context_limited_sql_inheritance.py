"""Offline terminal-record replay and SQLite/store integration; no model calls."""
import copy
import json
from pathlib import Path

import pytest

from backend.conversation_state import build_initial, finalize_state, _reviewed_limited_sql_intent
from test_query_planner import source, Responses

FIXTURE=json.loads((Path(__file__).parent/'fixtures/context-inheritance-r9-cases.json').read_text())
CASES={c['id']:c['record'] for c in FIXTURE['cases']}


def case(ident='ctx01-t2'):
    item=copy.deepcopy(CASES[ident])
    item['context_state']=build_initial(item['id'],item['question'],item['sources'],patch=item.get('context_patch'))
    return item


@pytest.mark.parametrize('ident',list(CASES))
def test_actual_terminal_records_only_accept_separately_reviewed_fields(ident):
    item=case(ident);before=copy.deepcopy(item);result=item['result']
    state=finalize_state(item,item['status'],result)
    rejected=ident in {'ctx02-t2','ctx02-t5','ctx02-t6'}
    assert state['interpretation_available'] is not rejected
    assert bool(state['slots']) is not rejected
    assert item==before
    if not rejected:
        assert {k:s['value'] for k,s in state['slots'].items()}=={k:v for k,v in item['intent'].items() if v not in (None,'',[])}
        assert all(s['confirmed_by_user'] is False for s in state['slots'].values())
        if result['verification']['status']=='limited':
            assert all(s['verification']=='independent_intent_review' and s['answer_verification_status']=='limited' for s in state['slots'].values())
    assert result['verification']==before['result']['verification']


@pytest.mark.parametrize('status',['failed','cancelled','insufficient_evidence','needs_clarification','running'])
def test_noncompleted_limited_result_cannot_create_verified_slots(status):
    item=case();state=finalize_state(item,status,item['result'])
    assert state['slots']=={} and not state['interpretation_available']


@pytest.mark.parametrize('bad',[
    'wrong_mode','source_stale','evidence_stale','wrong_hash','wrong_parser','no_query','no_sql',
    'no_execution','preview_only','missing_scope','false_scope','semantic_false','missing_semantic_review',
    'unresolved_mechanical','adapter_unavailable','wrong_intent','wrong_normalized','new_review_false',
    'normalized_false','overall_false','field_false','missing_field','duplicate_field','no_grounding',
])
def test_limited_is_not_a_blanket_inheritance_permission(bad):
    item=case();result=item['result'];q=result['query'];a=result['context_interpretation'];review=a['intent_review']
    if bad=='wrong_mode':item['mode']='multi_source'
    elif bad=='source_stale':item['scope_stale']=True
    elif bad=='evidence_stale':result['evidence'][0]['stale']=True
    elif bad=='wrong_hash':result['evidence'][0]['sha256']='another-source'
    elif bad=='wrong_parser':result['evidence'][0]['parser_version']='old'
    elif bad=='no_query':result.pop('query')
    elif bad=='no_sql':q['sql']=''
    elif bad=='no_execution':q.pop('returned_rows')
    elif bad=='preview_only':q['executed_result']={'rows':result['rows']}
    elif bad=='missing_scope':q['scope_review'].pop('missing_values')
    elif bad=='false_scope':q['review_response']['scope_review']['missing_values']=False
    elif bad=='semantic_false':q['review_response']['supported']=False
    elif bad=='missing_semantic_review':q.pop('review_response')
    elif bad=='unresolved_mechanical':q['mechanical_contract']['unresolved_issues']=[{'code':'unauthorized_null_filter'}]
    elif bad=='adapter_unavailable':a['status']='unavailable'
    elif bad=='wrong_intent':item['intent']['time_range']='1900年'
    elif bad=='wrong_normalized':item['normalized_question']='另一问题'
    elif bad=='new_review_false':review['new_review']=False
    elif bad=='normalized_false':review['normalized_question_supported']=False
    elif bad=='overall_false':review['overall_supported']=False
    elif bad=='field_false':review['fields'][0]['supported']=False
    elif bad=='missing_field':review['fields'].pop()
    elif bad=='duplicate_field':review['fields'].append(copy.deepcopy(review['fields'][0]))
    elif bad=='no_grounding':review['grounding']=[]
    state=finalize_state(item,'completed',result)
    assert state['slots']=={} and not state['interpretation_available']
    assert result['verification']['status']=='limited'


def test_adjudicated_sql_keeps_original_false_and_requires_actual_adoption():
    item=case('ctx01-t1');q=item['result']['query']
    assert q['review_response']['supported'] is False
    assert _reviewed_limited_sql_intent(item,item['result'])
    original=copy.deepcopy(q['review_response'])
    finalize_state(item,'completed',item['result'])
    assert q['review_response']==original
    for key in ('selected_as_effective_review','passed_remaining_gates'):
        bad=copy.deepcopy(item);bad['result']['query']['review_adjudication'][key]=False
        assert not _reviewed_limited_sql_intent(bad,bad['result'])


def test_partial_field_review_keeps_only_accepted_and_does_not_resurrect_old_unit():
    item=case();result=item['result'];a=result['context_interpretation'];r=a['intent_review']
    parent={'id':'prior','context_state':build_initial('prior','原问题',item['sources'],patch=[{'op':'replace','field':'unit','value':'万元'}])}
    parent['context_state']['slots']['unit'].update(origin='model_interpretation',confirmed_by_user=False)
    item['context_state']=build_initial(item['id'],item['question'],item['sources'],parent)
    a['unknown_fields']=['unit'];r.update(status='partially_supported',overall_supported=False,rejected_fields=['unit'])
    r['fields'].append({'field':'unit','supported':False})
    state=finalize_state(item,'completed',result)
    assert state['interpretation_available'] and 'unit' not in state['slots']
    assert state['unknown_fields']['unit']['previous_source_answer_id']=='prior'
    assert set(state['slots'])==set(a['intent'])


def test_explicit_removal_and_replacement_still_override_reviewed_model_values():
    for operation in ('remove','replace'):
        item=case();patch={'op':operation,'field':'time_range'}
        if operation=='replace':patch['value']='2028年'
        item['context_patch']=[patch]
        item['context_state']=build_initial(item['id'],item['question'],item['sources'],patch=[patch])
        state=finalize_state(item,'completed',item['result'])
        if operation=='remove':assert 'time_range' not in state['slots']
        else:
            assert state['slots']['time_range']['value']=='2028年'
            assert state['slots']['time_range']['confirmed_by_user'] is True
        assert state['unresolved']


def test_unchanged_prior_slot_retains_original_turn_and_model_not_user_provenance():
    first=case();state=finalize_state(first,'completed',first['result']);parent={'id':first['id'],'context_state':state}
    item=copy.deepcopy(first);item['id']='next';item['context_state']=build_initial('next','继续',item['sources'],parent)
    later=finalize_state(item,'completed',item['result'])
    assert later['slots']==state['slots']
    assert later['slots']['time_range']['source_answer_id']==first['id']
    assert later['slots']['time_range']['confirmed_by_user'] is False


@pytest.mark.parametrize('unit',['保留两位小数','万元，保留两位小数'])
def test_display_precision_cannot_reenter_measurement_unit(unit):
    item=case();a=item['result']['context_interpretation'];r=a['intent_review']
    item['intent']['unit']=unit;a['intent']['unit']=unit
    r['accepted_fields']=sorted([*r['accepted_fields'],'unit'])
    r['fields'].append({'field':'unit','supported':True})
    r['grounding'].append({'field':'unit','source':'current_question','quote':'保留两位小数','source_answer_id':item['id']})
    state=finalize_state(item,'completed',item['result'])
    assert not state['interpretation_available'] and not state['slots']
    assert '两位小数' in item['normalized_question']


def test_actual_sql_adapter_and_store_pipeline_keeps_limited_and_terminal_immutable(source,tmp_path):
    from backend.query_planner import answer_database
    from backend.context_intent_adapter import adapt_answer_intent
    from backend.store import Store
    from backend.answer_store import AnswerStore
    store=Store(tmp_path/'workspace');store.initialize();answers=AnswerStore(store);answers.initialize()
    pid=store.create_project('isolated test','')['id']
    question='统计销售额合计，保留两位小数。'
    item,_=answers.create(pid,source,question,'database',None,'request',{})
    intent={'metric':'销售额合计'}
    gateway=Responses(
        {'status':'ready','normalized_question':question,'intent':intent,'sql':'SELECT ROUND(SUM(CAST(amount AS REAL)),2) AS amount FROM sale','parameters':[]},
        {'supported':True,'interpretation_supported':True,'scope_review':{key:True for key in ('source_basis','population_grain','quantifier_scope','missing_values','output_contract')}},
        {'status':'ready','normalized_question':question,'intent':intent,'grounding':[{'field':'metric','source':'current_question','quote':'销售额合计'}]},
        {'supported':True,'normalized_question_supported':True,'fields':[{'field':'metric','supported':True}]},
    )
    def interpretation(q,i):item.update(normalized_question=q,intent=i)
    status,result=answer_database(source,question,[],gateway,interpretation=interpretation)
    assert status=='completed' and result['rows']==[[4.5]] and result['verification']['status']=='limited'
    adapter=adapt_answer_intent(item,result,status,[],gateway)
    assert adapter['status']=='passed' and len(gateway.calls)==4
    item.update(intent=adapter['intent'],normalized_question=adapter['normalized_question'])
    result['context_interpretation']=adapter
    saved=answers.update(pid,item['id'],status=status,result=result,intent=item['intent'],normalized_question=item['normalized_question'])
    assert saved['result']['verification']['status']=='limited'
    assert saved['context_state']['slots']['metric']['value']=='销售额合计'
    assert saved['context_state']['slots']['metric']['confirmed_by_user'] is False
    before=answers.get(pid,item['id'])
    assert answers.update(pid,item['id'],status='completed',result={'verification':{'status':'passed'}})==before
    assert len(gateway.calls)==4


def test_real_t7_to_t8_changes_metric_does_not_inherit_count_unit_or_result_reference():
    item=copy.deepcopy(CASES['ctx01-t8']);old=copy.deepcopy(item)
    assert item['context_state']['slots']['metric']['value']=='发票张数'
    assert item['context_state']['slots']['unit']['value']=='张'
    state=finalize_state(item,'completed',item['result'])
    assert state['slots']['metric']['value']=='Invoice.Total合计'
    assert 'unit' not in state['slots'] and state['unknown_fields']['unit']['previous_source_answer_id']==CASES['ctx01-t7']['id']
    assert state['result_reference']['answer_id']==item['id']
    assert state['slots']['time_range']['source_answer_id']==CASES['ctx01-t7']['id']
    assert state['interpretation_available'] and item==old
    assert any(c['field']=='unit' and c['operation']=='mark_unknown' and c['before']['value']=='张' for c in state['changes'])


@pytest.mark.parametrize('ident',['ctx02-t2','ctx02-t5','ctx02-t6'])
def test_unavailable_new_interpretation_does_not_label_prior_slots_as_current(ident):
    prior=case('ctx02-t1');prior['context_state']=finalize_state(prior,'completed',prior['result'])
    item=case(ident);item['context_state']=build_initial(item['id'],item['question'],item['sources'],prior)
    state=finalize_state(item,'completed',item['result'])
    assert not state['interpretation_available'] and state['interpretation_status']=='unavailable'
    assert state['slots']=={} and state['result_reference'] is None
    assert state['previous_result_reference']['answer_id']==prior['id']
    assert state['unknown_fields']['metric']['previous_source_answer_id']==prior['id']
    assert all(c['operation']=='mark_unknown' for c in state['changes'])


def test_unavailable_adapter_does_not_repeal_explicit_prior_user_patch():
    item=case('ctx02-t2')
    prior={'id':'prior','context_state':build_initial('prior','只看2025年',item['sources'],patch=[{'op':'replace','field':'time_range','value':'2025年'}])}
    item['context_state']=build_initial(item['id'],item['question'],item['sources'],prior)
    state=finalize_state(item,'completed',item['result'])
    assert state['slots']['time_range']['value']=='2025年' and state['slots']['time_range']['confirmed_by_user']
    assert state['unresolved'][0]['kind']=='prior_user_patch_not_reviewed_for_current_question'
    assert not state['interpretation_available'] and state['result_reference'] is None


@pytest.mark.parametrize('overall_status',['completed','insufficient_evidence'])
def test_explicit_prior_user_patch_survives_received_field_false(overall_status):
    item=case();result=item['result'];a=result['context_interpretation'];review=a['intent_review']
    prior={'id':'prior','context_state':build_initial('prior','单位万元',item['sources'],patch=[{'op':'replace','field':'unit','value':'万元'}])}
    item['context_state']=build_initial(item['id'],item['question'],item['sources'],prior)
    a['unknown_fields']=['unit'];review.update(status='partially_supported',overall_supported=False,rejected_fields=['unit'])
    review['fields'].append({'field':'unit','supported':False})
    before=copy.deepcopy(prior)
    state=finalize_state(item,overall_status,result)
    assert state['slots']['unit']==prior['context_state']['slots']['unit']
    assert 'unit' not in state['unknown_fields']
    assert [c for c in state['unresolved'] if c['field']=='unit']==[{'field':'unit','kind':'prior_user_patch_not_reviewed_for_current_question','source_answer_id':item['id'],'previous_source_answer_id':'prior'}]
    assert not any(c['field']=='unit' and c['operation']=='mark_unknown' for c in state['changes'])
    assert prior==before and review['fields'][-1]['supported'] is False
