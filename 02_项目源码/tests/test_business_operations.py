import copy
import pytest
from backend.formula_engine import calculate_formula, FormulaError, infer_sql_output_units, infer_sql_direct_columns
from backend.business_operations import review_passed, required_calculations

@pytest.fixture
def data():
    source={'tables':[{'name':'数据表','columns':[{'name':s} for s in ['日期','对象','用电量','电价','计量单位','价格单位']]}]}
    sql='SELECT 日期,对象,用电量,电价,计量单位,价格单位 FROM 数据表 WHERE 对象=? AND 日期=?'
    cols=['日期','对象','用电量','电价','计量单位','价格单位']
    record={'id':'E1','kind':'table','original_columns':cols,
        'query_result':{'columns':cols,'rows':[['2025-07-01','全站','2610','0.65','kWh','元/kWh']]},
        'query':{'sql':sql,'parameters':['全站','2025-07-01']},'direct_columns':infer_sql_direct_columns(source,sql,cols)}
    b={'q':{'evidence_id':'E1','row':0,'column':'用电量','unit':'kWh','unit_column':'计量单位'},
       'p':{'evidence_id':'E1','row':0,'column':'电价','unit':'元/kWh','unit_column':'价格单位'}}
    return source,{'E1':record},b

def calculate(data, **kwargs):
    _,e,b=data
    return calculate_formula(kwargs.pop('expression','q*p'),kwargs.pop('bindings',b),kwargs.pop('evidence',e),kwargs.pop('output_unit','元'),
        user_formula=kwargs.pop('question','按对应电价计算全站不含税电费。'),operation=kwargs.pop('operation',{'kind':'quantity_times_unit_price','pairs':[{'quantity':'q','unit_price':'p'}]}),**kwargs)

def test_observed_case_without_fabricated_source_formula(data):
    r=calculate(data)
    assert r['display_value']=='1696.50'
    assert r['rule_origin']=='user_operation' and r['rule_evidence_ids']==[]
    assert r['operation_authorization']['source_formula_claimed'] is False
    assert r['parameters'][0]['binding']['unit_proof']['value']=='kWh'
    assert not review_passed({},['K1'])
    assert review_passed({'operation_relations':[{'id':'K1','intent_supported':True,'parameter_roles_supported':True,'scope_supported':True,'pricing_supported':True}]},['K1'])

@pytest.mark.parametrize('expression',['q*p*2','q+p','q/p','q*p+1','p-q','q*p-q*p'])
def test_no_arbitrary_model_formula(data,expression):
    with pytest.raises(FormulaError):calculate(data,expression=expression)

@pytest.mark.parametrize('question',['说明电价','电费是多少','不要计算电费，只说明原文','不用计算费用'])
def test_no_unrequested_operation(data,question):
    with pytest.raises(FormulaError,match='授权'):calculate(data,question=question)

def test_units_and_row_selection_still_fail_closed(data):
    data[1]['E1']['query_result']['selection_required_for_scalar']=True
    with pytest.raises(FormulaError,match='未筛选'):calculate(data)
    data[1]['E1']['query_result'].pop('selection_required_for_scalar')
    data[1]['E1']['query_result']['rows'][0][5]='元/kW'
    with pytest.raises(FormulaError,match='单位'):calculate(data)

def test_row_unit_cannot_be_alias_or_other_row(data):
    data[1]['E1']['direct_columns'].pop('价格单位')
    with pytest.raises(FormulaError,match='真实直接'):calculate(data)
    source,e,b=data
    assert infer_sql_direct_columns(source,"SELECT '元/kWh' AS 价格单位,用电量 FROM 数据表",['价格单位','用电量'])=={'用电量':'数据表.用电量'}
    assert infer_sql_direct_columns(source,'SELECT SUM(用电量),价格单位 FROM 数据表',['q','价格单位'])=={}

def test_unit_column_cannot_borrow_another_tables_unit(data):
    data[1]['E1']['direct_columns']['价格单位']='另一表.价格单位'
    with pytest.raises(FormulaError,match='真实直接'):
        calculate(data)

def test_same_name_unit_column_not_order_unit(data):
    source,e,_=data
    assert infer_sql_output_units(source,'SELECT 计量单位,价格单位 FROM 数据表',['计量单位','价格单位'])=={}
    source={'tables':[{'name':'t','columns':[{'name':'订单数（单）'}]}]}
    assert infer_sql_output_units(source,'SELECT "订单数（单）" FROM t',['订单数（单）'])['订单数（单）']['unit']=='单'
    source={'tables':[{'name':'t','columns':[{'name':'粒度'},{'name':'用电量（度）'}]}]}
    assert infer_sql_output_units(source,'SELECT 粒度 FROM t',['粒度'])=={}
    assert infer_sql_output_units(source,'SELECT "用电量（度）" FROM t',['用电量（度）'])['用电量（度）']['unit']=='kWh'

def test_multiday_sum_exact_pairs(data):
    _,e,b=data
    e['E1']['query_result']['rows'].append(['2025-07-02','全站','2400','0.70','kWh','元/kWh'])
    for old,new in [('q','q2'),('p','p2')]:b[new]={**b[old],'row':1}
    r=calculate(data,expression='q*p+q2*p2',operation={'kind':'quantity_times_unit_price','pairs':[{'quantity':'q','unit_price':'p'},{'quantity':'q2','unit_price':'p2'}]})
    assert r['display_value']=='3376.50'

def test_transitive_review_and_false_or_duplicate_never_pass():
    cs={'K1':{'rule_origin':'user_operation'},'K2':{'parameters':[{'binding':{'calculation_id':'K1'}}]}}
    assert required_calculations([{'calculation_ids':['K2']}],cs)==['K1']
    row={'id':'K1','intent_supported':True,'parameter_roles_supported':True,'scope_supported':True,'pricing_supported':True}
    for key in list(row)[1:]:
        assert not review_passed({'operation_relations':[{**row,key:False}]},['K1'])
    assert not review_passed({'operation_relations':[row,row]},['K1'])
    assert not review_passed({'operation_relations':[{**row,'id':{}}]},['K1'])
