"""Offline synthetic semantic edge cases; public business DBs are named separately."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from backend.parsers import sqlite_tables
from backend.query_planner import answer_database
from backend.query_semantics import diagnose_query, probe_joins
from backend.schema_catalog import build_catalog
from backend.schema_semantics import build_semantic_layer, element_contract


def source_for(path):
    tables, _ = sqlite_tables(path)
    return dict(id='synthetic', name=path.name, project_id='p', data_path=str(path),
                tables=tables, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                parser_version='test', warnings=[])


@pytest.fixture
def business(tmp_path):
    path = tmp_path / 'synthetic-business.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE customer(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customer(id), amount TEXT, created TEXT);
        CREATE TABLE detail(id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), quantity INTEGER);
        INSERT INTO customer VALUES(1,'甲'),(2,'乙');
        INSERT INTO orders VALUES(10,1,'100.1','2024-01-01'),(11,2,'100.1','2025-01-01'),(12,NULL,NULL,NULL);
        INSERT INTO detail VALUES(1,10,1),(2,10,2),(3,11,3),(4,999,4),(5,NULL,5);''')
    return source_for(path)


def definition(**changes):
    return dict(id='revenue', kind='metric', label='销售额', definition='订单头金额',
                references=[{'table':'orders','column':'amount'}], aliases=['营业收入'],
                unit='元', confirmation_id='confirmation-1', **changes)


def test_candidate_cannot_self_promote_and_other_source_alias_not_reused(business):
    catalog=build_catalog(business)
    entry=definition(origin='user_confirmed')
    layer=build_semantic_layer(catalog,business,{'source_sha256':business['sha256'],'candidates':[entry]})
    assert layer['definitions'][0]['origin']=='model_candidate'
    assert layer['definitions'][0]['status']=='requires_confirmation'
    assert 'confirmation_id' not in layer['definitions'][0]
    other=build_semantic_layer(catalog,business,{'source_sha256':'other','confirmed':[entry]})
    assert not other['definitions'] and other['warnings']
    catalog['semantic_layer']=layer
    contract=element_contract({'metric':{'definition_id':entry['id']}},catalog,business)
    assert contract['issues'][0]['code']=='unconfirmed_semantic_definition'


def test_confirmations_reference_actual_fields_and_conflicting_units_require_review(business):
    catalog=build_catalog(business)
    a=definition(); b={**a,'id':'revenue2','unit':'万元','confirmation_id':'confirmation-2'}
    bad={**a,'id':'bad','references':[{'table':'orders','column':'profit'}]}
    layer=build_semantic_layer(catalog,business,{'source_sha256':business['sha256'],'confirmed':[a,b,bad]})
    assert len(layer['definitions'])==2
    assert all(d['status']=='conflicting_confirmations' for d in layer['definitions'])
    assert layer['warnings']


@pytest.mark.parametrize('bad', [None, {'kind':[]}, {'kind':'alias','references':[{'table':[]}]}, {'kind':'alias','references':[{'table':'orders','column':[]}]}])
def test_malformed_definitions_are_ignored_with_no_exception(business,bad):
    catalog=build_catalog(business)
    layer=build_semantic_layer(catalog,business,{'source_sha256':business['sha256'],'confirmed':[bad]})
    assert not layer['definitions']


def test_element_contract_retains_canonical_fields_without_trusting_review_claims(business):
    contract=element_contract({'metric':{'table':'orders','column':'amount'},'comparison_base':'2024',
        'top_k':5,'unit':'元','api_key':'secret','supported':True},build_catalog(business),business)
    assert contract['elements']['comparison_baseline']=='2024'
    assert contract['elements']['unit']=='元'
    assert not contract['issues']
    assert 'secret' not in json.dumps(contract) and 'supported' not in contract
    bad=element_contract({'metric':{'table':'orders','column':'not_here'},'top_k':True},build_catalog(business),business)
    assert {i['code'] for i in bad['issues']}=={'unknown_column_reference','invalid_top_k'}


def test_ddl_cardinality_detects_header_sum_fanout_but_not_detail_sum(business):
    catalog=build_catalog(business)
    wrong=diagnose_query('SELECT SUM(o.amount) FROM orders o JOIN detail d ON d.order_id=o.id',catalog)
    correct=diagnose_query('SELECT SUM(d.quantity) FROM orders o JOIN detail d ON d.order_id=o.id',catalog)
    assert 'aggregate_fanout' in {i['code'] for i in wrong['findings']}
    assert 'aggregate_fanout' not in {i['code'] for i in correct['findings']}
    assert wrong['joins'][0]['relationship']['origin']=='ddl'


def test_real_probe_observes_join_growth_orphans_and_null_without_mutating_file(business):
    before=Path(business['data_path']).read_bytes()
    catalog=build_catalog(business)
    report=probe_joins(business,catalog,diagnose_query('SELECT SUM(o.amount) FROM orders o JOIN detail d ON o.id=d.order_id',catalog))
    sample=report['probes'][0]
    assert (sample['left_sample_rows'],sample['right_sample_rows'],sample['joined_sample_rows'])==(3,5,3)
    assert sample['right_key_profile']['repeated_nonnull_rows']==1
    assert sample['right_sample_match_rate']==3/5
    amount=next(p for p in sample['aggregate_previews'] if p['column']=='amount')
    assert amount['matched_source_sum']=='200.2' and amount['joined_sample_sum']=='300.3'
    assert amount['changed_by_sample_multiplicity']
    assert not sample['uses_original_sql_filters'] and not sample['sample_uniqueness_is_global_constraint']
    assert Path(business['data_path']).read_bytes()==before


def test_tail_duplicate_cannot_be_claimed_global_unique(business):
    with sqlite3.connect(business['data_path']) as c:
        c.execute('DELETE FROM detail')
        c.executemany('INSERT INTO detail VALUES(?,?,?)',[(i,i,1) for i in range(1,202)]+[(202,1,1)])
    business=source_for(Path(business['data_path']))
    catalog=build_catalog(business)
    report=probe_joins(business,catalog,diagnose_query('SELECT * FROM orders o JOIN detail d ON o.id=d.order_id',catalog))
    profile=report['probes'][0]['right_key_profile']
    assert profile['observed_unique_nonnull_keys'] is True
    assert profile['scope']=='bounded_sample_only' and profile['is_declared_unique_constraint'] is False
    assert report['probes'][0]['right_complete'] is False


def test_sqlite_collation_used_for_actual_match_instead_of_python_equality(tmp_path):
    path=tmp_path/'collation.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE a(k TEXT COLLATE NOCASE,amount REAL); CREATE TABLE b(k TEXT); INSERT INTO a VALUES('ABC',2); INSERT INTO b VALUES('abc');")
    source=source_for(path); catalog=build_catalog(source)
    diagnostic=diagnose_query('SELECT * FROM a JOIN b ON a.k=b.k',catalog)
    assert diagnostic['joins'][0]['relationship']['origin']=='query_candidate'
    assert probe_joins(source,catalog,diagnostic)['probes'][0]['joined_sample_rows']==1


def test_null_safe_or_join_does_not_reuse_ordinary_equality_unique_bound(business):
    catalog=build_catalog(business)
    diagnostic=diagnose_query('SELECT * FROM orders o JOIN detail d ON o.id=d.order_id OR d.order_id IS NULL',catalog)
    assert not diagnostic['joins']
    assert 'non_equijoin_or_derived_relation' in {f['code'] for f in diagnostic['findings']}
    negated=diagnose_query('SELECT * FROM orders o JOIN detail d ON NOT(o.id=d.order_id)',catalog)
    assert not negated['joins']


@pytest.mark.parametrize('sql,code',[
    ('SELECT SUM(DISTINCT amount) FROM orders','distinct_measure_not_identity'),
    ('SELECT COUNT(amount) FROM orders','count_excludes_null'),
    ('SELECT COALESCE(amount,0) FROM orders','null_coalesced'),
    ('SELECT amount/NULLIF(customer_id,0) FROM orders','division_scope'),
    ('SELECT * FROM orders WHERE id NOT IN (SELECT order_id FROM detail)','not_in_null'),
    ('SELECT LAG(amount) OVER(ORDER BY created) FROM orders','comparison_period'),
    ('SELECT * FROM orders ORDER BY amount DESC LIMIT 2','topk_ties'),
    ("SELECT strftime('%m',created),SUM(amount) FROM orders GROUP BY 1",'time_boundary'),
])
def test_semantic_risks_are_visible_not_silently_accepted(business,sql,code):
    assert code in {f['code'] for f in diagnose_query(sql,build_catalog(business))['findings']}


def test_probe_cancel_propagates_and_never_becomes_zero_matches(business):
    catalog=build_catalog(business)
    with pytest.raises(InterruptedError):
        probe_joins(business,catalog,diagnose_query('SELECT * FROM orders o JOIN detail d ON o.id=d.order_id',catalog),lambda:True)


class Responses:
    def __init__(self,*values):self.values=list(values);self.calls=[]
    def __call__(self,messages,purpose):
        self.calls.append((purpose,json.loads(messages[-1]['content'])))
        return self.values.pop(0)


def ready(sql,intent=None):
    return {'status':'ready','normalized_question':'订单金额','intent':intent or {'metric':'金额'},'sql':sql,'parameters':[]}


def test_publication_retains_diagnostics_and_requires_independent_review(business):
    model=Responses(ready('SELECT SUM(o.amount) FROM orders o JOIN detail d ON o.id=d.order_id'),
                    {'supported':False,'reason':'重复统计订单头金额','failure_kind':'invalid_plan'},{'status':'failed'})
    status,result=answer_database(business,'订单总金额',[],model)
    assert status=='failed' and not result.get('rows')
    assert result['query']['executed_result']['rows']
    assert model.calls[-2][0]=='verify' and model.calls[-2][1]['join_probes']['probes']
    assert result['query']['semantic_reviews'][0]['review_response']['supported'] is False


def test_invalid_schema_element_cannot_be_overridden_by_model_self_report(business):
    model=Responses(ready('SELECT amount FROM orders',{'metric':{'table':'orders','column':'profit'}}))
    status,result=answer_database(business,'金额',[],model)
    assert status=='failed' and len(model.calls)==1 and not result.get('rows')


def test_distinct_actual_local_errors_can_progress_but_repeated_query_stops(business):
    model=Responses(ready('SELECT absent1 FROM orders'),ready('SELECT absent2 FROM orders'),
                    ready('SELECT amount FROM orders'),{'supported':True,'interpretation_supported':True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    status,result=answer_database(business,'金额',[],model)
    assert status=='completed' and len(result['query']['repairs'])==2
    assert len(model.calls)==4


def test_public_chinook_header_fanout_and_snapshot_hash_preserved():
    path=Path(__file__).parent/'fixtures/complex_queries/chinook.sqlite'
    source=source_for(path); before=source['sha256']; catalog=build_catalog(source)
    diagnostic=diagnose_query('SELECT SUM(i.Total) FROM Invoice i JOIN InvoiceLine l ON i.InvoiceId=l.InvoiceId',catalog)
    assert 'aggregate_fanout' in {f['code'] for f in diagnostic['findings']}
    report=probe_joins(source,catalog,diagnostic)
    assert report['probes'][0]['max_matches_per_left_sample_row']>1
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert report['probes'][0]['left_complete'] is False
