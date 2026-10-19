"""Actual SQLite execution, hostile-query boundaries, and honest provenance."""
from __future__ import annotations

import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from backend import parsers, sql_engine
from backend.sql_engine import SQLExecutionError, execute_sql


def source_for(path):
    tables, _ = parsers.sqlite_tables(path)
    return {'id':'fixture', 'name':path.name, 'tables':tables, 'data_path':str(path), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'parser_version':parsers.PARSER_VERSION}


@pytest.fixture
def shop(tmp_path):
    path = tmp_path/'shop.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('''
            CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, city TEXT);
            CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(customer_id), order_date TEXT, amount TEXT, status TEXT);
            CREATE TABLE items(item_id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(order_id), sku TEXT, quantity INTEGER);
            CREATE TABLE returns(order_id INTEGER, amount TEXT);
            INSERT INTO customers VALUES(1,'杭州'),(2,'北京'),(3,'杭州'),(4,'上海');
            INSERT INTO orders VALUES(10,1,'2024-01-15','0.10','paid'),(11,1,'2024-02-01','0.20','paid'),(12,2,'2024-02-12','12.80','paid'),(13,3,'2024-02-20',NULL,'cancelled'),(14,2,'2025-02-01','30.00','paid');
            INSERT INTO items VALUES(1,10,'a',1),(2,10,'b',2),(3,11,'a',3),(4,12,'c',1),(5,14,'c',2);
            INSERT INTO returns VALUES(11,'0.05'),(11,'0.05'),(12,NULL);
        ''')
    return source_for(path)


def test_multi_join_group_having_and_duplicate_count(shop):
    sql = '''SELECT c.city, COUNT(*) AS line_count, COUNT(DISTINCT o.order_id) AS orders,
        SUM(i.quantity) AS units FROM customers c JOIN orders o USING(customer_id)
        JOIN items i USING(order_id) WHERE o.order_date < ?
        GROUP BY c.city HAVING SUM(i.quantity)>? ORDER BY units DESC'''
    result = execute_sql(shop, sql, ['2025-01-01',1])
    assert result['rows'] == [['杭州',3,2,6]]
    assert result['used_tables'] == ['customers','items','orders']
    assert all(e['lineage']=='table' and e['row_numbers']==[] and e['rows']==[] for e in result['evidence'])
    assert result['matched_rows'] is None  # Not secretly COUNT(*) of a source table.
    assert result['total_rows'] == 1
    assert len(result['query_plan']) > 0


def test_cte_avoids_one_to_many_double_counting(shop):
    sql = '''WITH refunds AS (SELECT order_id, DECIMAL_SUM(amount) AS refund FROM returns GROUP BY order_id),
       net AS (SELECT o.order_id, o.customer_id, DECIMAL_SUB(o.amount,COALESCE(r.refund,'0')) AS amount
         FROM orders o LEFT JOIN refunds r ON r.order_id=o.order_id WHERE o.status='paid')
       SELECT c.city, DECIMAL_SUM(n.amount) AS net_revenue FROM net n
       JOIN customers c USING(customer_id) GROUP BY c.city ORDER BY c.city'''
    result = execute_sql(shop, sql)
    assert result['rows'] == [['北京','42.8'],['杭州','0.2']]
    assert result['used_tables'] == ['customers','orders','returns']
    assert len(result['calculations']) >= 4


def test_exists_correlated_subquery_and_null(shop):
    sql = '''SELECT customer_id FROM customers c WHERE EXISTS
      (SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id AND o.status=?)
      AND NOT EXISTS(SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id AND o.amount IS NULL)
      ORDER BY customer_id'''
    assert execute_sql(shop, sql, ['paid'])['rows'] == [[1],[2]]
    assert execute_sql(shop,'SELECT COUNT(*), COUNT(amount), SUM(amount) FROM orders WHERE order_id=13')['rows'] == [[1,0,None]]
    assert execute_sql(shop,"SELECT COUNT(*),SUM(amount) FROM orders WHERE status='missing'")['rows'] == [[0,None]]


def test_window_top_per_group_and_running_total(shop):
    sql = '''WITH ranked AS (SELECT order_id,customer_id,amount,
      ROW_NUMBER() OVER(PARTITION BY customer_id ORDER BY CAST(amount AS REAL) DESC, order_id) AS rn,
      SUM(CAST(amount AS REAL)) OVER(PARTITION BY customer_id ORDER BY order_date ROWS UNBOUNDED PRECEDING) AS running
      FROM orders WHERE status='paid')
      SELECT order_id,customer_id,amount,ROUND(running,2) FROM ranked WHERE rn=1 ORDER BY customer_id'''
    assert execute_sql(shop,sql)['rows'] == [[11,1,'0.20',0.3],[14,2,'30.00',42.8]]


def test_date_comparison_lag_and_union_order(shop):
    sql = '''WITH yearly AS (SELECT strftime('%Y',order_date) AS y, ROUND(SUM(amount),2) AS v
      FROM orders WHERE status='paid' GROUP BY y)
      SELECT y,v,LAG(v) OVER(ORDER BY y) FROM yearly ORDER BY y'''
    assert execute_sql(shop,sql)['rows'] == [['2024',13.1,None],['2025',30.0,13.1]]
    union = 'SELECT city AS label FROM customers WHERE customer_id=? UNION SELECT status FROM orders WHERE order_id=? ORDER BY label DESC'
    assert execute_sql(shop,union,[1,10])['rows'] == [['杭州'],['paid']]


def test_unknown_schema_names_are_not_constants(shop):
    for sql in ['SELECT "made_up" FROM orders','SELECT orders.made_up FROM orders','SELECT amount FROM missing','SELECT order_id FROM orders o JOIN items i USING(customer_id)']:
        with pytest.raises(SQLExecutionError) as error:
            execute_sql(shop,sql)
        assert error.value.code in ('unknown_column','unknown_table','execution')
    assert execute_sql(shop,"SELECT 'made_up' AS label FROM orders LIMIT 1")['rows']==[['made_up']]


def test_new_unfamiliar_schema_quoted_identifiers_and_exact_locations(tmp_path):
    path=tmp_path/'陌生库.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE "采购 明细" ("单号" TEXT, "数量(件)" INTEGER, "单价 ¥" TEXT)')
        c.executemany('INSERT INTO "采购 明细" VALUES(?,?,?)',[('Z9',1,'0.2'),('A2',3,'0.1'),('A1',2,'0.5')])
    result=execute_sql(source_for(path),'SELECT "单号", "数量(件)" FROM "采购 明细" WHERE "数量(件)" >= :minimum ORDER BY "单号"',{'minimum':2})
    assert result['rows']==[['A1',2],['A2',3]]
    assert result['evidence'][0]['row_numbers']==[3,2]
    assert result['evidence'][0]['lineage']=='row'
    assert result['evidence'][0]['rows']==result['rows']


def test_without_rowid_locators_match_preview_order(tmp_path):
    path=tmp_path/'keys.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE events(city TEXT, day TEXT, amount TEXT, PRIMARY KEY(city,day)) WITHOUT ROWID')
        c.executemany('INSERT INTO events VALUES(?,?,?)',[('b','2','4'),('a','2','2'),('a','1','1')])
    result=execute_sql(source_for(path),'SELECT e.city,e.day FROM events e WHERE e.day=:day ORDER BY e.city DESC',{'day':'2'})
    assert result['evidence'][0]['row_numbers']==[3,2]


def test_distinct_and_constant_queries_do_not_claim_row_lineage(shop):
    result=execute_sql(shop,'SELECT DISTINCT city FROM customers ORDER BY city')
    assert result['evidence'][0]['lineage']=='table'
    constant=execute_sql(shop,'SELECT 12 AS claimed_amount')
    assert constant['evidence']==[] and constant['verification']['status']=='limited'
    assert any('未引用真实数据表' in w for w in constant['warnings'])


def test_exact_decimals_and_precision_disclosure(shop):
    result=execute_sql(shop,"SELECT DECIMAL_SUM(amount),DECIMAL_AVG(amount),DECIMAL_DIV(1,3),DECIMAL_MUL('0.1','0.2') FROM orders WHERE customer_id=1")
    assert result['rows']==[['0.3','0.15','0.3333333333333333333333333333333333333333','0.02']]
    assert result['calculations'][0]['non_null_values']==2
    native=execute_sql(shop,'SELECT SUM(amount) FROM orders WHERE customer_id=1')
    assert native['rows'][0][0] != float(Decimal('0.3'))
    assert any('浮点误差' in warning for warning in native['warnings'])
    assert native['verification']['independent_recalculation'] is False
    assert execute_sql(shop,"SELECT DECIMAL_DIV(1,0)")['rows']==[[None]]
    assert execute_sql(shop,"SELECT DECIMAL_SUM(amount) FROM orders WHERE order_id=13")['rows']==[[None]]


def test_decimal_sort_and_bad_numeric_rejection(shop):
    result=execute_sql(shop,"SELECT amount FROM orders WHERE status='paid' ORDER BY amount COLLATE DECIMAL DESC")
    assert [r[0] for r in result['rows']]==['30.00','12.80','0.20','0.10']
    for value in ('abc','','NaN','1e999'):
        with pytest.raises(SQLExecutionError) as error:
            execute_sql(shop,'SELECT DECIMAL_SUM(?) FROM orders',[value])
        assert error.value.clarification


def test_integer_sum_large_integer_overflow_and_rounding(tmp_path):
    path=tmp_path/'ints.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE charges(cents INTEGER)')
        c.executemany('INSERT INTO charges VALUES(?)',[(2**53+1,),(2,)])
    source=source_for(path)
    assert execute_sql(source,'SELECT SUM(cents) FROM charges')['rows']==[[str(2**53+3)]]
    assert execute_sql(source,'SELECT DECIMAL_SUM(cents) FROM charges')['rows']==[[str(2**53+3)]]
    assert execute_sql(source,"SELECT ROUND(1.235,2),DECIMAL_DIV('1','8')")['rows']==[[1.24,'0.125']]
    with sqlite3.connect(path) as c:c.execute('INSERT INTO charges VALUES(?)',(2**63-1,))
    with pytest.raises(SQLExecutionError,match='overflow'):
        execute_sql(source,'SELECT SUM(cents) FROM charges')
    assert execute_sql(source,'SELECT DECIMAL_SUM(cents) FROM charges')['rows']==[[str(2**53+3+2**63-1)]]


@pytest.mark.parametrize('sql',[
    'DELETE FROM orders','UPDATE orders SET amount=0','DROP TABLE orders',
    'SELECT * FROM orders; DELETE FROM orders','PRAGMA table_info(orders)',
    "ATTACH DATABASE '/tmp/nope' AS other", "SELECT * FROM main.orders",
    'SELECT * FROM sqlite_schema','SELECT * FROM sqlite_master',
    "SELECT load_extension('/tmp/nope')", "SELECT readfile('/etc/passwd')",
    "SELECT writefile('/tmp/nope','bad')",'SELECT randomblob(1000000000)',
    'SELECT zeroblob(1000000000)', 'SELECT * FROM pragma_table_info(\'orders\')',
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
    'CREATE TABLE changed AS SELECT * FROM orders','VACUUM',
])
def test_hostile_sql_cannot_mutate_or_read_outside_source(shop,sql):
    before=Path(shop['data_path']).read_bytes()
    with pytest.raises(SQLExecutionError):
        execute_sql(shop,sql)
    assert Path(shop['data_path']).read_bytes()==before


def test_table_allowlist_views_and_virtual_tables(shop):
    scoped=dict(shop,tables=[t for t in shop['tables'] if t['name']=='orders'])
    with pytest.raises(SQLExecutionError,match='普通表'):
        execute_sql(scoped,'SELECT * FROM customers')
    with sqlite3.connect(shop['data_path']) as c:
        c.execute('CREATE VIEW visible AS SELECT * FROM orders')
        c.execute('CREATE VIRTUAL TABLE search USING fts5(text)')
    extended=source_for(Path(shop['data_path']))
    for sql in ('SELECT * FROM visible','SELECT * FROM search','SELECT * FROM search_data'):
        with pytest.raises(SQLExecutionError):
            execute_sql(extended,sql)


def test_parameters_binding_named_and_injection_literal(shop):
    assert execute_sql(shop,'SELECT order_id FROM orders WHERE status=:state ORDER BY order_id',{'state':'paid'})['rows']==[[10],[11],[12],[14]]
    assert execute_sql(shop,'SELECT order_id FROM orders WHERE status=?',["paid' OR 1=1 --"])['rows']==[]
    for params in (['x',1],{'bad':1},[2**100],[float('nan')],{'x':[]}):
        with pytest.raises(SQLExecutionError):
            execute_sql(shop,'SELECT ? FROM orders',params)


def test_result_bounds_and_output_count_are_honest(shop):
    result=execute_sql(shop,'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<500) SELECT x FROM n')
    assert len(result['rows'])==200 and result['truncated'] and result['total_rows'] is None
    limited=execute_sql(shop,'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<500) SELECT x FROM n LIMIT 5')
    assert len(limited['rows'])==5 and not limited['truncated'] and limited['total_rows']==5


def test_recursive_exhaustion_is_stopped(shop,monkeypatch):
    monkeypatch.setattr(sql_engine,'MAX_VM_STEPS',20_000)
    with pytest.raises(SQLExecutionError) as error:
        execute_sql(shop,'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n')
    assert error.value.code=='limit'


def test_cancel_before_and_during_work(shop):
    with pytest.raises(InterruptedError):
        execute_sql(shop,'SELECT * FROM orders',cancelled=lambda:True)
    calls=[0]
    def cancelled():
        calls[0]+=1
        return calls[0]>4
    with pytest.raises(InterruptedError):
        execute_sql(shop,'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n',cancelled=cancelled)


def test_oversized_cell_and_long_query_fail_safely(shop):
    # SQLite printf returns NULL when its requested allocation exceeds LENGTH.
    assert execute_sql(shop,"SELECT printf('%2000000s','x')")['rows']==[[None]]
    with pytest.raises(SQLExecutionError):
        execute_sql(shop,'SELECT '+',' .join('1' for _ in range(1000)))


def test_schema_drift_rejected(shop):
    with sqlite3.connect(shop['data_path']) as c:c.execute('ALTER TABLE orders ADD COLUMN changed TEXT')
    with pytest.raises(SQLExecutionError,match='结构与导入记录不一致'):
        execute_sql(shop,'SELECT * FROM orders')


def test_reference_sql_and_python_independent_business_totals(shop):
    # Independently assembled expected values, without calling the query engine.
    with sqlite3.connect(shop['data_path']) as c:
        raw=c.execute("SELECT customer_id,amount FROM orders WHERE status='paid' AND order_date<'2025-01-01'").fetchall()
        cities=dict(c.execute('SELECT customer_id,city FROM customers'))
    totals={}
    for customer,amount in raw:
        totals[cities[customer]]=totals.get(cities[customer],Decimal(0))+Decimal(amount)
    actual=execute_sql(shop,"SELECT c.city,DECIMAL_SUM(o.amount) FROM customers c JOIN orders o USING(customer_id) WHERE o.status='paid' AND o.order_date<'2025-01-01' GROUP BY c.city")
    assert {city:Decimal(amount) for city,amount in actual['rows']}==totals


def test_total_aggregate_does_not_claim_a_single_contributing_row(shop):
    result=execute_sql(shop,'SELECT TOTAL(amount) FROM orders')
    assert result['evidence'][0]['lineage']=='table'
    assert result['evidence'][0]['row_numbers']==[]


def test_real_to_decimal_conversion_is_disclosed(shop):
    result=execute_sql(shop,"SELECT DECIMAL_SUM(CAST(amount AS REAL)) FROM orders WHERE customer_id=1")
    assert result['rows']==[['0.3']]
    assert any('REAL值' in w for w in result['warnings'])


def test_result_byte_bound_is_separate_from_row_count(shop,monkeypatch):
    monkeypatch.setattr(sql_engine,'MAX_RESULT_BYTES',70)
    result=execute_sql(shop,"SELECT order_id, amount FROM orders ORDER BY order_id")
    assert result['truncated'] and len(result['rows'])<5
    assert result['total_rows'] is None


def test_invalid_function_arity_is_a_local_repairable_syntax_error(shop):
    with pytest.raises(SQLExecutionError) as caught:
        execute_sql(shop, 'SELECT DECIMAL_SUM(1,2)')
    assert caught.value.code == 'syntax'


def test_comma_limit_positional_bindings_preserve_original_sql(shop):
    sql='SELECT order_id FROM orders ORDER BY order_id LIMIT ?, ?'
    result=execute_sql(shop,sql,[1,3])
    assert result['rows']==[[11],[12],[13]]
    assert result['sql']==sql and result['parameters']==[1,3]
    assert result['evidence'][0]['lineage']=='table'
    assert result['evidence'][0]['row_numbers']==[]


def test_named_placeholders_with_sequence_bindings_preserve_slot_order(shop):
    result=execute_sql(shop,'SELECT order_id FROM orders ORDER BY order_id LIMIT :offset, :count',[1,3])
    assert result['rows']==[[11],[12],[13]]
    assert result['evidence'][0]['lineage']=='table'


def test_ordinary_positional_filter_retains_correct_rows_without_fake_locators(shop):
    result=execute_sql(shop,'SELECT order_id,amount FROM orders WHERE customer_id=? ORDER BY order_id',[1])
    assert result['rows']==[[10,'0.10'],[11,'0.20']]
    assert result['evidence'][0]['lineage']=='table'
    assert result['evidence'][0]['row_numbers']==[]
    assert result['evidence'][0]['rows']==[]


def test_named_mapping_limit_keeps_correct_rows_and_exact_locators(shop):
    result=execute_sql(shop,'SELECT order_id FROM orders ORDER BY order_id LIMIT :offset, :count',{'offset':1,'count':3})
    assert result['rows']==[[11],[12],[13]]
    assert result['evidence'][0]['lineage']=='row'
    assert result['evidence'][0]['row_numbers']==[2,3,4]


@pytest.mark.parametrize('sql,parameters',[
    ('SELECT order_id FROM orders WHERE status=?',[]),
    ('SELECT order_id FROM orders WHERE status=?',['paid','extra']),
    ('SELECT order_id FROM orders WHERE status=:state',{}),
    ('SELECT order_id FROM orders WHERE status=?',{'state':'paid'}),
])
def test_sqlite_binding_errors_are_repairable_syntax(shop,sql,parameters):
    with pytest.raises(SQLExecutionError) as error:
        execute_sql(shop,sql,parameters)
    assert error.value.code=='syntax'
    assert '参数' in str(error.value)


@pytest.mark.parametrize('sql',[
    'DELETE FROM orders WHERE order_id=?',
    'SELECT randomblob(?) FROM orders',
])
def test_forbidden_queries_do_not_become_repairable_binding_errors(shop,sql):
    with pytest.raises(SQLExecutionError) as error:
        execute_sql(shop,sql,[])
    assert error.value.code=='forbidden'


def test_top_level_round_retains_requested_zeroes_without_changing_raw_rows(shop):
    result=execute_sql(shop,'SELECT order_id, ROUND(1373,2) AS "金额", ROUND(-1.2,2) AS negative, ROUND(2.6,0) AS whole, ROUND(NULL,2) AS missing FROM orders WHERE order_id=10')
    assert result['rows']==[[10,1373.0,-1.2,3.0,None]]
    assert result['display_rows']==[[10,'1373.00','-1.20','3',None]]
    assert result['evidence'][0]['rows']==result['rows']
    assert result['columns']==['order_id','金额','negative','whole','missing']


def test_round_display_uses_executed_value_not_original_formula(shop):
    result=execute_sql(shop,"SELECT ROUND(2.675,2), ROUND(-2.675,2), ROUND(1.005,2), ROUND(1.2345678901234,12) FROM orders LIMIT 1")
    # SQLite 3.50 rounds its binary REAL value here; Decimal('2.675') would
    # instead produce 2.68. Display must retain the value SQLite executed.
    assert result['rows']==[[2.67,-2.67,1.0,1.234567890123]]
    assert result['display_rows']==[['2.67','-2.67','1.00','1.234567890123']]


def test_round_display_preserves_nonformatted_ids_text_decimals_and_null_rows(shop):
    result=execute_sql(shop,"SELECT order_id,amount,ROUND(amount,2) AS formatted FROM orders WHERE order_id IN (10,13) ORDER BY order_id")
    assert result['rows']==[[10,'0.10',0.1],[13,None,None]]
    assert result['display_rows']==[[10,'0.10','0.10'],[13,None,None]]


@pytest.mark.parametrize('sql,parameters',[
    ('SELECT ROUND(amount,2)+1 FROM orders',[]),
    ('SELECT CAST(ROUND(amount,2) AS REAL) FROM orders',[]),
    ('SELECT ROUND(amount,?) FROM orders',[2]),
    ('SELECT ROUND(amount,order_id) FROM orders',[]),
    ('SELECT ROUND(amount,2.0) FROM orders',[]),
    ("SELECT ROUND(amount,'2') FROM orders",[]),
    ('SELECT ROUND(amount,13) FROM orders',[]),
    ('SELECT ROUND(amount,-2) FROM orders',[]),
    ('SELECT ROUND(amount) FROM orders',[]),
    ('SELECT *,ROUND(amount,2) FROM orders',[]),
    ('SELECT orders.*,ROUND(amount,2) FROM orders',[]),
    ('SELECT rounded FROM (SELECT ROUND(amount,2) AS rounded FROM orders)',[]),
    ('SELECT ROUND(amount,2) FROM orders UNION SELECT ROUND(amount,2) FROM orders',[]),
])
def test_round_display_skips_unsupported_or_unreliable_projection(shop,sql,parameters):
    assert 'display_rows' not in execute_sql(shop,sql,parameters)


def test_top_level_round_formats_only_its_own_column_with_other_expressions(shop):
    result=execute_sql(shop,'SELECT order_id,ROUND(amount,2) AS a,ROUND(amount,2)+1 AS b FROM orders WHERE order_id=10')
    assert result['rows']==[[10,0.1,1.1]]
    assert result['display_rows']==[[10,'0.10',1.1]]


def test_round_display_handles_large_finite_value_without_decimal_context_error(shop):
    result=execute_sql(shop,'SELECT ROUND(1e100,2) FROM orders LIMIT 1')
    assert result['rows']==[[1e100]]
    assert result['display_rows']==[['1'+'0'*100+'.00']]


def test_round_display_is_absent_for_null_only_or_empty_results(shop):
    assert 'display_rows' not in execute_sql(shop,'SELECT ROUND(NULL,2) FROM orders LIMIT 1')
    assert 'display_rows' not in execute_sql(shop,'SELECT ROUND(amount,2) FROM orders WHERE 0')
