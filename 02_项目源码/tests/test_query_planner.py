"""Offline planner integration: actual SQLite execution, explicit model stubs."""
import json
import sqlite3
import threading

import pytest
from backend.parsers import sqlite_tables
from backend.query_planner import answer_database, resolve_followup, unresolved_result
from backend.sql_engine import SQLExecutionError
from backend.model_gateway import ModelError


@pytest.fixture
def source(tmp_path):
    p=tmp_path/'orders.sqlite'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE buyer(id INTEGER PRIMARY KEY,name TEXT); CREATE TABLE sale(id INTEGER PRIMARY KEY,buyer_id INTEGER REFERENCES buyer(id),amount TEXT); INSERT INTO buyer VALUES(1,'同名'),(2,'同名'),(3,'另一人'); INSERT INTO sale VALUES(1,1,'0.1'),(2,1,'0.2'),(3,2,'4.2');")
    tables,warnings=sqlite_tables(p)
    return dict(id='source',name='orders.sqlite',data_path=str(p),tables=tables,warnings=warnings,sha256='fixture-hash',parser_version='test')


class Responses:
    def __init__(self,*values):self.values=list(values);self.calls=[]
    def __call__(self,messages,purpose):
        self.calls.append((purpose,json.loads(messages[-1]['content'])))
        return self.values.pop(0)


def plan(sql,**kw):return dict(status='ready',normalized_question='按客户统计销售额',intent={'metric':'销售额'},sql=sql,parameters=[],**kw)


def test_join_executes_and_evidence_is_table_level(source):
    g=Responses(plan('SELECT b.id,DECIMAL_SUM(s.amount) AS amount FROM buyer b JOIN sale s ON b.id=s.buyer_id GROUP BY b.id'),{'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    interpretations=[]
    status,result=answer_database(source,'按客户统计销售额',[],g,interpretation=lambda q,i:interpretations.append((q,i)))
    assert status=='completed' and result['rows']==[[1,'0.3'],[2,'4.2']]
    assert [p for p,_ in g.calls]==['table','verify']
    assert {e['table_name'] for e in result['evidence']}=={'buyer','sale'}
    assert all(e['lineage']=='table' and not e['row_numbers'] for e in result['evidence'])
    assert interpretations[0][0]=='按客户统计销售额'


def test_clarification_has_independent_review_without_query(source):
    clarification={'status':'needs_clarification','normalized_question':'同名客户的金额','clarification':{'question':'需要哪位客户？','options':['客户1','客户2'],'field':'customer_id'}}
    g=Responses(clarification,clarification)
    status,result=answer_database(source,'同名客户的金额',[],g)
    assert status=='needs_clarification' and not result.get('rows')
    assert result['clarification']['options']==['客户1','客户2'] and len(g.calls)==2


def test_missing_clarification_detail_gets_resumable_default_shape():
    status,result=unresolved_result({'status':'needs_clarification','message':'请补充统计口径'})
    assert status=='needs_clarification'
    assert result['clarification']=={'question':'请补充统计口径','options':[],'field':''}


def test_table_numeric_execution_error_is_not_a_missing_user_condition(source,monkeypatch):
    from backend.answer_service import AnswerService
    from backend.table_engine import TablePlanError
    import backend.table_engine as table_engine

    source.update(status='ready',archived=False)
    def fail(*_args,**_kwargs):
        raise TablePlanError('分母为0，占比没有定义',True)
    monkeypatch.setattr(table_engine,'execute_plan',fail)
    gateway=Responses({'status':'ready','plan':{'table':'buyer','select':['id'],'limit':10}})
    status,result=AnswerService._table(None,source,{'question':'计算占比','table_name':'buyer'},gateway,threading.Event(),lambda _stage:None)
    assert status=='failed'
    assert 'clarification' not in result
    assert '分母为0' in result['text']


@pytest.mark.parametrize('bad_plan,error_fragment', [
    ({'table':'sale','select':['missing_field'],'limit':10},'结果列无效'),
    ({'table':'sale','ratio':{'numerator':{'column':'amount','filters':[]},'denominator':{'column':'amount','filters':[{'column':'buyer_id','op':'eq','value':99}]}},'limit':10},'分子或分母'),
])
def test_real_invalid_table_plan_does_not_prompt_user_to_restate_question(source,bad_plan,error_fragment):
    from backend.answer_service import AnswerService
    source.update(status='ready',archived=False)
    gateway=Responses({'status':'ready','plan':bad_plan})
    status,result=AnswerService._table(None,source,{'question':'列出金额','table_name':'sale'},gateway,threading.Event(),lambda _:None)
    assert status=='failed' and 'clarification' not in result and not result.get('rows')
    assert error_fragment in result['text']
    assert len(gateway.calls)==1


def test_real_zero_denominator_has_no_fabricated_result_or_clarification(source):
    from backend.answer_service import AnswerService
    with sqlite3.connect(source['data_path']) as connection:
        connection.execute("INSERT INTO sale VALUES(4,3,'0')")
    source.update(status='ready',archived=False)
    source['tables'],_=sqlite_tables(source['data_path'])
    gateway=Responses({'status':'ready','plan':{'table':'sale',
        'ratio':{'numerator':{'column':'amount','filters':[{'column':'buyer_id','op':'eq','value':1}]},
                 'denominator':{'column':'amount','filters':[{'column':'buyer_id','op':'eq','value':3}]}},'limit':10}})
    status,result=AnswerService._table(None,source,{'question':'客户1销售额与客户3的比值','table_name':'sale'},gateway,threading.Event(),lambda _:None)
    assert status=='failed' and '分母为0' in result['text']
    assert not result.get('rows') and 'clarification' not in result and len(gateway.calls)==1


def test_table_semantic_rejection_without_specific_missing_condition_fails(source):
    from backend.answer_service import AnswerService

    source.update(status='ready',archived=False)
    gateway=Responses(
        {'status':'ready','plan':{'table':'buyer','select':['name'],'limit':10}},
        {'supported':False,'reason':'统计对象不明确'},
    )
    status,result=AnswerService._table(None,source,{'question':'列出客户','table_name':'buyer'},gateway,threading.Event(),lambda _stage:None)
    assert status=='failed' and not result.get('rows')
    assert 'clarification' not in result
    assert [purpose for purpose,_ in gateway.calls]==['table','verify']


def test_entity_inspection_is_bounded_and_retained(source):
    g=Responses({'status':'inspect','queries':[{'purpose':'同名对象','sql':'SELECT id,name FROM buyer WHERE name=?','parameters':['同名']}]},plan('SELECT amount FROM sale WHERE buyer_id=?'),{'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    g.values[1]['parameters']=[1]
    status,result=answer_database(source,'客户1的金额',[],g)
    assert status=='completed'
    assert len(g.calls[1][1]['inspections'][0]['rows'])==2
    assert result['query']['inspections'][0]['parameters']==['同名']


def test_one_local_column_repair_and_no_transport_retry(source):
    g=Responses(plan('SELECT "ammount" FROM sale'),plan('SELECT amount FROM sale'),{'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    status,result=answer_database(source,'金额',[],g)
    assert status=='completed' and len(result['query']['repairs'])==1
    assert 'ammount' in result['query']['repairs'][0]['sql']
    g=Responses(plan('SELECT "ammount" FROM sale'),plan('SELECT "ammount" FROM sale'))
    with pytest.raises(SQLExecutionError):answer_database(source,'金额',[],g)
    assert len(g.calls)==2


def test_forbidden_query_is_not_repaired_or_verified(source):
    g=Responses(plan('DELETE FROM sale'))
    with pytest.raises(SQLExecutionError,match='只允许'):answer_database(source,'删除记录',[],g)
    assert len(g.calls)==1
    with sqlite3.connect(source['data_path']) as c:assert c.execute('SELECT COUNT(*) FROM sale').fetchone()[0]==3


def test_constant_answer_cannot_claim_source_evidence(source):
    g=Responses(plan('SELECT 100 AS amount'))
    status,result=answer_database(source,'金额',[],g)
    assert status=='insufficient_evidence' and not result['evidence'] and len(g.calls)==1


def test_failed_semantic_check_hides_unverified_numbers(source):
    g=Responses(plan('SELECT COUNT(*) FROM sale'),{'supported':False,'reason':'把销售笔数误认为客户数'})
    status,result=answer_database(source,'客户数',[],g)
    assert status=='failed' and not result.get('rows')
    assert 'clarification' not in result
    assert [purpose for purpose,_ in g.calls]==['table','verify']


def test_no_rows_is_not_zero(source):
    g=Responses(plan('SELECT amount FROM sale WHERE buyer_id=99'),{'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    status,result=answer_database(source,'客户99的金额',[],g)
    assert status=='completed' and result['rows']==[] and '不表示' in result['text']


def test_schema_linking_cannot_select_unknown_table(source):
    with sqlite3.connect(source['data_path']) as c:
        for i in range(17):c.execute(f'CREATE TABLE distraction_{i}(id INTEGER)')
    source['tables'],_=sqlite_tables(source['data_path'])
    g=Responses({'tables':['missing']})
    with pytest.raises(ModelError,match='有效的业务表'):answer_database(source,'金额',[],g)
    assert len(g.calls)==1


def test_followup_passes_exact_history_scope_and_rejects_empty_resolution(source):
    history=[{'question':'金额','clarification':{'question':'哪位客户？','options':['客户1','客户2']}}]
    item={'question':'客户1','mode':'table','table_name':'sale'}
    g=Responses({'status':'ready','normalized_question':'','intent':{}})
    with pytest.raises(ModelError,match='完整问题'):resolve_followup(item,history,source,g)
    assert g.calls[0][1]['history']==history
    assert g.calls[0][1]['scope']['table']=='sale'


def test_repair_refreshes_interpretation_and_does_not_publish_freeform_assumptions(source):
    second=plan('SELECT amount FROM sale',assumptions=['税率20%，已由业务确认'])
    second['normalized_question']='销售明细金额';second['intent']={'metric':'明细金额'}
    g=Responses(plan('SELECT "ammount" FROM sale'),second,{'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    interpreted=[]
    status,result=answer_database(source,'金额',[],g,interpretation=lambda q,i:interpreted.append((q,i)))
    assert interpreted[-1]==('销售明细金额',{'metric':'明细金额'})
    assert result['query']['plan']['intent']=={'metric':'明细金额'}
    assert g.calls[-1][1]['normalized_question']=='销售明细金额'
    assert '税率' not in json.dumps(result,ensure_ascii=False)


def test_later_selected_table_is_in_followup_schema(source):
    source['tables']=[{'name':f't{i}','columns':[{'name':f'col{i}'}]} for i in range(20)]
    g=Responses({'status':'ready','normalized_question':'t19问题','intent':{}},{'supported':True})
    resolve_followup({'question':'继续','mode':'table','table_name':'t19'},[],source,g)
    assert [t['name'] for t in g.calls[0][1]['tables']]==['t19']


def test_twelfth_result_is_retained_for_reference_resolution():
    from backend.query_planner import conversation_context
    rows=[[i,f'客户{i}'] for i in range(12)]
    context=conversation_context([{'question':'前12名','status':'completed','result':{'rows':rows}}])
    assert context[0]['rows']==rows and context[0]['result_truncated'] is False


def test_independent_verifier_corrects_misleading_interpretation(source):
    wrong=plan('SELECT buyer_id,DECIMAL_SUM(amount) FROM sale GROUP BY buyer_id')
    wrong['normalized_question']='每个客户取前5条'
    corrected={'normalized_question':'按客户汇总金额','intent':{'metric':'金额','grain':'客户'}}
    g=Responses(wrong,{'supported':True,'interpretation_supported':False,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True},'corrected_interpretation':corrected})
    interpreted=[]
    status,result=answer_database(source,'按客户汇总金额',[],g,interpretation=lambda q,i:interpreted.append((q,i)))
    assert status=='completed' and interpreted[-1][0]=='按客户汇总金额'
    assert result['query']['plan']['intent']['grain']=='客户'
    assert result['query']['plan']['normalized_question']==interpreted[-1][0]=='按客户汇总金额'
    assert 'interpretation_correction' in result['query']


@pytest.mark.parametrize('path', ['followup', 'repair'])
def test_negative_followup_and_repair_cannot_bypass_review(source,path):
    incorrect={'status':'insufficient_evidence','message':'数据仅覆盖1996年'}
    reviewed={'status':'needs_clarification','clarification':{'question':'请说明流失定义和比较年份。','options':[]}}
    history=[{'question':'流失客户数量','status':'needs_clarification'}] if path=='followup' else []
    g=Responses(incorrect,reviewed) if path=='followup' else Responses(plan('SELECT "ammount" FROM sale'),incorrect,reviewed)
    status,result=answer_database(source,'那1997年呢',history,g)
    assert status=='needs_clarification' and '1996' not in result['text']
    assert g.calls[-1][0]=='verify' and g.calls[-1][1]['history']==history
    assert not result.get('rows')


def test_rejected_inspection_is_not_retried_or_treated_as_missing_data(source):
    proposed={'status':'insufficient_evidence','message':'不存在客户资料'}
    reviewed={'status':'needs_clarification','clarification':{'question':'需要哪位客户？','options':['客户1','客户2']}}
    g=Responses({'status':'inspect','queries':[{'sql':"SELECT * FROM pragma_table_info('buyer')"},{'sql':'DELETE FROM buyer'}]},proposed,reviewed)
    status,result=answer_database(source,'同名客户的金额',[],g)
    assert status=='needs_clarification' and len(g.calls)==3
    rejected=g.calls[1][1]['inspections']
    assert len(rejected)==1 and rejected[0]['error_code']=='forbidden' and rejected[0]['rows'] is None
    assert result['query_diagnostics']['inspections']==rejected and '不存在' not in result['text']
    assert 'query' not in result
    with sqlite3.connect(source['data_path']) as c:assert c.execute('SELECT COUNT(*) FROM buyer').fetchone()[0]==3


def test_same_name_clarification_offers_actual_ids_when_model_options_are_objects(source):
    g=Responses(plan("SELECT DECIMAL_SUM(amount) FROM sale JOIN buyer ON buyer.id=sale.buyer_id WHERE buyer.name='同名'"),
        {'supported':False,'clarification':{'question':'需要哪位同名客户？','field':'buyer.id','options':[{'label':'随意编号99','value':99}]}})
    status,result=answer_database(source,'同名客户的金额',[],g)
    assert status=='needs_clarification' and not result.get('rows')
    assert result['clarification']['options']==['选择id=1（同名），其他条件不变。','选择id=2（同名），其他条件不变。']


def test_entity_choices_do_not_override_unrelated_clarification():
    from backend.query_planner import add_entity_choices
    result={'text':'年份？','clarification':{'field':'year','question':'年份？','options':['2024年','2025年']}}
    binding={'status':'checked','table':'buyer','matched_rows':2,'primary_key':['id'],'candidate_columns':['id','name'],'candidates':[[1,'同名'],[2,'同名']]}
    add_entity_choices(result,[binding])
    assert result['clarification']['options']==['2024年','2025年']


def test_entity_choices_mark_partial_candidates_and_keep_composite_ids():
    from backend.query_planner import add_entity_choices
    result={'text':'哪一条？','clarification':{'field':'part.id','question':'哪一条？','options':[]}}
    binding={'status':'checked','table':'part','matched_rows':7,'primary_key':['id','revision'],'candidate_columns':['id','revision','name'],'candidates':[[i,2,'名称'] for i in range(6)]}
    add_entity_choices(result,[binding])
    assert len(result['clarification']['options'])==4
    assert 'id=0，revision=2' in result['clarification']['options'][0]
    assert '共7项' in result['text']


@pytest.mark.parametrize('failure_kind', [None, 'missing_condition'])
def test_specific_legacy_or_classified_missing_condition_can_clarify(source,failure_kind):
    from backend.answer_service import AnswerService
    source.update(status='ready',archived=False)
    check={'supported':False,'reason':'问题未指定需要哪位同名客户',
           'clarification':{'question':'需要客户编号1还是2？','field':'buyer.id','options':['1','2']}}
    if failure_kind:check['failure_kind']=failure_kind
    gateway=Responses({'status':'ready','plan':{'table':'buyer','select':['name'],'limit':10}},check)
    status,result=AnswerService._table(None,source,{'question':'同名客户','table_name':'buyer'},gateway,threading.Event(),lambda _:None)
    assert status=='needs_clarification'
    assert result['clarification']['question']=='需要客户编号1还是2？'
    assert len(gateway.calls)==2 and not result.get('rows')


@pytest.mark.parametrize('check', [
    {'failure_kind':'invalid_plan','clarification':{'question':'需要哪位客户？','field':'buyer.id'}},
    {'failure_kind':'missing_condition','clarification':{'question':'请明确统计对象、指标、分组和筛选条件后再试。','field':'统计口径'}},
    {'reason':'查询遗漏用户已明确的年份条件','clarification':{'question':'需要哪一年？','field':'year'}},
    {'failure_kind':'missing_condition','clarification':{'question':'需要哪一年？'}},
])
def test_rejected_query_does_not_turn_model_errors_or_generic_prompts_into_clarification(source,check):
    values=[plan('SELECT COUNT(*) FROM sale'),{'supported':False,**check}]
    if check.get('failure_kind')=='invalid_plan':values.append({'status':'failed'})
    gateway=Responses(*values)
    status,result=answer_database(source,'2024年的客户数',[],gateway)
    assert status=='failed' and 'clarification' not in result and not result.get('rows')
    assert len(gateway.calls)==(3 if check.get('failure_kind')=='invalid_plan' else 2)


def test_unrepairable_interpretation_is_failure_not_user_clarification(source):
    gateway=Responses(plan('SELECT COUNT(*) FROM sale'),{'supported':True,'interpretation_supported':False,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    status,result=answer_database(source,'销售笔数',[],gateway)
    assert status=='failed' and 'clarification' not in result and not result.get('rows')
    assert len(gateway.calls)==2


def test_followup_rewrite_error_does_not_request_already_given_conditions(source):
    history=[{'question':'统计2024年销售额','status':'completed'}]
    gateway=Responses({'status':'ready','normalized_question':'所有年份销售额','intent':{}},
                      {'supported':False,'failure_kind':'invalid_plan','reason':'改写遗漏原年份'})
    response=resolve_followup({'question':'按客户分组','mode':'table','table_name':'sale'},history,source,gateway)
    assert response['status']=='failed' and 'clarification' not in response
    assert unresolved_result(response)[0]=='failed' and len(gateway.calls)==2
