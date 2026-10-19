"""Synthetic question/answer fixtures; no live model or business-data claim."""
import copy
import hashlib
import json

import pytest

from backend.answer_store import AnswerStore
from backend.context_intent_adapter import (
    adapt_answer_intent, retrieve_ancestor_history, validate_history_requests,
)
from backend.model_gateway import ModelError
from backend.store import Store


SOURCES=[{'id':'s','sha256':'original-sha','parser_version':'test-1','revision':'parse-1','semantic_revision':'sem-1'}]


def fact_result():
    return {'text':'仅限甲地区，2024年已登记金额为10万元。',
        'claims':[{'id':'C1','text':'仅限甲地区，2024年已登记金额为10万元。','evidence_ids':['E1']}],
        'evidence':[{'id':'E1','source_id':'s','sha256':'original-sha','page':2,'quote':'原始记载10万元'}],
        'verification':{'status':'passed'}}


def answer():
    return {'id':'a','project_id':'p','question':'仅看甲地区2024年已登记金额',
            'sources':copy.deepcopy(SOURCES),'context_state':{'slots':{}},'context_patch':[]}


def candidate():
    return {'status':'ready','normalized_question':'仅看甲地区2024年已登记金额',
        'intent':{'metric':'已登记金额','time_range':'2024年','filters':['仅甲地区']},
        'grounding':[{'field':'metric','source':'current_question','quote':'已登记金额'},
                     {'field':'time_range','source':'current_question','quote':'2024年'},
                     {'field':'filters','source':'current_question','quote':'仅看甲地区'}]}


def accepted(proposed=None):
    return {'supported':True,'normalized_question_supported':True,
            'fields':[{'field':f,'supported':True} for f in (proposed or candidate())['intent']]}


class Gateway:
    def __init__(self,*responses):self.responses=list(responses);self.calls=[];self.last=None
    def __call__(self,messages,role):
        self.calls.append((role,json.loads(messages[-1]['content'])))
        response=self.responses.pop(0) if self.responses else self.last
        self.last=response
        if isinstance(response,Exception):raise response
        return copy.deepcopy(response)


@pytest.mark.parametrize('location',['item','plan'])
def test_existing_reviewed_engine_intent_is_reused_without_another_call(location):
    item=answer();result=fact_result();intent={'metric':'amount','filters':[],'time_range':'2024','sort':'descending'}
    if location=='item':item.update(intent=intent,normalized_question=item['question'])
    else:result['query']={'plan':{'intent':intent,'normalized_question':item['question']}}
    gateway=Gateway();before=copy.deepcopy((item,result))
    output=adapt_answer_intent(item,result,'completed',[],gateway)
    assert output['status']=='passed' and output['model_calls']==0 and not gateway.calls
    assert output['intent']=={'metric':'amount','filters':[],'time_range':'2024'}
    assert output['intent_review']['basis']=='existing_engine_reviewed_interpretation'
    assert not output['confirmed_by_user'] and (item,result)==before


def test_document_intent_requires_exact_grounding_and_independent_field_review():
    gateway=Gateway(candidate(),accepted());item=answer();result=fact_result();before=copy.deepcopy((item,result))
    output=adapt_answer_intent(item,result,'completed',[],gateway)
    assert output['status']=='passed' and output['intent']==candidate()['intent']
    assert [role for role,_ in gateway.calls]==['document','verify'] and output['model_calls']==2
    assert output['intent_review']['new_review'] and not output['confirmed_by_user']
    assert all(g['source_answer_id']=='a' for g in output['intent_review']['grounding'])
    assert gateway.calls[1][1]['candidate']['normalized_question']==item['question']
    assert gateway.calls[1][1]['candidate_fields']==list(candidate()['intent'])
    assert 'allowed_fields' not in gateway.calls[1][1]
    assert (item,result)==before


def test_empty_unrecorded_placeholders_are_omitted_before_exact_candidate_review():
    proposed=candidate();proposed['intent'].update(dimensions=[],assumptions=[],grain='')
    gateway=Gateway(proposed,accepted())
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],gateway)
    assert output['status']=='passed' and output['intent']==candidate()['intent']
    assert gateway.calls[1][1]['candidate_fields']==list(candidate()['intent'])


@pytest.mark.parametrize('existing',[True,False])
def test_empty_values_cannot_silently_clear_an_existing_slot_or_bypass_their_own_grounding(existing):
    item=answer();proposed=candidate();proposed['intent']['dimensions']=[]
    if existing:item['context_state']['slots']['dimensions']={'value':['地区'],'verification':'answer_semantic_review','source_answer_id':'old'}
    else:proposed['grounding'].append({'field':'dimensions','source':'current_question','quote':'移除地区分组'})
    gateway=Gateway(proposed)
    assert adapt_answer_intent(item,fact_result(),'completed',[],gateway)['status']=='unavailable'
    assert 1<=len(gateway.calls)<=3 and all(role=='document' for role,_ in gateway.calls)


@pytest.mark.parametrize('mutation',[None,'wrong_id','summary_only','rewritten_only','duplicate','snapshot','wrong_quote'])
def test_historical_conditions_are_grounded_in_original_ancestor_utterance_not_invented_slots(mutation):
    item=answer();item['question']='只看第三产业，其他口径保持。'
    history=[{'answer_id':'old','original_question':'2024年石景山区地区生产总值及按不变价格计算的同比增速是多少？',
              'question':'2024年石景山区地区生产总值及按不变价格计算的同比增速是多少？',
              'normalized_question':'虚构改写口径','answer':'虚构摘要口径'}]
    proposed={'status':'ready','normalized_question':'2024年石景山区第三产业按不变价格计算的同比增速是多少？',
              'intent':{'entities':['第三产业'],'time_range':'2024年','metric':'按不变价格计算的同比增速'},
              'grounding':[{'field':'entities','source':'current_question','quote':'第三产业'},
                  {'field':'time_range','source':'ancestor_question','source_answer_id':'old','quote':'2024年'},
                  {'field':'metric','source':'ancestor_question','source_answer_id':'old','quote':'按不变价格计算的同比增速'}]}
    if mutation=='wrong_id':proposed['grounding'][1]['source_answer_id']='sibling'
    if mutation=='summary_only':history[0].pop('original_question');history[0]['answer']='2024年'
    if mutation=='rewritten_only':history[0]['original_question']='原话没有年份';history[0]['normalized_question']='2024年'
    if mutation=='duplicate':history.append(copy.deepcopy(history[0]))
    if mutation=='snapshot':history[0]['kind']='structured_context'
    if mutation=='wrong_quote':proposed['grounding'][1]['quote']='2025年'
    gateway=Gateway(proposed,accepted(proposed));before=copy.deepcopy(history)
    output=adapt_answer_intent(item,fact_result(),'completed',history,gateway)
    assert (output['status']=='passed')==(mutation is None)
    assert len(gateway.calls)==2
    if mutation is not None:assert all(role=='document' for role,_ in gateway.calls)
    assert history==before and not output['confirmed_by_user']


@pytest.mark.parametrize('source_id',['omitted','a','old',None])
def test_published_claim_can_omit_local_id_but_cannot_change_the_current_answer(source_id):
    proposed=candidate();proposed['grounding'][0]={'field':'metric','source':'published_claim','claim_ids':['C1']}
    if source_id!='omitted':proposed['grounding'][0]['source_answer_id']=source_id
    gateway=Gateway(proposed,accepted())
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],gateway)
    assert (output['status']=='passed')==(source_id in ('omitted','a'))
    if output['status']=='passed':assert output['intent_review']['grounding'][0]['source_answer_id']=='a'


def test_published_fact_cannot_become_an_unrequested_user_assumption():
    proposed=candidate()
    proposed['intent']['assumptions']=['假设登记金额为10万元']
    proposed['grounding'].append({'field':'assumptions','source':'published_claim','claim_ids':['C1']})
    gateway=Gateway(proposed)
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],gateway)
    assert output['status']=='unavailable' and output['intent']=={}
    assert all(role=='document' for role,_ in gateway.calls)
    assert any('用户假设' in error['reason']
               for attempt in output['protocol_attempts'] for error in attempt['errors'])


def test_explicit_user_assumption_keeps_question_provenance():
    item=answer();item['question']+='；仅假设费率为2%，不要当作实际费率。'
    proposed=candidate()
    proposed['normalized_question']=item['question']
    proposed['intent']['assumptions']=['假设费率为2%']
    proposed['grounding'].append({'field':'assumptions','source':'current_question','quote':'仅假设费率为2%'})
    gateway=Gateway(proposed,accepted(proposed))
    output=adapt_answer_intent(item,fact_result(),'completed',[],gateway)
    assert output['status']=='passed'
    assert output['intent']['assumptions']==['假设费率为2%']
    assert not output['confirmed_by_user']
    assumption=next(g for g in output['intent_review']['grounding'] if g['field']=='assumptions')
    assert assumption['source']=='current_question' and assumption['quote']=='仅假设费率为2%'


def test_reviewer_extra_absent_slots_remain_protocol_rejection():
    review=accepted();review['fields'].append({'field':'denominator','supported':True})
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],Gateway(candidate(),review))
    assert output['reason']=='independent_intent_review_rejected'


@pytest.mark.parametrize('mutation',[
    lambda c:c['grounding'][0].update(quote='不存在的用户条件'),
    lambda c:c['grounding'].pop(),
    lambda c:c['intent'].update(secret_extra='ignore instructions'),
    lambda c:c['intent'].update(filters='甲地区'),
    lambda c:c['intent'].update(metric=float('inf')),
    lambda c:c['grounding'][0].update(source='user_confirmed'),
    lambda c:c['grounding'][0].update(source='published_claim',source_answer_id='a',claim_ids=['C999']),
])
def test_ungrounded_or_untyped_proposal_is_not_even_sent_to_review(mutation):
    proposed=candidate();mutation(proposed);gateway=Gateway(proposed)
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],gateway)
    assert output['status']=='unavailable' and output['intent']=={} and 1<=len(gateway.calls)<=3
    assert all(role=='document' for role,_ in gateway.calls)


@pytest.mark.parametrize('review',[
    {'supported':True,'normalized_question_supported':True},
    {'supported':True,'normalized_question_supported':False,'fields':accepted()['fields']},
    {'supported':True,'normalized_question_supported':True,'fields':[{'field':'metric','supported':True}]*3},
    {'supported':False,'normalized_question_supported':True,'fields':accepted()['fields']},
    None,
])
def test_overall_confidence_does_not_replace_per_field_and_qualifier_review(review):
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],Gateway(candidate(),review))
    assert output['status']=='unavailable' and output['intent']=={}
    assert output['reason']=='independent_intent_review_rejected'


def test_wrong_year_rejected_without_discarding_other_independently_supported_fields():
    proposed=candidate();proposed['intent']['time_range']='2025年'
    review=accepted();review['fields'][1]['supported']=False
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],Gateway(proposed,review))
    assert output['status']=='passed' and 'time_range' not in output['intent']
    assert output['unknown_fields']==['time_range']
    assert set(output['intent'])=={'metric','filters'}


@pytest.mark.parametrize('invalid',['value','id','provenance',None])
def test_inherited_slot_requires_same_value_actual_turn_and_verified_provenance(invalid):
    item=answer();item['question']='仍按该口径'
    item['context_state']['slots']['time_range']={'value':'2024年','source_answer_id':'ancestor',
        'confirmed_by_user':False,'verification':'answer_semantic_review'}
    proposed={'status':'ready','normalized_question':'仍按2024年口径','intent':{'time_range':'2024年'},
              'grounding':[{'field':'time_range','source':'prior_slot','source_answer_id':'ancestor'}]}
    if invalid=='value':proposed['intent']['time_range']='2025年'
    if invalid=='id':proposed['grounding'][0]['source_answer_id']='sibling'
    if invalid=='provenance':item['context_state']['slots']['time_range']['verification']='model_self_report'
    gateway=Gateway(proposed,accepted(proposed))
    output=adapt_answer_intent(item,fact_result(),'completed',[],gateway)
    assert (output['status']=='passed')==(invalid is None)
    assert len(gateway.calls)==2
    if invalid is not None:assert all(role=='document' for role,_ in gateway.calls)


def test_partial_facts_can_support_question_interpretation_without_upgrading_the_answer():
    result=fact_result();result['verification']['status']='limited'
    before=copy.deepcopy(result)
    output=adapt_answer_intent(answer(),result,'insufficient_evidence',[],Gateway(candidate(),accepted()))
    assert output['status']=='passed' and result==before
    assert result['verification']['status']=='limited'


@pytest.mark.parametrize('status',['failed','cancelled','needs_clarification','running'])
def test_unpublished_results_never_become_context_facts(status):
    gateway=Gateway()
    assert adapt_answer_intent(answer(),fact_result(),status,[],gateway)['status']=='unavailable'
    assert not gateway.calls


@pytest.mark.parametrize('code',['timeout','transport','response_json_failed'])
def test_unknown_transport_outcome_propagates_once_and_is_not_retried(code):
    gateway=Gateway(ModelError('explicit test failure',code))
    with pytest.raises(ModelError) as error:adapt_answer_intent(answer(),fact_result(),'completed',[],gateway)
    assert error.value.code==code and len(gateway.calls)==1


def test_history_request_is_bounded_and_returned_before_any_review_or_history_guess():
    gateway=Gateway({'status':'history_required','history_requests':[{'answer_id':'ancestor','row_start':11,'row_limit':1,'text_limit':0}]})
    output=adapt_answer_intent(answer(),fact_result(),'completed',[],gateway)
    assert output['status']=='history_required' and output['model_calls']==1
    assert output['history_requests'][0]['row_start']==11 and not output['intent']


@pytest.mark.parametrize('requested_ranges',[
    [],[{'answer_id':'a','row_limit':41}],[{'answer_id':'a','row_start':-1}],
    [{'answer_id':'a','row_limit':True}],[{'answer_id':'a','sql':'SELECT 1'}],
    [{'answer_id':'a','row_limit':0,'text_limit':0}],
    [{'answer_id':'a','text_limit':6001}],
    [{'answer_id':str(i)} for i in range(5)],
])
def test_history_protocol_rejects_out_of_scope_operations_and_unbounded_ranges(requested_ranges):
    with pytest.raises(ValueError):validate_history_requests(requested_ranges)


def test_payload_budget_never_silently_discards_older_conditions():
    gateway=Gateway()
    output=adapt_answer_intent(answer(),fact_result(),'completed',[{'answer':'x'*80000}],gateway)
    assert output['reason']=='context_budget_without_silent_truncation' and not gateway.calls


def test_cancelled_between_calls_does_not_start_reviewer():
    gateway=Gateway(candidate())
    with pytest.raises(InterruptedError):
        adapt_answer_intent(answer(),fact_result(),'completed',[],gateway,cancelled=lambda:bool(gateway.calls))
    assert len(gateway.calls)==1


@pytest.fixture
def history_store(tmp_path):
    store=Store(tmp_path/'runtime');store.initialize();pid=store.create_project('合成历史隔离测试','')['id']
    answers=AnswerStore(store);answers.initialize()
    old=answer();old.update(id='ancestor',project_id=pid,status='completed',parent_answer_id=None,result=fact_result())
    old['result'].update(columns=['行号','金额'],rows=[[i,str(i)+'.00'] for i in range(1,26)],query={'truncated':True})
    old['result']['evidence'][0].update(secret='do not leak test value')
    old['result']['query']['executed_result']={'rows':[['rejected candidate',999999]]}
    latest=copy.deepcopy(old);latest.update(id='latest',parent_answer_id='ancestor')
    sibling=copy.deepcopy(old);sibling.update(id='sibling',parent_answer_id='ancestor')
    with store.transaction() as c:
        for record in (old,latest,sibling):
            c.execute('INSERT INTO answers VALUES(?,?,?,?)',(record['id'],pid,record['id'],json.dumps(record,ensure_ascii=False)))
    return store,answers,pid


def body_hashes(store):
    with store.transaction() as c:
        return {r['id']:hashlib.sha256(r['body'].encode()).hexdigest() for r in c.execute('SELECT id,body FROM answers')}


def test_actual_persisted_ancestor_recovers_twelfth_row_without_mutating_saved_bodies(history_store):
    store,answers,pid=history_store;before=body_hashes(store)
    output=retrieve_ancestor_history(answers,pid,'latest',[{'answer_id':'ancestor','row_start':11,'row_limit':1,'text_limit':5}],current_sources=SOURCES)
    item=output['items'][0]
    assert output['complete'] and item['rows']==[[12,'12.00']]
    assert item['row_range']=={'start':11,'end':12,'total_saved_rows':25,'complete_saved_rows':False,'original_query_truncated':True}
    assert item['text']=='仅限甲地区' and not item['text_range']['complete']
    assert item['claims'][0]['text']==fact_result()['text']
    assert item['evidence'][0]['page']==2 and item['historical_snapshot']
    serialized=json.dumps(output,ensure_ascii=False)
    assert '999999' not in serialized and 'do not leak test value' not in serialized
    assert before==body_hashes(store) and output['model_calls']==0


def test_sibling_and_other_project_cannot_be_read_by_guessing_ids(history_store):
    store,answers,pid=history_store
    with pytest.raises(ValueError,match='祖先分支'):
        retrieve_ancestor_history(answers,pid,'latest',[{'answer_id':'sibling'}])
    other=store.create_project('另一测试项目','')['id']
    with pytest.raises(ValueError):retrieve_ancestor_history(answers,other,'latest',[{'answer_id':'ancestor'}])


@pytest.mark.parametrize('change',['semantic','parse','missing','empty','malformed','duplicate'])
def test_changed_or_unrecorded_source_scope_does_not_release_old_values(history_store,change):
    _,answers,pid=history_store;current=copy.deepcopy(SOURCES)
    if change=='semantic':current[0]['semantic_revision']='sem-2'
    if change=='parse':current[0]['revision']='parse-2'
    if change=='missing':current[0].pop('revision')
    if change=='empty':current=[]
    if change=='malformed':current[0]['revision']=['wrong shape']
    if change=='duplicate':current.append(copy.deepcopy(current[0]))
    output=retrieve_ancestor_history(answers,pid,'latest',[{'answer_id':'ancestor','row_limit':20}],current_sources=current)
    assert not output['complete'] and output['items'][0]['rows']==[]
    assert output['items'][0]['text'] is None and output['items'][0]['claims']==[]


def test_recovery_budget_includes_omission_report_and_never_slices_facts(history_store):
    _,answers,pid=history_store
    output=retrieve_ancestor_history(answers,pid,'latest',[{'answer_id':'ancestor','row_limit':25},{'answer_id':'latest','row_limit':25}],max_chars=1000)
    assert not output['complete'] and output['omitted_requests']
    assert len(json.dumps(output,ensure_ascii=False))<=1000
    assert not output['items']


def test_insufficient_ancestor_keeps_verified_claims_but_never_returns_unpublished_rows(history_store):
    store,answers,pid=history_store
    with store.transaction() as c:
        record=json.loads(c.execute("SELECT body FROM answers WHERE id='ancestor'").fetchone()[0])
        record['status']='insufficient_evidence';record['result']['verification']['status']='limited'
        c.execute("UPDATE answers SET body=? WHERE id='ancestor'",(json.dumps(record,ensure_ascii=False),))
    output=retrieve_ancestor_history(answers,pid,'latest',[{'answer_id':'ancestor','row_limit':25}])
    assert output['items'][0]['claims'] and output['items'][0]['rows']==[]
    assert output['items'][0]['status']=='insufficient_evidence'


@pytest.mark.parametrize('location',['item','plan'])
def test_precision_only_unit_is_not_reused_as_measurement_unit(location):
    item=answer();item['question']='仅看甲地区2024年已登记金额，保留两位小数'
    result=fact_result();intent={'metric':'已登记金额','unit':'保留两位小数'}
    if location=='item':item.update(intent=intent,normalized_question=item['question'])
    else:result['query']={'plan':{'intent':intent,'normalized_question':item['question']}}
    gateway=Gateway();out=adapt_answer_intent(item,result,'completed',[],gateway)
    assert out['status']=='passed' and out['intent']=={'metric':'已登记金额'}
    assert out['unknown_fields']==['unit'] and out['model_calls']==0
    assert '保留两位小数' in out['normalized_question']


def test_mixed_unit_precision_requires_grounded_repair_not_silent_stripping():
    item=answer();item['question']='2024年已登记金额，以万元表示，保留两位小数'
    first={'status':'ready','normalized_question':item['question'],
           'intent':{'metric':'已登记金额','unit':'万元，保留两位小数'},
           'grounding':[{'field':'metric','source':'current_question','quote':'已登记金额'},
                        {'field':'unit','source':'current_question','quote':'万元'}]}
    fixed=copy.deepcopy(first);fixed['intent']['unit']='万元'
    gateway=Gateway(first,fixed,accepted(fixed));out=adapt_answer_intent(item,fact_result(),'completed',[],gateway)
    assert out['status']=='passed' and out['intent']['unit']=='万元'
    assert out['model_calls']==3 and out['protocol_attempts'][0]['errors'][0]['field']=='unit'
    assert '保留两位小数' in out['normalized_question']
    assert gateway.calls[-1][1]['candidate']['intent']['unit']=='万元'


@pytest.mark.parametrize('unit',['元','万元','%','kWh','小时','人','元/件'])
def test_real_measurement_units_survive_precision_type_guard(unit):
    from backend.context_intent_adapter import _typed_intent
    assert _typed_intent({'unit':unit})=={'unit':unit}
