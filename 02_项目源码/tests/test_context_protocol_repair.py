"""Explicit fake responses probe protocol repair; no external model/network call."""
import copy
import pytest
from backend.context_intent_adapter import adapt_answer_intent
from backend.conversation_state import build_initial,finalize_state
from backend.model_gateway import ModelError
from test_context_intent_adapter import answer,candidate,accepted,fact_result,Gateway

def test_precise_missing_field_feedback_reextracts_then_independently_reviews():
    bad=candidate();bad['grounding'].pop();good=candidate();g=Gateway(bad,good,accepted(good))
    out=adapt_answer_intent(answer(),fact_result(),'completed',[],g)
    assert out['status']=='passed' and out['model_calls']==3
    assert [role for role,_ in g.calls]==['document','document','verify']
    assert g.calls[1][1]['protocol_feedback']==[{'field':'filters','reason':'每个意图槽位必须有实际出处'}]
    assert [a['status'] for a in out['protocol_attempts']]==['grounding_rejected','grounding_valid']

def test_wrong_prior_value_not_relaxed_and_corrected_original_quote_requires_new_review():
    item=answer();item['context_state']['slots']['time_range']={'value':'2023年','source_answer_id':'ancestor','verification':'answer_semantic_review'}
    bad=candidate();bad['grounding'][1]={'field':'time_range','source':'prior_slot','source_answer_id':'ancestor'}
    good=candidate();g=Gateway(bad,good,accepted(good))
    out=adapt_answer_intent(item,fact_result(),'completed',[],g)
    assert out['status']=='passed' and out['intent']['time_range']=='2024年'
    assert g.calls[1][1]['protocol_feedback'][0]['field']=='time_range'
    assert out['intent_review']['grounding'][1]['source']=='current_question'
    bad_twice=Gateway(bad,bad)
    rejected=adapt_answer_intent(item,fact_result(),'completed',[],bad_twice)
    assert rejected['status']=='unavailable' and rejected['reason']=='protocol_repair_no_progress'
    assert len(bad_twice.calls)==2 and all(role=='document' for role,_ in bad_twice.calls)

def test_repair_cannot_replace_ancestor_id_with_a_sibling_or_summary():
    bad=candidate();bad['grounding'][1]={'field':'time_range','source':'ancestor_question','source_answer_id':'sibling','quote':'2024年'}
    history=[{'answer_id':'ancestor','original_question':'2024年'}, {'answer_id':'sibling','kind':'structured_context','original_question':'2024年'}]
    out=adapt_answer_intent(answer(),fact_result(),'completed',history,Gateway(bad,bad))
    assert out['status']=='unavailable' and not out['intent']

def test_genuinely_decreasing_grounding_errors_can_continue_past_two_calls():
    first=candidate();first['grounding']=[]
    second=candidate();second['grounding']=second['grounding'][:1]
    third=candidate();third['grounding']=third['grounding'][:2]
    good=candidate();g=Gateway(first,second,third,good,accepted(good))
    out=adapt_answer_intent(answer(),fact_result(),'completed',[],g)
    assert out['status']=='passed' and out['model_calls']==5
    assert [len(a['errors']) for a in out['protocol_attempts']]==[3,2,1,0]

def test_changed_words_without_fewer_grounding_errors_stop_for_no_progress():
    versions=[]
    for i in range(5):
        bad=candidate();bad['grounding']=[];bad['normalized_question']+=' '+str(i);versions.append(bad)
    g=Gateway(*versions);out=adapt_answer_intent(answer(),fact_result(),'completed',[],g)
    assert out['reason']=='protocol_repair_no_progress' and len(g.calls)==3

def test_optional_topic_without_anchor_is_omitted_before_review_not_fabricated():
    proposed=candidate();proposed['intent']['topic']='虚构标题'
    g=Gateway(proposed,accepted(candidate()));out=adapt_answer_intent(answer(),fact_result(),'completed',[],g)
    assert out['status']=='passed' and 'topic' not in out['intent']
    assert out['unknown_fields']==['topic']
    assert 'topic' not in g.calls[1][1]['candidate_fields']


def test_dropping_a_rejected_grounding_field_cannot_silently_reuse_old_value():
    item=answer();item['context_state']=build_initial('a',item['question'],[],patch=[{'op':'replace','field':'time_range','value':'2023年'}])
    item['context_state']['slots']['time_range']['source_answer_id']='old'
    item['context_state']['slots']['time_range'].update(origin='model_interpretation',confirmed_by_user=False)
    bad=candidate();bad['grounding'][1]={'field':'time_range','source':'prior_slot','source_answer_id':'old'}
    repaired=candidate();repaired['intent'].pop('time_range');repaired['grounding'].pop(1)
    out=adapt_answer_intent(item,fact_result(),'completed',[],Gateway(bad,repaired,accepted(repaired)))
    assert out['status']=='passed' and out['unknown_fields']==['time_range']
    item['intent']=out['intent']
    state=finalize_state(item,'completed',{**fact_result(),'context_interpretation':out})
    assert 'time_range' not in state['slots'] and 'time_range' in state['unknown_fields']

def partial_unit():
    proposed=candidate();proposed['intent']['unit']='万元';proposed['grounding'].append({'field':'unit','source':'published_claim','claim_ids':['C1']})
    review=accepted(proposed);review['supported']=False;review['fields'][-1]['supported']=False
    return proposed,review

def test_partial_independent_support_keeps_five_conditions_not_single_unit():
    proposed,review=partial_unit();g=Gateway(proposed,review);out=adapt_answer_intent(answer(),fact_result(),'completed',[],g)
    assert out['status']=='passed' and out['intent']==candidate()['intent']
    assert out['unknown_fields']==['unit'] and out['intent_review']['status']=='partially_supported'
    assert out['intent_review']['overall_supported'] is False
    assert all(x['field']!='unit' for x in out['intent_review']['grounding'])

@pytest.mark.parametrize('normalized_supported',[False,None,1])
def test_partial_fields_never_survive_unverified_independent_question(normalized_supported):
    proposed,review=partial_unit();review['normalized_question_supported']=normalized_supported
    out=adapt_answer_intent(answer(),fact_result(),'completed',[],Gateway(proposed,review))
    assert out['status']=='unavailable' and not out['intent'] and 'unknown_fields' not in out

@pytest.mark.parametrize('status',['completed','insufficient_evidence'])
def test_rejected_prior_user_unit_is_preserved_as_explicit_and_unresolved(status):
    parent={'id':'parent','context_state':build_initial('parent','旧问题',[],patch=[{'op':'replace','field':'unit','value':'万元'}])}
    item=answer();item['context_state']=build_initial(item['id'],item['question'],[],parent)
    before=copy.deepcopy(parent);proposed,review=partial_unit()
    out=adapt_answer_intent(item,fact_result(),status,[],Gateway(proposed,review))
    item['intent']=out['intent'];result={**fact_result(),'context_interpretation':out}
    state=finalize_state(item,status,result)
    assert state['slots']['unit']==parent['context_state']['slots']['unit'] and 'unit' not in state['unknown_fields']
    assert state['unresolved'][0]['previous_source_answer_id']=='parent'
    assert state['unresolved'][0]['kind']=='prior_user_patch_not_reviewed_for_current_question' and parent==before
    assert not any(c['field']=='unit' and c['operation']=='mark_unknown' for c in state['changes'])
    following=build_initial('next','继续',[],{'id':item['id'],'context_state':state})
    assert following['slots']['unit']['value']=='万元' and following['unresolved']==state['unresolved']

def test_model_unknown_cannot_repeal_current_explicit_user_patch():
    item=answer();item['context_patch']=[{'op':'replace','field':'unit','value':'万元'}]
    item['context_state']=build_initial('a',item['question'],[],patch=item['context_patch'])
    proposed,review=partial_unit();out=adapt_answer_intent(item,fact_result(),'completed',[],Gateway(proposed,review))
    item['intent']=out['intent'];state=finalize_state(item,'completed',{**fact_result(),'context_interpretation':out})
    assert state['slots']['unit']['value']=='万元' and state['slots']['unit']['confirmed_by_user']
    assert any(c['kind']=='explicit_patch_interpretation_unverified' for c in state['unresolved'])

def test_later_explicit_replacement_resolves_unknown():
    state=build_initial('a','q',[]);state['unknown_fields']={'unit':{'source_answer_id':'a'}}
    new=build_initial('b','我指定元',[],{'id':'a','context_state':state},[{'op':'replace','field':'unit','value':'元'}])
    assert new['slots']['unit']['value']=='元' and 'unit' not in new['unknown_fields']

@pytest.mark.parametrize('code',['transport','timeout','response_json_failed'])
def test_unknown_post_during_repair_is_not_automatically_replayed(code):
    bad=candidate();bad['grounding']=[];g=Gateway(bad,ModelError('fake transport',code),candidate())
    with pytest.raises(ModelError):adapt_answer_intent(answer(),fact_result(),'completed',[],g)
    assert len(g.calls)==2

def test_cancel_between_repair_attempts_stops_before_another_call():
    bad=candidate();bad['grounding']=[];g=Gateway(bad,candidate())
    with pytest.raises(InterruptedError):adapt_answer_intent(answer(),fact_result(),'completed',[],g,cancelled=lambda:len(g.calls)>0)
    assert len(g.calls)==1
