"""Synthetic many-table retrieval/path tests plus named public database checks."""
import json
import sqlite3
from pathlib import Path

import pytest

from backend.schema_catalog import build_catalog
from backend.schema_linking import retrieve_schema, expand_fk_bridges
from backend.schema_semantics import build_semantic_layer
from backend.query_planner import answer_database
from test_schema_semantics import source_for, Responses, ready


@pytest.fixture
def many_tables(tmp_path):
    path=tmp_path/'synthetic-36-tables.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER REFERENCES customers(id));
        CREATE TABLE lines(id INTEGER PRIMARY KEY,order_id INTEGER REFERENCES orders(id),product_id INTEGER REFERENCES products(id),amount REAL);
        CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT);
        INSERT INTO customers VALUES(1,'甲'); INSERT INTO orders VALUES(1,1);
        INSERT INTO products VALUES(1,'书'); INSERT INTO lines VALUES(1,1,1,2);''')
        for i in range(32):c.execute(f'CREATE TABLE unrelated_{i}(id INTEGER,value TEXT)')
    return source_for(path)


def test_many_table_retrieval_records_real_column_and_truncation(many_tables):
    catalog=build_catalog(many_tables,[])
    result=retrieve_schema('lines amount grouped by customers',catalog,limit=5)
    assert result['inspected_table_count']==36 and result['truncated']
    assert result['ranked_tables'][0]['table']=='lines'
    assert 'customers' in [r['table'] for r in result['ranked_tables']]
    assert not result['embedding_used']


def test_confirmed_alias_is_source_scoped_and_candidate_alias_has_no_ranking_power(many_tables):
    catalog=build_catalog(many_tables,[])
    entry={'id':'revenue','kind':'metric','label':'营业额','aliases':['销售收入'],
           'definition':'明细金额','references':[{'table':'lines','column':'amount'}],'confirmation_id':'human1'}
    envelope={'source_sha256':many_tables['sha256'],'candidates':[entry]}
    catalog['semantic_layer']=build_semantic_layer(catalog,many_tables,envelope)
    assert retrieve_schema('销售收入',catalog)['no_match']
    envelope={'source_sha256':many_tables['sha256'],'confirmed':[entry]}
    catalog['semantic_layer']=build_semantic_layer(catalog,many_tables,envelope)
    result=retrieve_schema('销售收入',catalog)
    assert result['ranked_tables'][0]['table']=='lines'
    assert result['ranked_tables'][0]['matches'][0]['origin']=='user_confirmed'


def test_fk_bridge_adds_missing_two_hops_not_same_name_guess(many_tables):
    catalog=build_catalog(many_tables,[])
    result=expand_fk_bridges(catalog,['customers','products'])
    assert set(result['bridge_tables'])=={'orders','lines'}
    assert result['paths'][0]['tables']==['customers','orders','lines','products']
    assert all(r['origin']=='ddl' for r in result['paths'][0]['relationships'])
    disconnected=expand_fk_bridges(catalog,['customers','unrelated_1'])
    assert not disconnected['paths'] and disconnected['unconnected_components']


def test_same_length_multiple_foreign_keys_are_ambiguous(tmp_path):
    path=tmp_path/'ambiguous.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE people(id INTEGER PRIMARY KEY); CREATE TABLE orders(id INTEGER PRIMARY KEY,buyer_id REFERENCES people(id),seller_id REFERENCES people(id));')
    result=expand_fk_bridges(build_catalog(source_for(path)),['orders','people'])
    assert result['ambiguous_paths'] and not result['paths']
    alternatives=result['ambiguous_paths'][0]['alternatives']
    assert {p['relationships'][0]['columns'][0] for p in alternatives}=={'buyer_id','seller_id'}


def test_bridge_budget_never_silently_omits_needed_table(many_tables):
    result=expand_fk_bridges(build_catalog(many_tables,[]),['customers','products'],max_tables=3)
    assert result['truncated'] and not result['paths']


def test_planner_actually_allows_missing_bridge_then_preserves_path_audit(many_tables):
    model=Responses({'tables':['customers','products']},ready('SELECT c.name,p.name,l.amount FROM customers c JOIN orders o ON c.id=o.customer_id JOIN lines l ON o.id=l.order_id JOIN products p ON p.id=l.product_id'),
                    {'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    status,result=answer_database(many_tables,'客户购买的商品和金额',[],model)
    assert status=='completed' and result['rows']==[['甲','书',2.0]]
    assert set(model.calls[1][1]['query_allowed_tables'])=={'customers','products','orders','lines'}
    assert set(result['query']['fk_bridges']['bridge_tables'])=={'orders','lines'}
    assert len(model.calls)==3


def test_public_chinook_multihop_path_uses_actual_declared_keys():
    source=source_for(Path(__file__).parent/'fixtures/complex_queries/chinook.sqlite')
    result=expand_fk_bridges(build_catalog(source,[]),['Customer','Track'])
    assert result['paths'][0]['tables']==['Customer','Invoice','InvoiceLine','Track']
    assert set(result['bridge_tables'])=={'Invoice','InvoiceLine'}


def test_public_northwind_names_without_fk_are_not_declared_relationships():
    source=source_for(Path(__file__).parent/'fixtures/complex_queries/northwind.sqlite')
    catalog=build_catalog(source,[])
    if not catalog['relationships']:
        result=expand_fk_bridges(catalog,['Order','OrderDetail'])
        assert not result['paths'] and result['unconnected_components']
    else:
        assert all(r['origin']=='ddl' for r in catalog['relationships'])
