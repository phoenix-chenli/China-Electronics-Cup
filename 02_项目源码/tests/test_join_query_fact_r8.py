"""Real local SQLite + scripted model returns; no external model calls."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from backend.parsers import sqlite_tables
from backend.schema_catalog import build_catalog
from backend.sql_engine import execute_sql
from backend.query_result_context import build_query_result_context, direct_join_projection
from backend.query_fact_renderer import render_query_fact
from backend.cross_source_engine import answer_cross_source
from backend.reasoning_tools import ReasoningLimits
from test_cross_query_fact_renderer import Gateway, review


def test_real_r7_cross007_same_bytes_same_sql_new_explicit_cell_selection():
    fixtures=Path(__file__).parent/'fixtures'
    cases=json.loads((fixtures/'r7_query_fact_saved_projections.json').read_text())['cases']
    case=next(c for c in cases if c['id']=='cross-007')
    old=next(e for e in case['evidence'] if e['id']=='E7')
    db=fixtures/'r7_station_register.sqlite'
    assert hashlib.sha256(db.read_bytes()).hexdigest()==old['sha256']
    tables,warnings=sqlite_tables(db)
    source={'id':old['source_id'],'name':old['source_name'],'data_path':str(db),
        'sha256':old['sha256'],'parser_version':old['parser_version'],'tables':tables,'warnings':warnings}
    actual=execute_sql(source,old['query']['sql'],old['query']['parameters'])
    assert {k:actual[k] for k in ('columns','rows','truncated')}==old['query_result']
    catalog={**build_catalog(source,table_names=[]),'id':source['id'],'sha256':source['sha256']}
    direct,audit=direct_join_projection(catalog,actual)
    assert direct['checks']=='inspections.checks' and direct['unit']=='inspections.unit'
    assert audit['right_unique_key']==['station_id']
    new={**copy.deepcopy(old),'direct_columns':direct,'direct_column_provenance':audit}
    new['query_result_context']=build_query_result_context(new)
    saved=case['saved_candidate_attempts'][0]['claims'][0]['query_fact']
    def remap(cell_id):
        # New, explicitly constructed test selector, NOT automatic correction
        # of an old id by product code. Read old trusted cell metadata; no ID parse.
        row,cell=next((r,c) for r in old['query_result_context']['rows'] for c in r['cells'] if c['cell_id']==cell_id)
        return new['query_result_context']['rows'][row['row_index']]['cells'][cell['column_index']]['cell_id']
    selected={**saved,'context_id':new['query_result_context']['context_alias'],
        'cell_id':remap(saved['cell_id']),'unit_cell_id':remap(saved['unit_cell_id']),
        'entity_cells':[remap(i) for i in saved['entity_cells']]}
    claim=render_query_fact(selected,[new])['claim']
    assert claim['query_bindings'][0]['value']==42 and claim['query_bindings'][0]['unit']=='次'
    assert '2025-02' in claim['text'] and '一致' not in claim['text']
    assert not old['direct_columns']  # historical record is not repaired or rescored


@pytest.fixture
def joined(tmp_path):
    path=tmp_path/'joined.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE sites(id TEXT PRIMARY KEY,name TEXT,bank TEXT,unit TEXT);
            CREATE TABLE readings(site_id TEXT,period TEXT,amount INTEGER,unit TEXT);
            INSERT INTO sites VALUES('D01','重名点','北岸','元'),('D02','重名点','南岸','元'),('D03','独立点','东岸','元');
            INSERT INTO readings VALUES('D01','2025-02',30,'次'),('D02','2025-02',45,'次'),('D03','2025-02',42,'次');''')
    tables,warnings=sqlite_tables(path)
    return {'id':'s','name':'joined.sqlite','format':'sqlite','data_path':str(path),'sha256':'a'*64,
        'parser_version':'test','tables':tables,'warnings':warnings,'metadata':{}}


SQL='SELECT r.site_id,r.period,r.amount,r.unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=?'


def prepare(source,sql=SQL,params=None):
    execution=execute_sql(source,sql,['独立点'] if params is None else params)
    catalog={**build_catalog(source,table_names=[]),'id':source['id'],'sha256':source['sha256']}
    direct,audit=direct_join_projection(catalog,execution)
    e={'id':'E1','kind':'table','source_id':source['id'],'sha256':source['sha256'],'parser_version':source['parser_version'],
       'query':{k:execution[k] for k in ('sql','parameters')},'query_result':{k:execution[k] for k in ('columns','rows','truncated')},
       'direct_columns':direct,'direct_column_provenance':audit}
    e['query_result_context']=build_query_result_context(e)
    return e


def spec(e,unit=True):
    ctx=e['query_result_context'];cells={c['column_label']:c for c in ctx['rows'][0]['cells']}
    out={'evidence_id':e['id'],'context_id':ctx['context_alias'],'cell_id':cells['amount']['cell_id'],
        'entity_cells':[cells[k]['cell_id'] for k in ('site_id','period') if k in cells]}
    if unit:out['unit_cell_id']=cells['unit']['cell_id']
    return out


def test_true_join_direct_fields_and_real_single_unit_reach_unchanged_checker(joined):
    e=prepare(joined)
    assert e['direct_columns']['amount']=='readings.amount' and e['direct_columns']['unit']=='readings.unit'
    assert e['direct_column_provenance']['right_unique_key']==['id']
    claim=render_query_fact(spec(e),[e])['claim']
    assert claim['query_bindings'][0]['value']==42 and claim['query_bindings'][0]['unit']=='次'


@pytest.mark.parametrize('sql',[
    "SELECT r.site_id,r.period,r.amount,'次' AS unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=?",
    "SELECT r.site_id,r.period,r.amount,s.unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=?",
    "SELECT r.site_id,r.period,r.amount,COALESCE(r.unit,'次') AS unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=?",
    "SELECT r.site_id,r.period,r.amount,r.unit FROM readings r JOIN sites s ON r.site_id=s.id JOIN sites x ON r.site_id=x.id WHERE s.name=?",
    "SELECT r.site_id,r.period,SUM(r.amount) AS amount,r.unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=? GROUP BY r.site_id,r.period,r.unit",
    "SELECT r.site_id,r.period,r.amount,r.unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=? LIMIT 1",
])
def test_constants_expression_cross_table_repeated_join_aggregate_and_hidden_rows_stay_rejected(joined,sql):
    e=prepare(joined,sql)
    with pytest.raises(ValueError,match='同一真实原表'):render_query_fact(spec(e),[e])


def test_nonunique_join_target_not_eligible(joined):
    e=prepare(joined,SQL.replace('r.site_id=s.id','r.site_id=s.name'))
    assert not e['direct_columns']


def test_mixed_units_not_added_to_direct_unit_proof(joined):
    with sqlite3.connect(joined['data_path']) as c:c.execute("UPDATE readings SET unit='项' WHERE site_id='D02'")
    e=prepare(joined,params=['重名点'])
    assert 'unit' not in e['direct_columns']
    assert e['direct_column_provenance']['rejected_mixed_or_missing_unit_outputs']==['unit']
    with pytest.raises(ValueError,match='同一真实原表'):render_query_fact(spec(e),[e])


def test_declared_pk_collation_fanout_still_rejected(joined):
    with sqlite3.connect(joined['data_path']) as c:
        c.executescript("CREATE TABLE labels(id TEXT PRIMARY KEY);INSERT INTO labels VALUES('x'),('X');CREATE TABLE measured(site_id TEXT COLLATE NOCASE,amount INTEGER,unit TEXT);INSERT INTO measured VALUES('x',8,'次');")
    joined['tables'],_=sqlite_tables(joined['data_path'])
    e=prepare(joined,'SELECT m.site_id,m.amount,m.unit FROM measured m JOIN labels l ON m.site_id=l.id',[])
    assert len(e['query_result']['rows'])==2 and not e['direct_columns']


@pytest.mark.parametrize('query_ok',[True,False,None,1])
def test_actual_controller_join_uses_declared_mapping_then_independent_relation(joined,query_ok):
    plan={'actions':[{'id':'S1','kind':'sql','source_id':'s','sql':SQL,'parameters':['独立点']}]}
    def chosen(p):
        e=next(e for e in p['evidence'] if e.get('query_result_context'))
        assert e['direct_column_provenance']['constraint_origin']=='ddl'
        return {'status':'completed','claims':[{'query_fact':spec(e)}]}
    g=Gateway([plan,chosen,lambda p:review(p,query=query_ok)])
    status,result=answer_cross_source([joined],'独立点2025年2月巡检多少次',[],g,limits=ReasoningLimits(max_no_progress_rounds=1))
    assert (status=='completed') is (query_ok is True)
    assert '99999' not in result['text']


def test_ambiguous_aggregate_is_rejected_by_claim_scope_then_can_clarify(joined):
    aggregate='SELECT SUM(r.amount) AS amount,r.unit FROM readings r JOIN sites s ON r.site_id=s.id WHERE s.name=? GROUP BY r.unit'
    plan={'actions':[{'id':'S1','kind':'sql','source_id':'s','sql':aggregate,'parameters':['重名点']}]}
    def bad(p):
        e=next(e for e in p['evidence'] if e.get('query_result_context'))
        assert not e['direct_columns'] and e['query_result']['rows']==[[75,'次']]
        # Explicit adversarial unitless selector gets past the renderer so this
        # verifies the REAL controller's homonym gate, not the missing unit gate.
        return {'status':'completed','claims':[{'query_fact':spec(e,unit=False),'text':'重名点合计75次。'}]}
    clarification={'status':'needs_clarification','clarification':{'question':'你指北岸D01还是南岸D02？','field':'site_id','options':['D01 北岸','D02 南岸']}}
    g=Gateway([plan,bad,clarification,{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True}])
    status,result=answer_cross_source([joined],'重名点巡检多少次？',[],g)
    assert status=='needs_clarification' and not result['claims']
    assert result['clarification']['options']==['D01 北岸','D02 南岸']
    assert any('同名' in e['message'] for e in result['diagnostics']['tool_errors'])
    assert [role for role,_,_ in g.calls]==['table','table','table','verify']
