"""Actual readonly SQLite with explicit fake reviewers; no model/network calls.

Controlled boundary fixtures exercise scope; named Northwind regression uses
the existing public teaching DB. None is an independent heldout success rate.
"""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from backend.model_gateway import ModelError
from backend.query_planner import answer_database
from backend.schema_catalog import build_catalog
from backend.sql_answer_contract import REVIEW_FIELDS, MAX_CHARS, build_answer_scope, candidate_contract
from backend.parsers import sqlite_tables


def source_for(path):
    tables, warnings = sqlite_tables(path)
    return dict(id='controlled', name=path.name, data_path=str(path), tables=tables,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(), parser_version='test', warnings=warnings)


@pytest.fixture
def source(tmp_path):
    path = tmp_path/'scope.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('''
        CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER REFERENCES customers(id),amount TEXT);
        CREATE TABLE payments(id INTEGER PRIMARY KEY,order_id INTEGER REFERENCES orders(id),amount TEXT);
        CREATE TABLE monthly(region TEXT,month TEXT,amount_cents INTEGER);
        CREATE TABLE measurements(day TEXT,value TEXT,unit TEXT);
        INSERT INTO customers VALUES(1,'部分付款'),(2,'完全无付款'),(3,'无订单'),(4,'全部有付款');
        INSERT INTO orders VALUES(10,1,'10.34'),(11,1,'0.12'),(20,2,NULL),(30,4,'0');
        INSERT INTO payments VALUES(1,10,'10.34'),(2,30,'0');
        INSERT INTO monthly VALUES('东','2025-01',100),('东','2025-02',200),('西','2025-01',NULL);
        INSERT INTO measurements VALUES('2025-01-01','1.20','kWh'),('2025-01-02',NULL,'kWh'),('2025-01-03','1200','Wh');
        ''')
    return source_for(path)


def reviewed(**patch):
    value = dict(supported=True, interpretation_supported=True, scope_review={k: True for k in REVIEW_FIELDS})
    value.update(patch)
    return value


class Gateway:
    def __init__(self, sql, review=None, contract=None):
        self.calls = [];self.sql = sql;self.review = reviewed() if review is None else review;self.contract = contract
    def __call__(self, messages, purpose):
        payload = json.loads(messages[-1]['content']);self.calls.append((purpose,payload,messages[0]['content']))
        if purpose == 'table':
            plan = dict(status='ready', normalized_question=payload['question'], intent={}, sql=self.sql, parameters=[])
            if self.contract is not None:plan['answer_contract'] = self.contract
            return plan
        if isinstance(self.review, Exception):raise self.review
        return self.review(payload) if callable(self.review) else copy.deepcopy(self.review)


ALL_NONE = '''SELECT c.id FROM customers c WHERE EXISTS(SELECT 1 FROM orders o WHERE o.customer_id=c.id)
AND NOT EXISTS(SELECT 1 FROM orders o JOIN payments p ON p.order_id=o.id WHERE o.customer_id=c.id) ORDER BY c.id'''
ANY_NONE = '''SELECT DISTINCT o.customer_id FROM orders o WHERE NOT EXISTS(SELECT 1 FROM payments p WHERE p.order_id=o.id) ORDER BY o.customer_id'''


def test_correct_empty_query_is_completed_but_not_zero(source):
    before = Path(source['data_path']).read_bytes()
    gateway = Gateway('SELECT id FROM customers WHERE id=99')
    status, result = answer_database(source,'列出编号99的客户',[],gateway)
    assert status == 'completed' and result['rows'] == [] and not result['claims']
    assert '不表示数值为零' in result['text'] and result['evidence']
    assert [c[0] for c in gateway.calls] == ['table','verify']
    assert Path(source['data_path']).read_bytes() == before


@pytest.mark.parametrize('review', [reviewed(supported=False), {'supported':True,'interpretation_supported':True}])
def test_empty_query_cannot_skip_scope_or_independent_rejection(source,review):
    status, result = answer_database(source,'客户99',[],Gateway('SELECT id FROM customers WHERE id=99',review))
    assert status == 'failed' and not result.get('rows')
    assert result['query']['executed_result']['rows'] == []


def test_count_zero_and_unknown_sum_remain_distinct(source):
    sql = 'SELECT COUNT(*),SUM(CAST(amount AS NUMERIC)) FROM orders WHERE customer_id=99'
    status,result = answer_database(source,'客户99订单数和已知金额和',[],Gateway(sql))
    assert status == 'completed' and result['rows'] == [[0,None]]
    assert result['claims'] and '未提供' in result['text']


def test_negative_correlation_scope_is_exposed_without_guessing_user_intent(source):
    catalog=build_catalog(source)
    wrong=build_answer_scope(ANY_NONE,{},catalog,source)
    right=build_answer_scope(ALL_NONE,{},catalog,source)
    correlations=lambda scope:[(x['table'],x['column']) for s in scope['sql_scopes'] for q in s['quantifiers'] if q['operator']=='NOT EXISTS' for x in q['outer_correlations']]
    assert correlations(wrong)==[('orders','id')]
    assert correlations(right)==[('customers','id')]
    assert wrong['sql_scopes'][0]['projections']==['o.customer_id']
    assert any(r['table']=='payments' and r['referenced_table']=='orders' for r in wrong['declared_relationships'])


def test_none_customer_scope_excludes_partly_paid_and_no_order_entities(source):
    status,result=answer_database(source,'有订单且完全无付款记录的客户',[],Gateway(ALL_NONE))
    assert status=='completed' and result['rows']==[[2]]


def test_any_unpaid_order_is_valid_for_a_different_user_question(source):
    status,result=answer_database(source,'存在一张没有付款记录订单的客户',[],Gateway(ANY_NONE))
    assert status=='completed' and result['rows']==[[1],[2]]


def test_wrong_quantifier_cannot_be_overruled_by_overall_supported(source):
    def review(payload):
        q=payload['answer_scope']['sql_scopes'][0]['quantifiers'][0]
        assert q['outer_correlations'][0]['table']=='orders'
        return reviewed(scope_review={**{k:True for k in REVIEW_FIELDS},'quantifier_scope':False})
    gateway=Gateway(ANY_NONE,review)
    status,result=answer_database(source,'有订单且完全无付款记录的客户',[],gateway)
    assert status=='failed' and not result.get('rows') and not result['claims']
    assert result['query']['executed_result']['rows']==[[1],[2]]
    assert len(gateway.calls)==2


@pytest.mark.parametrize('field', REVIEW_FIELDS)
def test_every_scope_dimension_requires_an_explicit_boolean_true(source,field):
    for value in (False,None,1,'true'):
        check=reviewed();check['scope_review'][field]=value
        status,result=answer_database(source,'客户编号',[],Gateway('SELECT id FROM customers',check))
        assert status=='failed' and not result.get('rows')
        assert result['query']['scope_review'][field] is (False if value is False else None)


def test_source_choice_review_has_unused_stored_metric_schema(source):
    sql='SELECT customer_id,SUM(CAST(amount AS NUMERIC)) FROM orders GROUP BY customer_id'
    def review(payload):
        inventory={t['table']:t for t in payload['answer_scope']['source_tables']}
        assert inventory['monthly']['used'] is False and 'amount_cents' in inventory['monthly']['columns']
        assert payload['answer_scope']['sql_scopes'][0]['group_by']==['customer_id']
        return reviewed(scope_review={**{k:True for k in REVIEW_FIELDS},'source_basis':False})
    status,result=answer_database(source,'月度汇总表按地区累计金额',[],Gateway(sql,review))
    assert status=='failed' and not result.get('rows')


def test_extra_month_group_is_visible_and_rejected_without_rewriting_sql(source):
    sql='SELECT region,month,SUM(amount_cents) FROM monthly GROUP BY region,month'
    def review(payload):
        assert payload['answer_scope']['sql_scopes'][0]['group_by']==['region','month']
        return reviewed(scope_review={**{k:True for k in REVIEW_FIELDS},'population_grain':False})
    status,result=answer_database(source,'全年按地区合计金额',[],Gateway(sql,review))
    assert status=='failed' and result['query']['sql']==sql
    assert len(result['query']['executed_result']['rows'])==3


def test_zero_child_entity_is_preserved_by_valid_population_query(source):
    sql='SELECT c.id,COUNT(o.id),SUM(CAST(o.amount AS NUMERIC)) FROM customers c LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.id ORDER BY c.id'
    status,result=answer_database(source,'每个客户的订单数与已知金额',[],Gateway(sql))
    assert status=='completed' and result['rows'][2]==[3,0,None]


def test_unrequested_null_filter_is_explicit_and_cannot_pass_missing_values_review(source):
    sql="SELECT day,value FROM measurements WHERE unit='kWh' AND value IS NOT NULL ORDER BY day"
    def review(payload):
        assert payload['answer_scope']['sql_scopes'][0]['null_filters']==[{'predicate':'NOT value IS NULL'}]
        return reviewed(scope_review={**{k:True for k in REVIEW_FIELDS},'missing_values':False})
    status,result=answer_database(source,'列全部kWh测量',[],Gateway(sql,review))
    assert status=='failed' and not result.get('rows')
    good="SELECT day,value FROM measurements WHERE unit='kWh' ORDER BY day"
    status,result=answer_database(source,'列全部kWh测量',[],Gateway(good))
    assert status=='completed' and result['rows'][-1]==['2025-01-02',None]


def test_known_sum_is_not_reinterpreted_as_force_filling_each_unknown(source):
    status,result=answer_database(source,'已知金额合计及未知行数',[],Gateway('SELECT SUM(CAST(amount AS NUMERIC)),SUM(amount IS NULL) FROM orders'))
    assert status=='completed' and result['rows'][0][0]==pytest.approx(10.46) and result['rows'][0][1]==1


def test_actual_display_rows_are_given_to_review_without_changing_raw_numbers(source):
    def review(payload):
        assert payload['rows']==[[1.2]] and payload['display_rows']==[['1.20']]
        assert payload['display_contract']['raw_values_preserved'] is True
        return reviewed()
    sql="SELECT ROUND(CAST(value AS NUMERIC),2) FROM measurements WHERE day='2025-01-01'"
    status,result=answer_database(source,'两位小数显示测量值',[],Gateway(sql,review))
    assert status=='completed' and result['rows']==[[1.2]] and result['display_rows']==[['1.20']]


def test_decimal_text_already_two_places_needs_no_specific_sql_function(source):
    def review(payload):
        assert payload['rows']==[['10.46']] and payload['display_rows']==[['10.46']]
        return reviewed()
    status,result=answer_database(source,'两位小数金额合计',[],Gateway('SELECT DECIMAL_SUM(amount) FROM orders',review))
    assert status=='completed' and result['rows']==[['10.46']]


def test_extra_true_column_is_not_silently_removed_to_fit_evaluation(source):
    sql='SELECT id,name,1 AS ranking FROM customers ORDER BY id'
    bad=reviewed(scope_review={**{k:True for k in REVIEW_FIELDS},'output_contract':False})
    status,result=answer_database(source,'只输出客户编号和姓名',[],Gateway(sql,bad))
    assert status=='failed' and result['query']['sql']==sql
    assert len(result['query']['executed_result']['columns'])==3


def test_bad_label_does_not_change_true_query_or_override_review(source):
    sql='SELECT COUNT(*) AS "只有一条未付订单的客户" FROM customers'
    bad=reviewed(supported=False,scope_review={**{k:True for k in REVIEW_FIELDS},'output_contract':False})
    gateway=Gateway(sql,bad)
    status,result=answer_database(source,'所有客户数量',[],gateway)
    assert status=='failed' and result['query']['sql']==sql and len(gateway.calls)==2
    assert not result['claims']


def test_unknown_post_result_has_no_automatic_second_review(source):
    gateway=Gateway('SELECT id FROM customers',ModelError('transport timeout'))
    with pytest.raises(ModelError):answer_database(source,'所有客户',[],gateway)
    assert len(gateway.calls)==2


def test_untrusted_candidate_is_allowlisted_and_cannot_claim_confirmation(source):
    catalog=build_catalog(source)
    raw={'source_basis':[{'table':'orders','role':'event','reason':'候选'}], 'confirmed_by_user':True,
         'api_key':'DO-NOT-PERSIST','scope_review':{k:True for k in REVIEW_FIELDS},
         'result_grain':[{'table':{},'column':[]}], 'quantifiers':[{'entity_key':{'table':'customers','column':'id'},'mode':'none','related_tables':[{}]}]}
    out=candidate_contract(raw,catalog)
    assert out['issues'] and out['status']=='candidate_requires_independent_review'
    assert 'DO-NOT-PERSIST' not in json.dumps(out) and 'scope_review' not in out and 'confirmed_by_user' not in out
    gateway=Gateway('SELECT id FROM customers',contract=raw)
    status,result=answer_database(source,'所有客户',[],gateway)
    assert status=='failed' and len(gateway.calls)==1


def test_scope_budget_is_explicit_and_bounded(source):
    catalog=build_catalog(source)
    for i in range(100):catalog['tables'].append({'name':f'extra{i}','primary_key':[],'columns':[{'name':f'col{j}'} for j in range(60)]})
    out=build_answer_scope('SELECT id FROM customers',{},catalog,source)
    assert out['truncated'] is True and len(json.dumps(out,ensure_ascii=False))<=MAX_CHARS


@pytest.mark.parametrize('contract', [
    {'source_basis':[{'table':'orders'}]*17},
    {'result_grain':[{'table':'orders','column':'id'}]*17},
    {'quantifiers':[{'entity_key':{'table':'customers','column':'id'},'mode':'none','related_tables':['orders']*17}]},
    {'output_columns':['id']*33},
    {'null_policy':'保留未知；'*120},
])
def test_truncated_candidate_is_visible_and_cannot_publish_a_partial_contract(source,contract):
    gateway=Gateway('SELECT id FROM customers',contract=contract)
    status,result=answer_database(source,'全部客户编号',[],gateway)
    candidate=result['query']['answer_scope']['candidate']
    assert status=='failed' and candidate['truncated'] is True
    assert 'candidate_contract_truncated' in candidate['issues']
    assert not result['claims'] and len(gateway.calls)==1


@pytest.mark.parametrize('contract', [
    {'population':{'table':[]}},
    {'quantifiers':[{'entity_key':{'table':'customers','column':'id'},'mode':'none','related_tables':{'orders':True}}]},
])
def test_invalid_nested_candidate_shape_is_rejected_without_crash(source,contract):
    gateway=Gateway('SELECT id FROM customers',contract=contract)
    status,result=answer_database(source,'全部客户编号',[],gateway)
    assert status=='failed' and result['query']['answer_scope']['candidate']['issues']
    assert not result['claims'] and len(gateway.calls)==1


def test_public_northwind_freight_original_candidate_has_two_decimal_values():
    path=Path(__file__).parent/'fixtures/complex_queries/northwind.sqlite'
    if not path.exists():pytest.skip('named public Northwind fixture unavailable')
    source=source_for(path)
    sql='SELECT ShipCountry,COUNT(Id),DECIMAL_SUM(Freight) FROM "Order" GROUP BY ShipCountry HAVING COUNT(Id)>50'
    def review(payload):
        assert len(payload['display_rows'])==5
        assert all(len(row[2].split('.')[-1])==2 for row in payload['display_rows'])
        return reviewed()
    status,result=answer_database(source,'按收货国家列订单数超过50的国家、订单数及两位小数运费',[],Gateway(sql,review))
    assert status=='completed' and len(result['rows'])==5
    assert source['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
