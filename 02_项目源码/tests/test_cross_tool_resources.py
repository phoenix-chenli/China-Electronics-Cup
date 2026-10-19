"""Immutable read aliases and resource boundaries. All gateways are offline."""
import copy
import json
import pytest
from backend import cross_source_engine as engine
from backend.reasoning_tools import ReasoningLimits
from test_cross_source_engine import sources, actions, search, sql, approved
from test_cross_protocol_recovery import Scripted

def candidate(text='林禾适用优惠规则。'):
    return {'status':'completed','claims':[{'text':text,'evidence_ids':['E1']}]}

@pytest.mark.parametrize('same_batch',[True,False])
def test_new_ids_reuse_search_and_preserve_dependency_alias(sources,monkeypatch,same_batch):
    original=engine._retrieve;reads=[]
    def retrieve(*a,**kw):reads.append(1);return original(*a,**kw)
    monkeypatch.setattr(engine,'_retrieve',retrieve)
    a,b=search('S1'),search('Alias')
    vals=[actions(a,b)] if same_batch else [actions(a),actions(b)]
    g=Scripted(*vals,candidate(),approved())
    status,r=engine.answer_cross_source(sources,'林禾是否适用规则？',[],g,limits=ReasoningLimits(max_actions=1))
    assert status=='completed' and len(reads)==1 and r['verification']['tool_actions']==1
    assert len(r['evidence'])==1
    alias=next(s for s in r['steps'] if s['id']=='Alias')
    assert alias['reused_from']=='S1' and alias['depends_on']==['S1']
    assert alias['action_budget_charged']==0 and not alias['result_reexecuted']
    assert g.calls[-2]['payload']['remaining_actions']==0

def test_sql_alias_is_usable_by_later_parameter_dependency(sources,monkeypatch):
    original=engine.execute_sql;executions=[]
    def execute(*a,**kw):executions.append(copy.deepcopy(a[2]));return original(*a,**kw)
    monkeypatch.setattr(engine,'execute_sql',execute)
    dependent=sql('Last',sql='SELECT amount FROM orders WHERE customer=?',
        parameters=[{'from_step':'Alias','row':0,'column':'customer'}],depends_on=['Alias'])
    g=Scripted(actions(sql('S1'),sql('Alias')),actions(dependent),candidate('客户为林禾。'),approved())
    status,r=engine.answer_cross_source(sources,'客户是谁？',[],g,limits=ReasoningLimits(max_actions=2))
    assert status=='completed' and executions==[[1],['林禾']]
    assert r['steps'][1]['depends_on']==['S1'] and r['steps'][2]['depends_on']==['Alias']
    assert r['verification']['tool_actions']==2

def test_more_than_ten_distinct_actions_can_still_publish_and_be_reviewed(sources):
    queries=[sql(f'S{i}',sql='SELECT customer,amount FROM orders WHERE id=? LIMIT ?',parameters=[1,i]) for i in range(1,12)]
    g=Scripted(actions(*queries),candidate('客户为林禾。'),approved())
    status,r=engine.answer_cross_source(sources,'客户是谁？',[],g)
    assert status=='completed' and r['verification']['tool_actions']==11
    assert g.calls[-1]['purpose']=='verify'
    assert g.calls[0]['payload']['tool_limits']['max_actions']==32

def test_explicit_low_limit_records_exact_batch_boundary(sources):
    planned=[{**search(f'S{i}'),'query':f'条件{i}'} for i in range(3)]
    g=Scripted(actions(*planned));audit=[]
    status,r=engine.answer_cross_source(sources,'规则？',[],g,limits=ReasoningLimits(max_actions=2),audit=audit.append)
    assert status=='insufficient_evidence' and not r['steps']
    detail=r['diagnostics']['budget_limits'][-1]
    assert detail['resource']=='tool_action_batch' and detail['actions_used']==0
    assert detail['actions_limit']==2 and detail['requested_action_count']==3
    assert [x['id'] for x in detail['proposed_actions']]==['S0','S1','S2']
    assert not detail['source_absence_proven'] and audit

def test_local_context_limit_is_explicit_without_gateway_request(sources,monkeypatch):
    monkeypatch.setattr(engine,'MAX_CONTEXT',1)
    g=Scripted()
    status,r=engine.answer_cross_source(sources,'规则？',[],g)
    assert status=='insufficient_evidence' and not g.calls
    detail=r['diagnostics']['budget_limits'][-1]
    assert detail['resource']=='model_context' and detail['requested_action_count']==0
    assert detail['local_diagnostic']['request_sent'] is False

def test_renamed_replay_never_resets_no_progress(sources,monkeypatch):
    original=engine._retrieve;reads=[]
    def retrieve(*a,**kw):reads.append(1);return original(*a,**kw)
    monkeypatch.setattr(engine,'_retrieve',retrieve)
    g=Scripted(*[actions(search(f'S{i}')) for i in range(4)])
    status,r=engine.answer_cross_source(sources,'规则？',[],g)
    assert status=='insufficient_evidence' and r['verification']['reason']=='no_progress'
    assert r['verification']['planning_progress']['consecutive_no_progress']==3
    assert r['verification']['tool_actions']==1 and len(reads)==1 and len(r['steps'])==4


def test_planner_accepts_multisource_structure_context_but_overflow_never_dispatches():
    # These are explicit capacity fixtures, not model facts. Real row contexts
    # repeat headers and provenance, so their cost exceeds the short cell text.
    payload={'sources':[{'id':f's{i}','catalog_projection':'导航'*990} for i in range(12)],
             'evidence':[{'id':f'E{i}','quote':'原文'*180,
                          'table_context':{'headers_and_provenance':'结构'*2650}}
                         for i in range(10)]}
    assert 24000 < sum(engine.evidence_cost(e) for e in payload['evidence']) < engine.MAX_EVIDENCE_CHARACTERS
    assert 65000 < len(json.dumps(payload,ensure_ascii=False)) < engine.MAX_CONTEXT
    gateway=Scripted({'status':'insufficient_evidence'})
    response=engine._ask(gateway,'offline capacity fixture',payload,'table',[],lambda:False)
    assert response['status']=='insufficient_evidence' and len(gateway.calls)==1
    overflowing=copy.deepcopy(payload);overflowing['extra']='x'*engine.MAX_CONTEXT
    with pytest.raises(engine.CrossSourceError) as caught:
        engine._ask(gateway,'offline capacity fixture',overflowing,'table',[],lambda:False)
    assert len(gateway.calls)==1
    assert caught.value.details['kind']=='local_context_limit'
    assert caught.value.details['request_sent'] is False
