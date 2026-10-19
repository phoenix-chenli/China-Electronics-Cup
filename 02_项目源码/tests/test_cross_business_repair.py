"""Scripted responses against real temporary SQLite; no model/network calls."""
import json
import sqlite3
import copy
import pytest
from backend.cross_source_engine import answer_cross_source
from backend.parsers import sqlite_tables
from backend.reasoning_tools import ReasoningLimits

@pytest.fixture
def source(tmp_path):
    p=tmp_path/'data.sqlite'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE data(period TEXT,object TEXT,quantity TEXT,price TEXT,quantity_unit TEXT,price_unit TEXT,tax TEXT)')
        c.executemany('INSERT INTO data VALUES(?,?,?,?,?,?,?)',[
            ('2025-07-01','全站','2610','0.65','kWh','元/kWh','不含税'),
            ('2025-07-02','全站','2400','0.70','kWh','元/kWh','不含税')])
    tables,warnings=sqlite_tables(p)
    return {'id':'s','name':'data.csv','format':'csv','data_path':str(p),'sha256':'sourcehash','parser_version':'test','tables':tables,'warnings':warnings,'metadata':{}}

def sql(id='S1',period='2025-07-01'):
    return {'actions':[{'id':id,'kind':'sql','source_id':'s','sql':'SELECT * FROM data WHERE object=? AND period=?','parameters':['全站',period]}]}

def calculation():
    return {'actions':[{'id':'S2','kind':'calculate','expression':'q*p','bindings':{
        'q':{'evidence_id':'E1','row':0,'column':'quantity','unit':'kWh','unit_column':'quantity_unit'},
        'p':{'evidence_id':'E1','row':0,'column':'price','unit':'元/kWh','unit_column':'price_unit'}},
        'output_unit':'元','operation':{'kind':'quantity_times_unit_price','pairs':[{'quantity':'q','unit_price':'p'}]}}]}

def review(ok=True):
    return {'supported':True,'answer_complete':True,'conflict':False,'claims':[{'id':'C1','supported':True}],
        'coverage':[{'question_id':'Q1','status':'answered','claim_ids':['C1']}],
        'operation_relations':[{'id':'K1','intent_supported':True,'parameter_roles_supported':True,'scope_supported':True,'pricing_supported':ok}]}

class Gateway:
    def __init__(self,values): self.values=list(values);self.calls=[]
    def __call__(self,messages,role):
        self.calls.append((role,json.loads(messages[-1]['content'])))
        return self.values.pop(0)

def test_real_sql_units_operation_and_independent_review(source):
    g=Gateway([sql(),calculation(),{'status':'completed','claims':[{'text':'全站不含税电费为1696.50元。','evidence_ids':['E1'],'calculation_ids':['K1']}]},review()])
    status,r=answer_cross_source([source],'计算2025年7月1日全站不含税电费。',[],g)
    assert status=='completed' and r['calculations'][0]['rule_origin']=='user_operation'
    assert r['verification']['operation_relations'][0]['pricing_supported'] is True
    payload=g.calls[-1][1]
    assert payload['user_operation_calculation_ids']==['K1']
    assert payload['calculations'][0]['operation_authorization']['source_formula_claimed'] is False

def test_false_operation_review_cannot_publish_even_overall_claim_true(source):
    g=Gateway([sql(),calculation(),{'status':'completed','claims':[{'text':'电费为1696.50元。','evidence_ids':['E1'],'calculation_ids':['K1']}]},review(False)])
    status,r=answer_cross_source([source],'计算全站电费。',[],g,limits=ReasoningLimits(max_no_progress_rounds=1))
    assert status!='completed' and r['claims']==[] and r['calculations']==[]
    assert any(e['kind']=='operation_review' for e in r['diagnostics']['tool_errors'])

def test_same_error_feedback_requires_changed_plan(source):
    bad=calculation();bad['actions'][0].pop('operation')
    g=Gateway([sql(),bad,copy.deepcopy(bad),copy.deepcopy(bad)])
    status,r=answer_cross_source([source],'计算全站电费。',[],g)
    assert status!='completed'
    errs=[e for e in r['diagnostics']['tool_errors'] if e['kind']=='calculate']
    assert [e['same_local_error_count'] for e in errs]==[1,2,3]
    assert 'quantity_times_unit_price' in g.calls[2][1]['tool_errors'][-1]['repair_guidance']

def test_repeated_different_step_ids_do_not_create_progress(source):
    class Repeat:
        def __init__(self):self.n=0
        def __call__(self,m,r):self.n+=1;return sql('S'+str(self.n),'2025年7月1日')
    g=Repeat();status,r=answer_cross_source([source],'查7月1日全站记录',[],g)
    assert status!='completed' and r['verification']['reason']=='no_progress'
    assert len(r['evidence'])==1 and r['verification']['tool_actions']==2
    assert g.n==4
    hints=r['steps'][0]['query']['empty_result_inspection']['observed_date_spellings']
    assert hints[0]['observed_equivalent_spellings']==['2025-07-01']
    assert all(s.get('reused_from')=='S1' for s in r['steps'][1:])

def test_empty_date_feedback_can_lead_to_actual_new_query(source):
    g=Gateway([sql('S1','2025年7月1日'),sql('S2','2025-07-01'),
        {'status':'completed','claims':[{'text':'全站用电量为2610kWh。','evidence_ids':['E2']}]},
        {'supported':True,'answer_complete':True,'conflict':False,'claims':[{'id':'C1','supported':True}],
         'coverage':[{'question_id':'Q1','status':'answered','claim_ids':['C1']}]}])
    status,r=answer_cross_source([source],'查2025年7月1日全站用电量',[],g)
    assert status=='completed' and r['steps'][0]['query']['rows']==[]
    assert r['steps'][1]['query']['rows'][0][2]=='2610'
    assert len(r['evidence'])==2

def test_unknown_post_outcome_propagates_once(source):
    class Transport(Gateway):
        def __call__(self,messages,role):
            if role=='verify':
                self.calls.append((role,{}))
                raise TimeoutError('unknown POST outcome')
            return super().__call__(messages,role)
    g=Transport([sql(),calculation(),{'status':'completed','claims':[{'text':'电费1696.50元。','evidence_ids':['E1'],'calculation_ids':['K1']}]}])
    with pytest.raises(TimeoutError,match='unknown POST'):
        answer_cross_source([source],'计算电费。',[],g)
    assert len([c for c in g.calls if c[0]=='verify'])==1

def test_cancel_during_empty_result_inspection_never_replans(source,monkeypatch):
    import backend.cross_source_engine as engine
    real=engine.build_catalog;cancel=[False]
    def inspect(*args,**kwargs):
        result=real(*args,**kwargs)
        if kwargs.get('table_names'):cancel[0]=True
        return result
    monkeypatch.setattr(engine,'build_catalog',inspect)
    g=Gateway([sql(period='2025年7月1日')])
    with pytest.raises(InterruptedError):answer_cross_source([source],'查7月1日',[],g,cancelled=lambda:cancel[0])
    assert len(g.calls)==1

def test_result_context_budget_does_not_leave_half_evidence(source,monkeypatch):
    import backend.cross_source_engine as engine
    monkeypatch.setattr(engine,'MAX_EVIDENCE_CHARACTERS',128)
    g=Gateway([sql()])
    status,r=answer_cross_source([source],'查用电量',[],g,limits=ReasoningLimits(max_no_progress_rounds=1))
    assert status!='completed' and r['evidence']==[] and r['steps']==[]
    assert '证据预算' in r['diagnostics']['tool_errors'][0]['message']
