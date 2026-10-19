"""Omitted old model slots are unknown only after a separate complete field review."""
import copy,json
from pathlib import Path
import pytest
from backend.context_intent_adapter import adapt_answer_intent
from backend.conversation_state import build_initial,finalize_state,context_history,project_reviewed_context_state


def actual():return json.loads((Path(__file__).parent/'fixtures/context_omission_actual_t5.json').read_text())['item']

def reviewed(item=None):
    item=copy.deepcopy(item or actual());calls=[]
    clean=copy.deepcopy(item['intent']);normalized=item['normalized_question']
    def gateway(messages,purpose):
        calls.append(json.loads(messages[-1]['content']))
        if len(calls)==1:return {'status':'ready','intent':clean,'normalized_question':normalized,
            'grounding':[{'field':f,'source':'published_claim','claim_ids':['C1']} for f in clean]}
        return {'supported':True,'normalized_question_supported':True,'fields':[{'field':f,'supported':True} for f in clean]}
    out=adapt_answer_intent(item,item['result'],'completed',[],gateway)
    assert len(calls)==2 and out['status']=='passed' and out['intent_review']['new_review'] is True
    item['result']['context_interpretation']=out;item['intent']=copy.deepcopy(out['intent']);item['normalized_question']=out['normalized_question']
    return item


def test_real_t5_existing_overall_receipt_is_not_a_separate_field_review():
    item=actual();before=copy.deepcopy(item)
    assert project_reviewed_context_state(item)==item['context_state']
    assert item==before and 'comparison_base' in item['context_state']['slots']


def test_real_t5_missing_old_model_slot_uses_existing_two_step_review_and_quarantines():
    item=reviewed();before=copy.deepcopy(item);state=finalize_state(item,'completed',item['result'])
    assert item==before and 'comparison_base' not in state['slots']
    unknown=state['unknown_fields']['comparison_base']
    assert unknown['previous_slot']==before['context_state']['slots']['comparison_base']
    assert unknown['verification']=='omitted_from_complete_current_interpretation'
    assert '不代表用户已撤销' in unknown['reason'] and not unknown['confirmed_by_user']
    for f in ('metric','time_range','filters','unit','entities','topic'):
        assert state['slots'][f]['value']==item['intent'][f]


def test_new_child_and_long_history_share_detached_projection_without_editing_parent():
    item=reviewed();before=copy.deepcopy(item)
    child=build_initial('child','继续',item['sources'],item)
    assert 'comparison_base' not in child['slots'] and 'comparison_base' in child['unknown_fields']
    history=context_history([item]*31,max_chars=200000)
    assert history[0]['kind']=='structured_context'
    assert 'comparison_base' not in history[0]['context_state']['slots']
    assert history[-1]['original_question']==item['question'] and item==before

@pytest.mark.parametrize('mutate',[
    lambda i:i.update(status='failed'),lambda i:i.update(scope_stale=True),
    lambda i:i['result'].update(scope_stale=True),
    lambda i:i['result']['verification'].update(status='limited'),
    lambda i:i['result']['context_interpretation'].update(status='unavailable'),
    lambda i:i['result']['context_interpretation'].update(normalized_question='not same'),
    lambda i:i['result']['context_interpretation']['intent'].update(metric='other'),
    lambda i:i['result']['context_interpretation']['intent_review'].update(new_review=False),
    lambda i:i['result']['context_interpretation']['intent_review'].update(normalized_question_supported=False),
    lambda i:i['result']['context_interpretation']['intent_review'].update(overall_supported=False),
    lambda i:i['result']['context_interpretation']['intent_review'].update(fields=[]),
    lambda i:i['result']['context_interpretation']['intent_review']['fields'][0].update(supported=False),
    lambda i:i['result']['context_interpretation']['intent_review']['fields'].append({'field':'metric','supported':True}),
    lambda i:i['result']['context_interpretation']['intent_review'].update(accepted_fields=[]),
    lambda i:i['result']['context_interpretation']['intent_review'].update(grounding=[]),
    lambda i:i['result']['context_interpretation']['intent_review']['fields'][0].update(field=[]),
    lambda i:i['result']['context_interpretation']['intent_review']['grounding'][0].update(field={}),
])
def test_incomplete_rejected_mismatched_or_stale_review_never_clears(mutate):
    item=reviewed();mutate(item);before=copy.deepcopy(item)
    out=project_reviewed_context_state(item)
    assert out==item['context_state'] and item==before

@pytest.mark.parametrize('origin,source,confirmed',[
    ('user_patch','old',True),('user_patch','current',True),('unknown','old',False),('model_interpretation','current',False),
])
def test_explicit_user_current_and_unknown_origin_slots_are_never_cleared(origin,source,confirmed):
    item=reviewed();slot=item['context_state']['slots']['comparison_base'];slot.update(origin=origin,source_answer_id=item['id'] if source=='current' else 'old',confirmed_by_user=confirmed)
    assert project_reviewed_context_state(item)['slots']['comparison_base']==slot


def test_explicitly_retained_current_field_is_not_quarantined_and_complete_fast_path_stays_free():
    item=actual();item['intent']['comparison_base']=item['context_state']['slots']['comparison_base']['value']
    def no_calls(*a):raise AssertionError('complete reused interpretation should be free')
    out=adapt_answer_intent(item,item['result'],'completed',[],no_calls)
    assert out['model_calls']==0 and out['intent']['comparison_base']==item['intent']['comparison_base']
    item=reviewed();v=item['context_state']['slots']['comparison_base']['value']
    item['intent']['comparison_base']=v;ci=item['result']['context_interpretation'];ci['intent']['comparison_base']=v
    review=ci['intent_review'];review['fields'].append({'field':'comparison_base','supported':True})
    review['accepted_fields']=sorted(item['intent']);review['grounding'].append({'field':'comparison_base','source':'published_claim','claim_ids':['C1']})
    assert 'comparison_base' in project_reviewed_context_state(item)['slots']


def test_missing_item_field_cannot_be_resurrected_by_query_plan_fast_path():
    item=actual();other=copy.deepcopy(item['intent']);other['comparison_base']=item['context_state']['slots']['comparison_base']['value']
    item['result']['query']={'plan':{'intent':other,'normalized_question':item['normalized_question']}}
    assert reviewed(item)['result']['context_interpretation']['intent_review']['new_review'] is True
