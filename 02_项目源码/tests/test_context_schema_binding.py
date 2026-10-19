"""Synthetic schema/context fixtures. No API calls or benchmark answer imports."""
import copy

import pytest

from backend.context_intent_adapter import adapt_answer_intent, _binding_review
from backend.conversation_state import build_initial, context_history, finalize_state
from test_context_intent_adapter import Gateway, accepted


def fixture():
    item={'id':'a','mode':'database','source_sha256':'source-sha','sources':[],
          'question':'只看西区、人员编号2，2024年；按logs.hours求和，NULL未知不补零。',
          'context_patch':[]}
    item['context_state']=build_initial('a',item['question'],[])
    result={'text':'已登记工时为2小时。','rows':[[2]],'columns':['工时'],
            'claims':[{'id':'C1','text':'已登记工时为2小时。','evidence_ids':['E1']}],
            'evidence':[{'id':'E1','source_id':'s','sha256':'source-sha','table_name':'logs'}],
            'verification':{'status':'passed'},
            'query':{'sql':'SELECT SUM(l.hours) FROM logs l JOIN people p ON p.id=l.person_id WHERE p.region=? AND p.id=? AND l.year=?',
                     'parameters':['西区',2,2024],'used_tables':['people','logs'],'returned_rows':1,
                     'read_columns':{'people':['id','region'],'logs':['hours','person_id','year']},
                     'answer_scope':{'source_sha256':'source-sha','source_tables':[
                         {'table':'people','columns':['id','name','region'],'columns_truncated':False},
                         {'table':'logs','columns':['hours','person_id','year'],'columns_truncated':False}]}}}
    proposed={'status':'ready','normalized_question':item['question'],
              'intent':{'filters':[{'table':'people','column':'store','value':'西区'},
                                   {'table':'people','column':'id','value':2}],
                        'metric':'logs.hours求和','time_range':'2024年','assumptions':['NULL未知不补零']},
              'grounding':[{'field':'filters','source':'current_question','quote':'只看西区、人员编号2'},
                           {'field':'metric','source':'current_question','quote':'logs.hours求和'},
                           {'field':'time_range','source':'current_question','quote':'2024年'},
                           {'field':'assumptions','source':'current_question','quote':'NULL未知不补零'}]}
    return item,result,proposed


def test_semantic_acceptance_cannot_establish_a_nonexistent_column():
    item,result,proposed=fixture();before=copy.deepcopy((item,result,proposed))
    gateway=Gateway(proposed,accepted(proposed))
    out=adapt_answer_intent(item,result,'completed',[],gateway)
    assert out['status']=='passed' and out['model_calls']==2
    assert 'filters' not in out['intent'] and out['unknown_fields']==['filters']
    assert out['intent']['time_range']=='2024年'
    assert out['intent']['assumptions']==['NULL未知不补零']
    assert out['unbound_conditions'][0]['original_question']==item['question']
    assert out['unbound_conditions'][0]['grounding'][0]['quote']=='只看西区、人员编号2'
    assert out['intent_review']['schema_binding']['issues'][0]['reason']=='column_not_in_current_schema'
    assert next(v for v in out['intent_review']['model_fields'] if v['field']=='filters')['supported']
    assert not next(v for v in out['intent_review']['fields'] if v['field']=='filters')['supported']
    assert gateway.calls[0][1]['sql_binding_evidence']['read_columns']==result['query']['read_columns']
    assert (item,result,proposed)==before


def test_real_field_is_kept_without_relabeling_or_rewriting_the_condition():
    item,result,proposed=fixture();proposed['intent']['filters'][0]['column']='region'
    out=adapt_answer_intent(item,result,'completed',[],Gateway(proposed,accepted(proposed)))
    assert out['intent']==proposed['intent'] and out['unbound_conditions']==[]


@pytest.mark.parametrize('column,reason',[
    ('name','column_not_read_by_current_query'),
    ('store','column_not_in_current_schema'),
    (None,'invalid_structured_reference'),
])
def test_existing_but_unexecuted_and_invalid_bindings_are_not_confirmed(column,reason):
    item,result,_=fixture()
    review=_binding_review(item,result,{'filters':[{'table':'people','column':column,'value':'west'}]})
    assert review['rejected_fields']==['filters']
    assert review['issues'][0]['reason']==reason


@pytest.mark.parametrize('mutation',['source_mismatch','malformed_reads','missing_rows','diagnostic_only'])
def test_unavailable_authoritative_binding_never_falls_back_to_planner_claims(mutation):
    item,result,proposed=fixture();proposed['intent']['filters'][0]['column']='region'
    if mutation=='source_mismatch':result['query']['answer_scope']['source_sha256']='other'
    if mutation=='malformed_reads':result['query']['read_columns']='model says valid'
    if mutation=='missing_rows':result.pop('rows')
    if mutation=='diagnostic_only':result['query']['executed_result']={'rows':[[2]]}
    result['query']['plan']={'intent':proposed['intent']}
    review=_binding_review(item,result,proposed['intent'])
    assert set(review['rejected_fields'])=={'filters','metric'}


def test_actual_reads_can_establish_identifiers_when_catalog_excerpt_is_missing():
    item,result,_=fixture();result['query'].pop('answer_scope')
    assert _binding_review(item,result,{'filters':[{'table':'people','column':'region','value':'west'}]})['status']=='passed'


def test_legacy_sql_projection_uses_only_unambiguous_executed_physical_references():
    item,result,_=fixture();result['query'].pop('read_columns');result['query'].pop('answer_scope')
    assert _binding_review(item,result,{'filters':[{'table':'people','column':'region','value':'west'}]})['status']=='passed'
    assert _binding_review(item,result,{'filters':[{'table':'people','column':'store','value':'west'}]})['status']=='unbound'
    result['query']['sql']='SELECT hours FROM logs JOIN people ON people.id=logs.person_id'
    assert _binding_review(item,result,{'metric':'logs.hours求和'})['status']=='unbound'


def test_string_binding_checked_but_literal_values_and_natural_conditions_are_not_reparsed():
    item,result,_=fixture()
    assert _binding_review(item,result,{'filters':["people.store='west'"]})['rejected_fields']==['filters']
    assert _binding_review(item,result,{'filters':[{'table':'people','column':'region','value':'people.store'},'仅西区']})['status']=='passed'


def test_sqlite_identifier_case_is_not_a_semantic_rename():
    item,result,_=fixture()
    assert _binding_review(item,result,{'filters':[{'table':'PEOPLE','column':'REGION','value':'west'}]})['status']=='passed'


def test_entities_explicit_reference_cannot_bypass_the_same_schema_guard():
    item,result,_=fixture()
    bad={'entities':[{'table':'people','column':'store','value':'west'}]}
    assert _binding_review(item,result,bad)['rejected_fields']==['entities']
    good={'entities':[{'table':'people','column':'region','value':'west'}]}
    assert _binding_review(item,result,good)['status']=='passed'


def test_plain_entity_names_are_not_forced_into_database_field_bindings():
    item,result,_=fixture()
    plain={'entities':['Dr.Smith','people.store',{'name':'Example.Co'}]}
    assert _binding_review(item,result,plain)['status']=='passed'


def test_reviewed_plan_does_not_bypass_binding_guard():
    item,result,proposed=fixture();item.update(intent=proposed['intent'],normalized_question=item['question'])
    good=copy.deepcopy(proposed['intent']);good['filters'][0]['column']='region'
    result['query']['plan']={'intent':good,'normalized_question':item['question']}
    gateway=Gateway();out=adapt_answer_intent(item,result,'completed',[],gateway)
    assert out['intent']==good and not gateway.calls


def test_finalization_guards_legacy_or_bypassed_adapter_without_erasing_original_condition():
    item,result,proposed=fixture();item['intent']=proposed['intent']
    before=copy.deepcopy((item,result));state=finalize_state(item,'completed',result)
    assert 'filters' not in state['slots'] and state['slots']['time_range']['value']=='2024年'
    assert state['unknown_fields']['filters']['binding_status']=='unbound'
    assert state['unknown_fields']['filters']['conditions'][0]['original_question']==item['question']
    assert (item,result)==before


def test_current_adapter_unbound_condition_is_carried_into_state():
    item,result,proposed=fixture()
    out=adapt_answer_intent(item,result,'completed',[],Gateway(proposed,accepted(proposed)))
    item.update(intent=out['intent'],normalized_question=out['normalized_question'])
    result['context_interpretation']=out
    state=finalize_state(item,'completed',result)
    assert 'filters' not in state['slots']
    assert state['unknown_fields']['filters']['conditions'][0]['grounding'][0]['quote']=='只看西区、人员编号2'


def test_user_request_is_preserved_but_bad_identifier_is_not_confirmed():
    item,result,proposed=fixture()
    item['context_patch']=[{'op':'replace','field':'filters','value':proposed['intent']['filters']}]
    item['context_state']=build_initial(item['id'],item['question'],[],patch=item['context_patch'])
    state=finalize_state(item,'completed',result)
    assert 'filters' not in state['slots']
    unknown=state['unknown_fields']['filters']
    assert unknown['previous_slot']['origin']=='user_patch'
    assert not unknown['confirmed_by_user'] and unknown['conditions'][0]['candidate_value']==proposed['intent']['filters']


def test_history_filters_legacy_binding_only_in_projection_and_keeps_original_utterance():
    item,result,proposed=fixture();item.update(intent=proposed['intent'],result=result,status='completed')
    before=copy.deepcopy(item);history=context_history([item])
    assert 'filters' not in history[0]['intent']
    assert history[0]['question']==item['question'] and history[0]['original_question']==item['question']
    assert history[0]['unbound_conditions'][0]['candidate_value']==proposed['intent']['filters']
    assert item==before


def test_compressed_legacy_snapshot_does_not_reintroduce_the_bad_binding():
    item,result,proposed=fixture();item.update(intent=proposed['intent'],result=result,status='completed')
    item['context_state']['interpretation_available']=True
    item['context_state']['slots']={k:{'value':copy.deepcopy(v),'origin':'model_interpretation'}
                                    for k,v in proposed['intent'].items()}
    chain=[copy.deepcopy(item) for _ in range(31)];before=copy.deepcopy(chain)
    for i,turn in enumerate(chain):turn['id']=str(i)
    before=copy.deepcopy(chain);history=context_history(chain)
    assert history[0]['kind']=='structured_context'
    assert 'filters' not in history[0]['context_state']['slots']
    assert history[0]['context_state']['unknown_fields']['filters']['binding_status']=='unbound'
    assert chain==before
