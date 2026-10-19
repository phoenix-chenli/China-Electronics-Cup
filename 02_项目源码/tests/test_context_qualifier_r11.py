"""Real regression projection plus controlled offline planner/host contracts."""
import copy
import json
from pathlib import Path
import pytest
from backend.claim_evidence_scope import missing_requested_qualifiers
from backend.cross_source_engine import _brief_history, _claims, answer_cross_source, CrossSourceError
from backend.conversation_state import context_history
from test_cross_source_engine import Responses, actions, approved
from test_answers_api import app_env, project, upload, request_body, submit, wait_terminal


def test_actual_flat_history_retains_scope_answer_and_clarification_without_aliasing():
    chain=[{'id':'a1','question':'原问','normalized_question':'完整问题','status':'completed',
            'intent':{'unit':'亿元'},'result':{'text':'原回答','rows':[['1222.1']],
            'columns':['增加值'],'claims':[{'text':'原断言'}],'clarification':{'question':'口径？'}}}]
    history=context_history(chain)
    original=copy.deepcopy(history)
    brief=_brief_history(history)
    assert brief[0]['answer']=='原回答' and brief[0]['rows']==[['1222.1']]
    assert brief[0]['clarification']=={'question':'口径？'}
    assert brief[0]['answer_id']=='a1' and brief[0]['intent']=={'unit':'亿元'}
    assert brief[0]['normalized_question']=='完整问题'
    brief[0]['rows'][0][0]='changed'
    assert history==original


def test_nested_history_supported_and_oversized_history_explicitly_rejected():
    assert _brief_history([{'result':{'text':'旧接口','rows':[[3]]}}])[0]['answer']=='旧接口'
    with pytest.raises(CrossSourceError,match='上文较长'):
        _brief_history([{'answer':'字'*14001}])


def test_real_r9_t2_missing_qualifier_is_detected_without_rewriting_its_record():
    fixture=json.loads((Path(__file__).parent/'fixtures/r9-context04-t2-qualifier.json').read_text())
    before=copy.deepcopy(fixture)
    # The standalone question is a regression input authored from the observed
    # AFTER-answer interpretation, not evidence of the original planner input.
    question=fixture['normalized_question_after_answer']
    assert missing_requested_qualifiers(question,fixture['published_claims'])==[
        {'family':'constant_price','text':'不变价格','scope':'explicit_question_condition_not_source_evidence'}]
    assert fixture==before


@pytest.mark.parametrize('question',[
    '是否按不变价格计算？','是不是不变价？','采用不变价格吗？','按现价还是不变价？',
    '不要按不变价格计算','无需使用初步统计数据','预测模型有什么输入？',
    '不变价格口径未知，请说明缺口','未按现价计算，说明原因'])
def test_question_does_not_license_qualifier_assertion(question):
    assert missing_requested_qualifiers(question,[])==[]


def test_qualifier_family_and_current_question_are_exact_not_inferred_from_history():
    assert missing_requested_qualifiers('按可比价格计算增长',[{'text':'按不变价增长。'}])[0]['family']=='comparable_price'
    assert not missing_requested_qualifiers('按现价计算增长',[{'text':'按现行价格增长。'}])
    assert not missing_requested_qualifiers('查看当前金额',[{'text':'数值为10。'}])


def source_and_candidates():
    source=dict(id='s',name='原文.txt',sha256='hash',parser_version='test',format='txt',
        tables=[],warnings=[],metadata={},pages=[{'number':1,'text':'按不变价格计算，增加值增长5%。'}])
    bad={'status':'completed','claims':[{'text':'增加值增长5%。','evidence_ids':['E1']}]}
    good={'status':'completed','claims':[{'text':'按不变价格计算，增加值增长5%。','evidence_ids':['E1']}]}
    return source,bad,good


def test_missing_scope_is_feedback_then_explicit_backed_repair_and_independent_review():
    source,bad,good=source_and_candidates()
    gateway=Responses(actions({'id':'S1','kind':'search','source_id':'s','query':'增加值','depends_on':[]}),bad,good,approved())
    status,result=answer_cross_source([source],'按不变价格计算，增加值增长多少？',[],gateway)
    assert status=='completed' and result['claims'][0]['text']==good['claims'][0]['text']
    assert gateway.calls[2]['payload']['tool_errors'][-1]['kind']=='requested_qualifier_missing'
    assert gateway.calls[-1]['purpose']=='verify'
    assert gateway.calls[-1]['payload']['candidate_claims'][0]['text']==good['claims'][0]['text']
    assert result['diagnostics']['rejections'][0]['kind']=='requested_qualifier_missing'


def test_question_and_old_history_cannot_supply_absent_current_evidence_qualifier():
    _,_,good=source_and_candidates()
    evidence={'E1':dict(id='E1',kind='text',source_id='s',quote='增加值增长5%。',page=1)}
    with pytest.raises(CrossSourceError,match='限定词引用不自足'):
        _claims(good,evidence,{},question='按不变价格计算，增长多少？')


def test_fixed_scope_still_subject_to_independent_semantic_veto():
    source,bad,good=source_and_candidates()
    denied=approved(supported=False,claims=[{'id':'C1','supported':False}])
    gateway=Responses(actions({'id':'S1','kind':'search','source_id':'s','query':'增加值','depends_on':[]}),
        bad,good,denied,good,denied,good,denied)
    status,result=answer_cross_source([source],'按不变价格计算，增加值增长多少？',[],gateway)
    assert status!='completed' and not result['claims']
    assert any(call['purpose']=='verify' for call in gateway.calls)


@pytest.mark.parametrize('resolution_status',['ready','needs_clarification'])
def test_short_cross_followup_resolved_before_planning_and_unresolved_not_executed(app_env,monkeypatch,resolution_status):
    client,gateway,_=app_env
    pid=project(client); detail=upload(client,pid)
    calls=[]
    resolved='2024年第三产业按不变价格计算的同比增速是多少？'
    def fake_cross(sources,question,history,*args,**kwargs):
        calls.append(('cross',question,copy.deepcopy(history)))
        return 'completed',{'text':'受控测试回答','claims':[],'evidence':[],'verification':{'status':'passed','checks':[]}}
    def fake_resolution(item,history,source,*args,**kwargs):
        calls.append(('resolve',item['question'],copy.deepcopy(history)))
        assert history and history[0]['answer']=='受控测试回答'
        return {'status':resolution_status,'normalized_question':resolved,'intent':{'metric':'同比增速'},
            'message':'需要选择口径','clarification':{'question':'选择口径','options':['不变价格','现价'],'field':'口径'}}
    monkeypatch.setattr('backend.cross_source_engine.answer_cross_source',fake_cross)
    monkeypatch.setattr('backend.query_planner.resolve_followup',fake_resolution)
    monkeypatch.setattr('backend.context_intent_adapter.adapt_answer_intent',lambda *a,**k:{'status':'unavailable'})
    first=wait_terminal(client,pid,submit(client,pid,request_body(detail,mode='multi_source',table_name=None))['id'])
    assert first['status']=='completed'
    second=wait_terminal(client,pid,submit(client,pid,request_body(detail,mode='multi_source',table_name=None,
        question='改看第三产业',parent_answer_id=first['id']))['id'])
    assert [c[0] for c in calls]==(['cross','resolve','cross'] if resolution_status=='ready' else ['cross','resolve'])
    assert second['normalized_question']==resolved
    if resolution_status=='ready':
        assert calls[-1][1]==resolved and second['status']=='completed'
    else:
        assert second['status']=='needs_clarification'
    assert not gateway.calls
