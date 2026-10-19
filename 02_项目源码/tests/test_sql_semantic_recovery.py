"""Actual readonly fixture DB; received model responses are explicit fakes.

No evaluation keys or live model calls. Positive/negative populations include
partially paid, wholly unpaid and no-order entities, plus nullable readings.
"""
import copy,json,sqlite3
import pytest
from backend.query_planner import answer_database
from backend.model_gateway import ModelError
from backend.schema_catalog import build_catalog
from backend.sql_engine import execute_sql
from backend.sql_contract_checks import mechanical_contract,explicit_output_contract,fingerprint
from test_sql_answer_contract_repair import source,ALL_NONE,ANY_NONE,reviewed


def plan(sql,parameters=()):return {'status':'ready','normalized_question':'用户原问','intent':{},'sql':sql,'parameters':list(parameters)}
class Fake:
    def __init__(self,*responses):self.responses=list(responses);self.calls=[]
    def __call__(self,messages,role):
        self.calls.append((role,json.loads(messages[-1]['content'])))
        item=self.responses.pop(0)
        if isinstance(item,Exception):raise item
        return copy.deepcopy(item)


def check(source,sql,q,params=()):
    return mechanical_contract(sql,list(params),q,build_catalog(source),source,execute_sql)


def test_received_invalid_plan_is_new_candidate_and_independently_rechecked(source):
    bad=reviewed(supported=False,failure_kind='invalid_plan',reason='金额未知不得补0',scope_review={'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':False,'output_contract':True})
    g=Fake(plan('SELECT COALESCE(amount_cents,0) FROM monthly'),bad,plan('SELECT amount_cents FROM monthly'),reviewed())
    status,result=answer_database(source,'列出月度原始金额',[],g)
    assert status=='completed' and result['rows']==[[100],[200],[None]]
    assert [c[0] for c in g.calls]==['table','verify','table','verify']
    assert g.calls[2][1]['received_review']==bad
    assert result['query']['semantic_reviews'][0]['review_response']==bad
    assert len(result['query']['semantic_reviews'])==2
    assert result['query']['semantic_reviews'][1]['review_response']['supported'] is True


def test_semantic_recovery_failure_does_not_flip_reviewer_false(source):
    bad=reviewed(supported=False,failure_kind='invalid_plan',reason='母集错误')
    g=Fake(plan('SELECT id FROM customers'),bad,plan('SELECT id,name FROM customers'),bad)
    status,result=answer_database(source,'列出客户',[],g)
    assert status=='failed' and not result.get('rows') and not result['claims']
    assert result['query']['semantic_recovery_stop']=='no_semantic_progress'
    assert len(g.calls)==4 and all(x['review_response']['supported'] is False for x in result['query']['semantic_reviews'])


def test_same_ast_comments_whitespace_and_parameters_stops_before_reexecute(source):
    bad=reviewed(supported=False,failure_kind='invalid_plan',reason='口径不符')
    g=Fake(plan('SELECT id FROM customers WHERE id>?',[1]),bad,plan('select id /* same */ from customers where id > ?', [1]))
    status,result=answer_database(source,'列出客户',[],g)
    assert status=='failed' and result['query']['semantic_recovery_stop']=='repeated_ast_and_parameters'
    assert len(g.calls)==3 and len(result['query']['semantic_reviews'])==1
    assert fingerprint('select id from customers where id>?',[2])!=fingerprint('select id from customers where id>?',[1])


def test_real_missing_condition_clarifies_instead_of_replanning(source):
    g=Fake(plan('SELECT id FROM customers'),reviewed(supported=False,failure_kind='missing_condition',reason='用户没有指定客户范围',clarification={'question':'需要哪个客户编号？','field':'customers.id'}))
    status,result=answer_database(source,'这个客户的订单',[],g)
    assert status=='needs_clarification' and len(g.calls)==2
    assert result['query']['semantic_reviews'][0]['status']=='needs_clarification'


@pytest.mark.parametrize('stage',[1,3])
def test_unknown_post_or_cancel_propagates_without_retry(source,stage):
    problem=ModelError('timeout without completed response','timeout')
    bad=reviewed(supported=False,failure_kind='invalid_plan',reason='口径错误')
    values=[plan('SELECT id FROM customers'),bad,plan('SELECT name FROM customers'),reviewed()]
    values[stage]=problem;g=Fake(*values)
    with pytest.raises(ModelError):answer_database(source,'列出客户',[],g)
    assert len(g.calls)==stage+1


def test_cancel_before_semantic_replan_does_not_send_new_request(source):
    stopped=False
    def progress(_):pass
    g=Fake(plan('SELECT id FROM customers'),reviewed(supported=False,failure_kind='invalid_plan',reason='错'))
    def cancelled():return len(g.calls)>=2
    with pytest.raises(InterruptedError):answer_database(source,'列出客户',[],g,cancelled=cancelled,progress=progress)
    assert len(g.calls)==2


def test_strong_none_probes_partial_entities_and_ignores_all_true_self_assertion(source):
    question='有订单但完全没有付款记录的客户编号有哪些？不把部分未付款当完全未付款。'
    wrong=check(source,ANY_NONE,question)
    assert wrong['issues'][0]['code']=='global_none_correlated_at_child_grain'
    assert wrong['counterexamples'][0]['rows']==[[1,2]]
    assert wrong['counterexamples'][0]['basis']=='actual_readonly_query'
    assert check(source,ALL_NONE,question)['issues']==[]
    g=Fake(plan(ANY_NONE),reviewed(),plan(ALL_NONE),reviewed())
    status,result=answer_database(source,question,[],g)
    assert status=='completed' and result['rows']==[[2]]
    first=result['query']['semantic_reviews'][0]
    assert first['review_response']['supported'] is True and first['status']=='failed'
    assert g.calls[2][1]['mechanical_contract']['counterexamples'][0]['rows']==[[1,2]]


def test_per_order_absence_not_misread_as_entity_global_absence(source):
    assert check(source,ANY_NONE,'列出存在未付款订单的客户编号')['issues']==[]
    assert check(source,'SELECT o.id FROM orders o WHERE NOT EXISTS(SELECT 1 FROM payments p WHERE p.order_id=o.id)','完全没有付款的订单编号')['issues']==[]


def test_joined_parent_projection_still_blocks_child_not_exists(source):
    sql='SELECT DISTINCT c.id FROM customers c JOIN orders o ON o.customer_id=c.id WHERE NOT EXISTS(SELECT 1 FROM payments p WHERE p.order_id=o.id)'
    assert any(i['code']=='global_none_correlated_at_child_grain' for i in check(source,sql,'完全没有付款记录的客户编号')['issues'])


def test_explicit_output_count_cannot_be_overruled_by_five_model_booleans(source):
    bad='SELECT id,name,ROW_NUMBER() OVER(ORDER BY id) AS ranking FROM customers'
    good='SELECT id,name FROM customers ORDER BY id'
    g=Fake(plan(bad),reviewed(),plan(good),reviewed())
    status,result=answer_database(source,'输出客户编号、姓名；按编号排序。',[],g)
    assert status=='completed' and len(result['columns'])==2
    assert result['query']['semantic_reviews'][0]['mechanical_contract']['issues'][0]['code']=='explicit_output_column_count'
    assert result['query']['semantic_reviews'][0]['execution_preview']['columns'][-1]=='ranking'


@pytest.mark.parametrize('q,count',[
 ('输出年份、编号、金额；按年份和名次排序',3),
 ('输出地区、年月、当月金额、累计金额（均分），按地区年月升序',4),
 ('按最早、最晚两列输出',None),
 ('输出例如编号、姓名等字段',None),
 ('输出编号或姓名',None),
 ('列明细折后金额',None),
 ('输出员工编号及三项值，按编号。',None),
 ('输出国家、订单数和两位小数运费。',3),
])
def test_only_unambiguous_literal_column_enumeration(q,count):
    c=explicit_output_contract(q)
    assert (c['expected_count'] if c else None)==count


def test_unrequested_null_filter_blocked_with_false_positive_boundaries(source):
    sql='SELECT day,value FROM measurements WHERE value IS NOT NULL'
    assert check(source,sql,'按日期列出测量值')['issues'][0]['code']=='unrequested_nonnull_filter'
    assert check(source,sql,'列出value不是NULL的测量值')['issues']==[]
    assert check(source,sql,'列出day非NULL的测量值')['issues']  # Other field is not authorization.
    assert check(source,sql,'保留value的NULL，不要过滤')['issues']
    with sqlite3.connect(source['data_path']) as c:c.execute('CREATE TABLE nonnull(v TEXT NOT NULL)')
    from backend.parsers import sqlite_tables
    source['tables'],_=sqlite_tables(source['data_path'])
    assert check(source,'SELECT v FROM nonnull WHERE v IS NOT NULL','列出字段')['issues']==[]  # Explicit DDL proves redundant.
    assert check(source,'SELECT value AS day FROM measurements WHERE value IS NOT NULL','列出day非NULL的测量')['issues']


def test_null_preserving_recovery_retains_unknown_value_not_zero(source):
    bad='SELECT day,value FROM measurements WHERE unit=\'kWh\' AND value IS NOT NULL ORDER BY day'
    good='SELECT day,value FROM measurements WHERE unit=\'kWh\' ORDER BY day'
    g=Fake(plan(bad),reviewed(),plan(good),reviewed())
    status,result=answer_database(source,'列出kWh测量值，不能过滤未知值',[],g)
    assert status=='completed' and result['rows']==[['2025-01-01','1.20'],['2025-01-02',None]]
    assert len(result['query']['semantic_reviews'])==2


def test_left_join_antiform_cannot_evade_child_grain_gate(source):
    sql='SELECT DISTINCT o.customer_id FROM orders o LEFT JOIN payments p ON p.order_id=o.id WHERE p.id IS NULL'
    checks=check(source,sql,'有订单但完全没有付款记录的客户')
    assert checks['issues'][0]['code']=='global_none_correlated_at_child_grain'
    assert checks['counterexamples'][0]['rows']==[[1,2]]


def test_executed_star_projection_count_is_checked(source):
    g=Fake(plan('SELECT * FROM orders'),reviewed(),{'status':'failed'})
    status,result=answer_database(source,'只返回订单编号和金额。',[],g)
    assert status=='failed'
    assert result['query_diagnostics']['semantic_reviews'][0]['mechanical_contract']['issues'][0]['actual']==3


def test_prior_literal_user_nonnull_basis_can_be_inherited_but_not_restored_after_remove(source):
    sql='SELECT value FROM measurements WHERE value IS NOT NULL'
    history=[{'original_question':'只列出value非NULL的数据','answer_id':'ancestor'}]
    args=(sql,[],'按日期排序',build_catalog(source),source,execute_sql)
    assert mechanical_contract(*args,history=history)['issues']==[]
    args=(sql,[],'取消非空筛选，保留全部数据',build_catalog(source),source,execute_sql)
    assert mechanical_contract(*args,history=history)['issues']
    fake_history=[{'question':'只列出value非NULL的数据'}]
    assert mechanical_contract(sql,[],'按日期排序',build_catalog(source),source,execute_sql,history=fake_history)['issues']


def test_other_predicate_proves_nonnull_filter_redundant(source):
    checks=check(source,'SELECT value FROM measurements WHERE value IS NOT NULL AND value > \'1.0\'','列出测量值超过1.0的记录')
    assert checks['issues']==[] and checks['null_filters'][0]['entailed_by_other_conjunct'] is True
    assert check(source,'SELECT value FROM measurements WHERE value IS NOT NULL AND COALESCE(value,2)>1','列出测量值')['issues']


def test_detailed_independent_filter_mapping_needs_exact_explicit_user_quote(source):
    from backend.sql_contract_checks import adjudicate_nonnull_basis
    q='未采集的不计入，列出剩余测量记录'
    sql='SELECT value FROM measurements WHERE value IS NOT NULL'
    checks=check(source,sql,q)
    assert adjudicate_nonnull_basis(copy.deepcopy(checks),reviewed(),q)
    valid=reviewed(nonnull_filter_basis=[{'table':'measurements','column':'value','user_quote':'未采集的不计入','supported':True}])
    assert adjudicate_nonnull_basis(copy.deepcopy(checks),valid,q)==[]
    for quote in ['列出剩余测量记录','按同一单位比较','非空记录','未采集的不计入（模型新增）']:
        invalid=reviewed(nonnull_filter_basis=[{'table':'measurements','column':'value','user_quote':quote,'supported':True}])
        assert adjudicate_nonnull_basis(copy.deepcopy(checks),invalid,q)
    invalid=reviewed(nonnull_filter_basis=[{'table':'measurements','column':'day','user_quote':'未采集的不计入','supported':True}])
    assert adjudicate_nonnull_basis(copy.deepcopy(checks),invalid,q)


def test_projection_distance_reduction_is_real_progress_not_fixed_two_api_limit(source):
    # 4 -> 3 -> 2 projections. No published candidate before the final check.
    g=Fake(plan('SELECT id,name,1,2 FROM customers'),reviewed(),plan('SELECT id,name,1 FROM customers'),reviewed(),plan('SELECT id,name FROM customers'),reviewed())
    status,result=answer_database(source,'输出编号、姓名。',[],g)
    assert status=='completed' and len(g.calls)==6 and len(result['query']['semantic_reviews'])==3
    assert [c['status'] for c in result['query']['semantic_reviews']]==['failed','failed','completed']


@pytest.mark.parametrize('question',[
 '列出至少有一笔从未支付订单的客户',
 '找出存在完全未付款订单的客户',
 '有某条没有任何付款记录的交易的客户编号',
 '列出部分未付款客户，不把部分未付款当完全未付款',
])
def test_explicit_child_existence_is_not_parent_global_none(source,question):
    assert not any(i['code']=='global_none_correlated_at_child_grain' for i in check(source,ANY_NONE,question)['issues'])


def test_received_review_is_sent_to_durable_sink_before_unknown_replanning_post(source):
    bad=reviewed(supported=False,failure_kind='invalid_plan',reason='实际计划错误')
    g=Fake(plan('SELECT id FROM customers'),bad,ModelError('unknown timeout','timeout'))
    audits=[]
    with pytest.raises(ModelError):answer_database(source,'列出客户',[],g,audit=audits.append)
    assert len(g.calls)==3 and len(audits)==1
    assert audits[0]['semantic_reviews'][0]['review_response']==bad
