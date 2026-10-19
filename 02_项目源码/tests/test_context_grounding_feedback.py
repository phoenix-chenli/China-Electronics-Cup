"""Offline regression tests for precise provenance repair feedback; no model API calls."""
import copy

import pytest

from test_context_intent_adapter import answer, fact_result, Gateway, accepted
from backend import context_intent_adapter as candidate_module


def proposed():
    return {'status':'ready','normalized_question':'统计全部年份登记金额，仅限甲地区。',
            'intent':{'metric':'登记金额','filters':['仅甲地区'],'time_range':None},
            'grounding':[{'field':'metric','source':'current_question','quote':'登记金额'},
                         {'field':'filters','source':'current_question','quote':'仅限甲地区'},
                         {'field':'time_range','source':'current_question','quote':'全部年份'}]}


def item():
    x=answer();x['question']='统计全部年份登记金额，仅限甲地区。';return x


def repaired():
    x=proposed();x['intent'].pop('time_range');x['grounding'].pop();return x


def test_removed_null_field_identified_exactly_no_automatic_deletion_or_acceptance():
    bad=proposed();good=repaired();g=Gateway(bad,good,accepted(good));before=copy.deepcopy(bad)
    out=candidate_module.adapt_answer_intent(item(),fact_result(),'completed',[],g)
    assert out['status']=='passed' and out['intent']==good['intent']
    assert [r for r,_ in g.calls]==['document','document','verify']
    feedback=g.calls[1][1]['protocol_feedback'][0]
    assert feedback['provided_field']=='time_range' and feedback['grounding_index']==2
    assert feedback['required_grounding_fields']==['filters','metric']
    assert g.calls[1][1]['protocol_field_contract']['required_grounding_fields']==['filters','metric']
    assert bad==before and out['confirmed_by_user'] is False


def test_unfixed_extra_grounding_still_fails_at_same_progress_bound():
    bad=proposed();g=Gateway(bad,bad)
    out=candidate_module.adapt_answer_intent(item(),fact_result(),'completed',[],g)
    assert out['status']=='unavailable' and out['reason']=='protocol_repair_no_progress'
    assert len(g.calls)==2 and not out['intent']


def test_repaired_protocol_still_requires_true_independent_meaning_review():
    bad=proposed();good=repaired();g=Gateway(bad,good,{'supported':False,'normalized_question_supported':False,'fields':[]})
    out=candidate_module.adapt_answer_intent(item(),fact_result(),'completed',[],g)
    assert out['status']=='unavailable' and out['reason']=='independent_intent_review_rejected'
    assert [r for r,_ in g.calls]==['document','document','verify']


def test_unknown_entry_key_is_reported_and_still_rejected():
    x=repaired();x['grounding'][0]['value']='made-up'
    errors=candidate_module._grounding_errors(x['grounding'],x['intent'],item()['question'],{},[], 'a',[])
    assert errors[0]['field']=='metric' and errors[0]['unexpected_entry_keys']==['value']
    with pytest.raises(ValueError):candidate_module._grounding(x['grounding'],x['intent'],item()['question'],{},[],'a',[])


def test_limited_old_slot_not_silently_promoted_to_confirmed():
    slots={'metric':{'value':'登记金额','source_answer_id':'old','verification':'independent_intent_review','confirmed_by_user':False}}
    raw=[{'field':'metric','source':'prior_slot','source_answer_id':'old'}]
    errors=candidate_module._grounding_errors(raw,{'metric':'登记金额'},item()['question'],slots,[],'a',[])
    assert errors[0]['prior_slot_checks']['recorded_verification']=='independent_intent_review'
    assert errors[0]['prior_slot_checks']['confirmed_by_user'] is False
    with pytest.raises(ValueError):candidate_module._grounding(raw,{'metric':'登记金额'},item()['question'],slots,[],'a',[])


@pytest.mark.parametrize('bad_entry',[None,{}, {'field':123}, {'field':'made_up_field','source':'current_question','quote':'登记金额'}])
def test_malformed_or_unrecognized_field_is_identified_without_approval(bad_entry):
    errors=candidate_module._grounding_errors([bad_entry],{},item()['question'],{},[],'a',[])
    assert errors and errors[0]['grounding_index']==0
    with pytest.raises(ValueError):candidate_module._grounding([bad_entry],{},item()['question'],{},[],'a',[])
