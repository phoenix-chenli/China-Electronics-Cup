"""Offline suggestion/state tests; gateway fixtures are explicit fakes."""
import copy
import json
import threading
import time

import pytest

from backend.store import Store
from backend.suggestion_service import SuggestionService, SuggestionStore, candidate_suggestions, suggestion_scope


@pytest.fixture
def runtime(tmp_path):
    store=Store(tmp_path/'runtime');store.initialize()
    project=store.create_project('建议测试','')
    return store,project['id']


def answer(pid,ident='a1',**updates):
    value={'id':ident,'project_id':pid,'question':'按地区统计金额','status':'completed','context_revision':'context1',
           'sources':[{'id':'s','sha256':'sha','parser_version':'p1','revision':'r1','semantic_revision':'m1'}],
           'context_state':{'slots':{'metric':{'value':'金额','verification':'answer_semantic_review','confirmed_by_user':False}}},
           'result':{'text':'甲为10元，乙为7元。','claims':[{'id':'C1','text':'甲为10元，乙为7元。','evidence_ids':['E1']}],
                     'columns':['地区','金额'],'rows':[['甲',10],['乙',7]],'evidence':[{'id':'E1','source_id':'s'}],
                     'verification':{'status':'passed'}}}
    value.update(updates);return value


CAPS={'local_actions':['explain_scope','verify_sources','visualize_result','scenario_setup','add_sources'],
      'catalogs':[{'semantic_layer':{'source_sha256':'sha'},'source_snapshot':{'id':'s','sha256':'sha','parser_version':'p1','revision':'r1','semantic_revision':'m1'},'tables':[{'name':'orders','columns':[
          {'name':'region','type':'TEXT'},{'name':'created','type':'DATETIME'}]}]}]}


def wait_ready(service,pid,key):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        record=service.get(pid,key)
        if record['state']!='preparing':return record
        threading.Event().wait(.005)
    raise AssertionError('suggestion worker did not finish')


def test_templates_require_actual_schema_or_registered_local_action(runtime):
    store,pid=runtime;item=answer(pid)
    candidates=candidate_suggestions(item,CAPS)
    assert {'explain_scope','visualize_result','compare_result','verify_sources'}<={c['kind'] for c in candidates}
    assert all(c['automatic_submission'] is False for c in candidates)
    refs=[r for c in candidates for r in c['payload'].get('schema_references',[])]
    assert refs==[]  # A real field alone is not a confirmed dimension for this result.
    other=copy.deepcopy(CAPS);other['catalogs'][0]['semantic_layer']['source_sha256']='another-source'
    assert not any(c['payload'].get('schema_references') for c in candidate_suggestions(item,other))
    outdated=copy.deepcopy(CAPS);outdated['catalogs'][0]['source_snapshot']['revision']='old-parse'
    assert not any(c['payload'].get('schema_references') for c in candidate_suggestions(item,outdated))
    assert all(c['action']=='ask' for c in candidate_suggestions(item,{}))


def test_suggestions_are_questions_not_predicted_answers_or_audit_leaks(runtime):
    _,pid=runtime;item=answer(pid)
    item['result']['diagnostics']={'candidate_attempts':[{'text':'未来增长999% key=secret'}]}
    candidates=candidate_suggestions(item,CAPS)
    rendered=json.dumps(candidates,ensure_ascii=False)
    assert '999' not in rendered and 'secret' not in rendered
    assert all(c['text'].endswith('？') for c in candidates)
    assert all('增长' not in c['text'] for c in candidates)


def test_exact_answered_question_and_duplicates_are_removed(runtime):
    _,pid=runtime;item=answer(pid)
    first=candidate_suggestions(item,CAPS)
    texts=[c['text'] for c in first]
    filtered=candidate_suggestions(item,CAPS,answered_questions=texts)
    assert filtered==[] and len(set(texts))==len(texts)


def test_default_generation_runs_in_worker_and_persistent_cache_is_reused(runtime):
    store,pid=runtime;service=SuggestionService(store);service.start()
    try:
        pending=service.submit(answer(pid),'branch-main',CAPS)
        ready=wait_ready(service,pid,pending['key'])
        assert ready['state']=='ready' and ready['model_calls']==0
        again=service.submit(answer(pid),'branch-main',CAPS)
        assert again['generation']==ready['generation']
        assert SuggestionStore(store).get(pid,pending['key'])['state']=='ready'
    finally:service.close()


def test_accept_only_fills_text_and_never_calls_model_or_submits_query(runtime):
    store,pid=runtime;service=SuggestionService(store);service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);ready=wait_ready(service,pid,record['key'])
        selected=ready['suggestions'][0]
        accepted=service.accept(pid,ready['key'],selected['id'],ready['scope'])
        assert accepted['state']=='accepted' and accepted['fill_text']==selected['text']
        assert accepted['model_calls']==0
        with pytest.raises(ValueError):service.accept(pid,ready['key'],selected['id'],ready['scope'])
    finally:service.close()


def slow_factory(started,release,counter,*,bad=False):
    def factory(cancelled):
        def gateway(messages,purpose):
            counter.append((threading.current_thread().name,purpose))
            started.set();assert release.wait(3)
            data=json.loads(messages[-1]['content'])
            if bad:raise ValueError('secret_api_key=must-not-leak')
            return {'ranked_ids':[c['id'] for c in data['candidates']]}
        return gateway
    return factory


@pytest.mark.parametrize('close_action',['dismiss','disable','cancel','close'])
def test_late_model_reply_cannot_restore_closed_suggestion(runtime,close_action):
    store,pid=runtime;started=threading.Event();release=threading.Event();calls=[]
    service=SuggestionService(store,slow_factory(started,release,calls));service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);assert started.wait(1)
        assert calls[0][0].startswith('zhiheng-suggestion')
        if close_action=='dismiss':service.dismiss(pid,record['key'],record['scope'])
        elif close_action=='disable':service.set_enabled(pid,False)
        elif close_action=='cancel':service.cancel(pid,record['key'])
        else:service.close()
        release.set()
        deadline=time.monotonic()+2
        while service.tasks and time.monotonic()<deadline:threading.Event().wait(.005)
        current=service.get(pid,record['key'])
        assert current['state']==('dismissed' if close_action=='dismiss' else 'hidden')
        assert len(calls)==1
        assert current['model_calls']==1
    finally:release.set();service.close()


def test_new_turn_expires_old_worker_but_sibling_branch_remains_isolated(runtime):
    store,pid=runtime;started=threading.Event();release=threading.Event();calls=[]
    service=SuggestionService(store,slow_factory(started,release,calls));service.start()
    try:
        old=service.submit(answer(pid),'main',CAPS);assert started.wait(1)
        newer=service.submit(answer(pid,'a2',context_revision='context2'),'main',CAPS)
        sibling=service.submit(answer(pid,'a3',context_revision='context3'),'fork',CAPS)
        release.set();new_ready=wait_ready(service,pid,newer['key']);sibling_ready=wait_ready(service,pid,sibling['key'])
        assert service.get(pid,old['key'])['state']=='hidden'
        assert service.get(pid,old['key'])['reason']=='superseded'
        assert new_ready['state']==sibling_ready['state']=='ready'
    finally:release.set();service.close()


def test_source_or_semantic_change_invalidates_ready_and_accept(runtime):
    store,pid=runtime;current={'valid':True}
    service=SuggestionService(store,is_current=lambda scope:current['valid']);service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);ready=wait_ready(service,pid,record['key'])
        current['valid']=False
        with pytest.raises(ValueError):service.accept(pid,ready['key'],ready['selected_id'],ready['scope'])
        assert service.get(pid,ready['key'])['state']=='hidden'
    finally:service.close()


def test_scope_cache_keys_include_branch_context_and_semantic_revision(runtime):
    _,pid=runtime;item=answer(pid)
    first=suggestion_scope(item,'branch')
    changed=copy.deepcopy(item);changed['sources'][0]['semantic_revision']='m2'
    assert suggestion_scope(changed,'branch')!=first
    assert suggestion_scope(item,'other-branch')!=first
    changed=copy.deepcopy(item);changed['context_revision']='other-context'
    assert suggestion_scope(changed,'branch')!=first


def test_model_error_is_sanitized_once_and_recovers_without_resending(runtime):
    store,pid=runtime;started=threading.Event();release=threading.Event();release.set();calls=[]
    service=SuggestionService(store,slow_factory(started,release,calls,bad=True));service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);failed=wait_ready(service,pid,record['key'])
        assert failed['state']=='hidden' and failed['reason']=='generation_failed'
        assert failed['recovery']['action']=='retry_suggestions'
        assert 'secret' not in json.dumps(failed)
        service.submit(answer(pid),'branch',CAPS)
        assert len(calls)==1
    finally:service.close()


def test_model_cannot_inject_new_question_through_ranking(runtime):
    store,pid=runtime
    service=SuggestionService(store,lambda cancelled:lambda messages,purpose:{'ranked_ids':['未来利润必涨99%']});service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);failed=wait_ready(service,pid,record['key'])
        assert failed['state']=='hidden' and not failed['suggestions']
        assert '99%' not in json.dumps(failed,ensure_ascii=False)
    finally:service.close()


@pytest.mark.parametrize('status',['failed','cancelled','needs_clarification'])
def test_failure_or_clarification_gets_recovery_not_business_prediction(runtime,status):
    store,pid=runtime
    def forbidden(_cancelled):raise AssertionError('no gateway should be created')
    service=SuggestionService(store,forbidden);service.start()
    try:
        item=answer(pid,status=status)
        item['result']['clarification']={'question':'需要哪位客户？','options':['编号1','编号2'],'field':'id'}
        record=service.submit(item,'branch',CAPS)
        assert record['state']=='hidden' and record['recovery'] and not record['suggestions']
        if status=='needs_clarification':assert record['recovery']['question']=='需要哪位客户？'
    finally:service.close()


def test_restart_marks_preparing_interrupted_without_new_call(runtime):
    store,pid=runtime;durable=SuggestionStore(store);durable.initialize()
    record,_=durable.begin(suggestion_scope(answer(pid),'branch'))
    restarted=SuggestionStore(store);restarted.initialize()
    current=restarted.get(pid,record['key'])
    assert current['state']=='hidden' and current['reason']=='interrupted'


def test_project_disable_survives_restart_and_enable_does_not_revive_dismissed(runtime):
    store,pid=runtime;service=SuggestionService(store);service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);wait_ready(service,pid,record['key'])
        service.dismiss(pid,record['key']);service.set_enabled(pid,False);service.set_enabled(pid,True)
        assert service.submit(answer(pid),'branch',CAPS)['state']=='dismissed'
        service.set_enabled(pid,False)
        assert service.submit(answer(pid,'a2'),'branch',CAPS)['reason']=='disabled'
    finally:service.close()
    restarted=SuggestionStore(store);restarted.initialize()
    next_record,_=restarted.begin(suggestion_scope(answer(pid,'a3'),'branch'))
    assert next_record['state']=='hidden' and next_record['reason']=='disabled'


def test_other_project_cannot_read_or_accept_cached_suggestion(runtime):
    store,pid=runtime;other=store.create_project('另一个项目','')['id'];service=SuggestionService(store);service.start()
    try:
        record=service.submit(answer(pid),'branch',CAPS);ready=wait_ready(service,pid,record['key'])
        assert service.get(other,ready['key']) is None
        with pytest.raises(ValueError):service.accept(other,ready['key'],ready['selected_id'],ready['scope'])
    finally:service.close()


def document_answer(pid,question,text,metric=None,entities=(),year=None,status='completed'):
    item=answer(pid,status=status,question=question)
    item['result'].pop('columns');item['result'].pop('rows')
    item['result']['text']=text;item['result']['claims'][0]['text']=text
    item['context_state']['slots']={}
    for name,value in [('metric',metric),('entities',list(entities)),('time_range',year)]:
        if value is not None and value!=[]:
            item['context_state']['slots'][name]={'value':value,'verification':'answer_semantic_review','source_answer_id':item['id'],'confirmed_by_user':False}
    if status=='insufficient_evidence':item['result']['verification']['status']='limited'
    return item


def test_document_next_question_is_ask_with_actual_metric_year_and_auditable_grounding(runtime):
    _,pid=runtime
    item=document_answer(pid,'2024年工业增加值是多少？','2024年工业增加值为30.7亿元。',metric='工业增加值',year='2024年')
    before=copy.deepcopy(item)
    candidates=candidate_suggestions(item,CAPS)
    assert candidates[0]['action']=='ask' and '2024年工业增加值' in candidates[0]['text']
    assert candidates[0]['payload']['claim_ids']==['C1'] and candidates[0]['payload']['evidence_ids']==['E1']
    assert candidates[0]['payload']['new_facts_asserted'] is False and item==before
    assert not any(word in candidates[0]['text'] for word in ('2025','上涨','下降','原因','预测'))
    assert any(c['action']=='verify_sources' for c in candidates)


def test_entity_labels_without_comparable_roles_do_not_authorize_comparison(runtime):
    _,pid=runtime
    item=document_answer(pid,'列出甲区和乙区的金额','甲区金额为10元；乙区金额为7元。','金额',['甲区','乙区'])
    asks=[c for c in candidate_suggestions(item,CAPS) if c['action']=='ask']
    assert not any(c['kind']=='compare_verified_entities' for c in asks)
    assert asks[0]['kind']=='inspect_metric_scope' and '金额' in asks[0]['text'] and '3元' not in asks[0]['text']
    item['context_state']['slots']['entities']['value'].append('丙区')
    item['result']['claims'][0]['text']='甲区金额为10元。'
    assert not any(c['kind']=='compare_verified_entities' for c in candidate_suggestions(item,CAPS))


def test_partial_question_uses_only_accepted_fact_not_missing_or_rejected_candidate(runtime):
    _,pid=runtime
    item=document_answer(pid,'甲区金额和乙区金额各是多少？','仅确认甲区金额为10元。','甲区金额',status='insufficient_evidence')
    item['result']['diagnostics']={'candidate_attempts':[{'text':'乙区金额999元，必然增长'}]}
    item['result']['completeness']={'items':[{'id':'Q2','text':'乙区金额','status':'missing_evidence'}]}
    candidates=candidate_suggestions(item,CAPS)
    assert candidates[0]['action']=='ask' and '已确认' in candidates[0]['text'] and '甲区金额' in candidates[0]['text']
    assert '999' not in json.dumps(candidates,ensure_ascii=False)
    assert any(c['kind']=='fill_gap' for c in candidates)


def test_calculation_question_requires_used_source_rule_and_copies_no_hypothetical_parameter(runtime):
    _,pid=runtime;item=document_answer(pid,'能量变化率是多少？','能量变化率为5kW。','能量变化率')
    item['result']['claims'][0]['calculation_ids']=['K1']
    item['result']['calculations']=[{'id':'K1','rule_origin':'source','expression':'(a+b)/c'}]
    asks=[c for c in candidate_suggestions(item,CAPS) if c['action']=='ask']
    assert asks[0]['kind']=='explain_calculation' and asks[0]['payload']['calculation_id']=='K1'
    assert '10' not in asks[0]['text']
    item['result']['calculations'][0]['rule_origin']='model_candidate'
    assert not any(c['kind']=='explain_calculation' for c in candidate_suggestions(item,CAPS))


@pytest.mark.parametrize('mutation',[
    lambda a:a['context_state']['slots']['metric'].update(verification='model_self_report'),
    lambda a:a['context_state']['slots']['metric'].update(value='不存在的指标'),
    lambda a:a['result']['claims'][0].update(evidence_ids=['missing']),
])
def test_document_question_does_not_fabricate_an_anchor_for_display_coverage(runtime,mutation):
    _,pid=runtime;item=document_answer(pid,'金额是多少？','金额为10元。','金额');mutation(item)
    assert not any(c['action']=='ask' for c in candidate_suggestions(item,{'local_actions':CAPS['local_actions']}))


def test_document_ask_is_selected_cached_and_acceptance_never_submits_model_work(runtime):
    store,pid=runtime;item=document_answer(pid,'就业人数是多少？','就业人数为120人。','就业人数')
    service=SuggestionService(store);service.start()
    try:
        record=service.submit(item,'document-branch',CAPS);ready=wait_ready(service,pid,record['key'])
        selected=next(c for c in ready['suggestions'] if c['id']==ready['selected_id'])
        assert selected['action']=='ask'
        accepted=service.accept(pid,ready['key'],selected['id'],ready['scope'])
        assert accepted['accepted_action']=='ask' and accepted['fill_text']==selected['text']
        assert accepted['model_calls']==0 and not service.tasks
    finally:service.close()


@pytest.mark.parametrize('question,text,metric',[
    ('方案是什么性质？','方案尚为征求意见稿，不能认定已经实施。','方案'),
    ('柱高能给精确值吗？','柱高未标注精确值，只能确认高于另一柱。','柱高'),
])
def test_non_statistical_fact_prefers_its_explicit_limits_without_inventing_statistical_scope(runtime,question,text,metric):
    _,pid=runtime;item=document_answer(pid,question,text,metric)
    asks=[c for c in candidate_suggestions(item,CAPS) if c['action']=='ask']
    assert asks[0]['kind']=='inspect_fact_limits'
    assert '已明确' in asks[0]['text'] and metric in asks[0]['text']
    assert not any('统计范围' in c['text'] for c in asks)


def test_formula_symbols_do_not_become_peer_comparison(runtime):
    _,pid=runtime
    item=document_answer(pid,'按原页公式和参数计算 R，并说明单位。',
                         'R 为5kW，a 为12kWh，b 为8kWh，c 为4h。','R',['R','a','b','c'])
    item['result']['claims'][0]['calculation_ids']=['K1']
    item['result']['calculations']=[{'id':'K1','rule_origin':'source','expression':'(a+b)/c',
        'parameters':[{'name':n} for n in ['a','b','c']]}]
    asks=[s for s in candidate_suggestions(item,CAPS) if s['action']=='ask']
    assert asks[0]['kind']=='explain_units'
    assert asks[0]['payload']['calculation_id']=='K1'
    assert not any(s['kind']=='compare_verified_entities' for s in asks)
    item['question']='按原公式说明 R 的单位换算过程。'
    assert not any(s['kind']=='explain_units' for s in candidate_suggestions(item,CAPS))


def test_generator_update_refreshes_ready_but_preserves_dismissal(runtime):
    from backend.suggestion_service import SuggestionStore,suggestion_scope,GENERATOR_VERSION
    store,pid=runtime; cache=SuggestionStore(store);cache.initialize()
    scope=suggestion_scope(answer(pid),'branch')
    initial,_=cache.begin(scope)
    ready=cache.finish(pid,initial['key'],initial['generation'],candidate_suggestions(answer(pid),CAPS))
    with store.transaction() as connection:
        ready['generator_version']='old';cache._save(connection,ready)
    refreshed,started=cache.begin(scope)
    assert started and refreshed['generation']!=ready['generation']
    assert refreshed['generator_version']==GENERATOR_VERSION
    dismissed=cache.transition(pid,refreshed['key'],'dismissed',expected_scope=scope)
    with store.transaction() as connection:
        dismissed['generator_version']='old';cache._save(connection,dismissed)
    kept,started=cache.begin(scope)
    assert not started and kept['state']=='dismissed'


def test_explicit_reenable_restores_disabled_suggestion_not_dismissed(runtime):
    from backend.suggestion_service import SuggestionStore,suggestion_scope
    store,pid=runtime;cache=SuggestionStore(store);cache.initialize()
    scope=suggestion_scope(answer(pid),'branch');item,_=cache.begin(scope)
    ready=cache.finish(pid,item['key'],item['generation'],candidate_suggestions(answer(pid),CAPS))
    cache.set_enabled(pid,False)
    assert cache.get(pid,ready['key'])['reason']=='disabled'
    cache.set_enabled(pid,True)
    renewed,started=cache.begin(scope)
    assert started and renewed['state']=='preparing'
    cache.transition(pid,renewed['key'],'dismissed',expected_scope=scope)
    cache.set_enabled(pid,False);cache.set_enabled(pid,True)
    kept,started=cache.begin(scope)
    assert not started and kept['state']=='dismissed'
