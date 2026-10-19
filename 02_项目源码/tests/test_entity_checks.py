"""Generic entity candidates on real SQLite data, without business-name rules."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend import entity_checks,parsers
from backend.entity_checks import inspect_bindings


def source_for(path):
    tables,_=parsers.sqlite_tables(path)
    return {'id':'fixture','name':'entities','data_path':str(path),'tables':tables,'sha256':'fixture','parser_version':'test'}


@pytest.fixture
def entities(tmp_path):
    path=tmp_path/'entities.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('''
        CREATE TABLE parent_unit(unit_id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE objects(object_id INTEGER PRIMARY KEY, name VARCHAR(120), country TEXT, unit_id INTEGER REFERENCES parent_unit(unit_id), note TEXT);
        CREATE TABLE other(other_id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO parent_unit VALUES(1,'Alpha'),(2,'Beta');
        INSERT INTO objects VALUES(1,'Echo','USA',1,'first'),(2,'Echo','USA',2,'second'),(3,'Solo','USA',1,'third'),(4,'O''Reilly','UK',2,'quoted');
        INSERT INTO other VALUES(10,'Echo');
        ''')
    return source_for(path)


def checked(results):
    return [r for r in results if r['status']=='checked']


def test_duplicate_name_candidates_include_pk_fk_and_real_values(entities):
    records=checked(inspect_bindings(entities,'SELECT object_id FROM objects o WHERE o.name=?',['Echo']))
    assert len(records)==1
    r=records[0]
    assert (r['table'],r['column'],r['value'],r['matched_rows'])==('objects','name','Echo',2)
    assert r['primary_key']==['object_id']
    assert 'unit_id' in r['candidate_columns']
    mapped=[dict(zip(r['candidate_columns'],row)) for row in r['candidates']]
    assert [(m['object_id'],m['unit_id'],m['note']) for m in mapped]==[(1,1,'first'),(2,2,'second')]
    assert not r['truncated']


def test_nonunique_dimension_is_reported_not_declared_ambiguous(entities):
    r=checked(inspect_bindings(entities,"SELECT COUNT(*) FROM objects WHERE country='USA'"))[0]
    assert r['matched_rows']==3 and r['value']=='USA'
    assert 'ambiguous' not in r and 'needs_clarification' not in r
    assert r['stats_scope']=='independent_equality'


def test_literal_escaping_and_bound_injection_are_only_values(entities):
    before=Path(entities['data_path']).read_bytes()
    first=checked(inspect_bindings(entities,"SELECT * FROM objects WHERE name='O''Reilly'"))[0]
    assert first['matched_rows']==1 and first['value']=="O'Reilly"
    injection="Echo' OR 1=1; DROP TABLE objects --"
    second=checked(inspect_bindings(entities,'SELECT * FROM objects WHERE name=?',[injection]))[0]
    assert second['matched_rows']==0 and second['value']==injection
    assert Path(entities['data_path']).read_bytes()==before


def test_alias_resolution_does_not_probe_other_same_named_column(entities):
    results=checked(inspect_bindings(entities,"SELECT o.object_id FROM objects o JOIN other x ON o.name=x.name WHERE x.name='Echo'"))
    assert len(results)==1 and results[0]['table']=='other' and results[0]['matched_rows']==1
    skipped=inspect_bindings(entities,"SELECT * FROM objects o JOIN other x ON o.name=x.name WHERE name='Echo'")
    assert skipped[0]['status']=='skipped' and '同名' in skipped[0]['reason']


def test_cte_inner_binding_uses_lexical_parameter_order(entities):
    sql='WITH chosen AS(SELECT * FROM objects WHERE country=?) SELECT * FROM objects WHERE name=?'
    records=checked(inspect_bindings(entities,sql,['UK','Echo']))
    assert {(r['column'],r['value'],r['matched_rows']) for r in records}=={('country','UK',1),('name','Echo',2)}


def test_cte_output_alias_is_not_assumed_to_be_original_column(entities):
    sql="WITH renamed AS(SELECT object_id, country AS name FROM objects) SELECT * FROM renamed WHERE name='USA'"
    records=inspect_bindings(entities,sql)
    assert len(records)==1 and records[0]['status']=='skipped'
    assert records[0]['table'] is None and records[0]['matched_rows'] is None
    assert '派生表' in records[0]['reason']


def test_derived_scope_and_unqualified_column_not_misattributed(entities):
    sql="SELECT * FROM objects o JOIN (SELECT name FROM other) x ON 1=1 WHERE name='Echo'"
    records=inspect_bindings(entities,sql)
    assert records[0]['status']=='skipped' and '派生表' in records[0]['reason']


def test_named_parameters_repeated_and_mixed_list_mapping(entities):
    sql='SELECT * FROM objects WHERE name=:n OR note=:n OR country=:c'
    records=checked(inspect_bindings(entities,sql,{'n':'Echo','c':'USA'}))
    assert {(r['column'],r['value'],r['matched_rows']) for r in records}=={('name','Echo',2),('note','Echo',0),('country','USA',3)}
    mixed='SELECT ? AS flag FROM objects WHERE name=:n OR note=:n OR country=?'
    records=checked(inspect_bindings(entities,mixed,[123,'Echo','USA']))
    assert {(r['column'],r['value']) for r in records}=={('name','Echo'),('note','Echo'),('country','USA')}


def test_in_text_values_are_individually_counted_and_bounded(entities):
    records=checked(inspect_bindings(entities,'SELECT * FROM objects WHERE name IN (?,?)',['Echo','Solo']))
    assert [(r['value'],r['matched_rows'],r['predicate']) for r in records]==[('Echo',2,'in'),('Solo',1,'in')]
    excessive=inspect_bindings(entities,'SELECT * FROM objects WHERE name IN ('+','.join('?' for _ in range(7))+')',['Echo']*7)
    assert len(excessive)==1 and excessive[0]['status']=='skipped' and '6项' in excessive[0]['reason']


def test_six_binding_limit_and_duplicate_deduplication(entities):
    sql='SELECT * FROM objects WHERE '+ ' OR '.join('name=?' for _ in range(7))
    result=inspect_bindings(entities,sql,[str(i) for i in range(7)])
    assert len(checked(result))==6 and result[-1]['status']=='skipped'
    duplicate=checked(inspect_bindings(entities,"SELECT * FROM objects WHERE name='Echo' OR name='Echo'"))
    assert len(duplicate)==1


def test_negated_expression_and_projection_equality_are_not_entity_filter(entities):
    assert inspect_bindings(entities,"SELECT name='Echo' AS same FROM objects")==[]
    negated=inspect_bindings(entities,"SELECT * FROM objects WHERE NOT name='Echo'")
    assert negated[0]['status']=='skipped' and '否定' in negated[0]['reason']
    expression=inspect_bindings(entities,"SELECT * FROM objects WHERE lower(name)='echo'")
    assert expression[0]['status']=='skipped' and '表达式' in expression[0]['reason']


def test_original_collation_is_respected(tmp_path):
    path=tmp_path/'collation.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE people(id INTEGER PRIMARY KEY,name TEXT COLLATE NOCASE);INSERT INTO people VALUES(1,'Echo'),(2,'ECHO');")
    result=checked(inspect_bindings(source_for(path),"SELECT * FROM people WHERE name='echo'"))[0]
    assert result['matched_rows']==2


def test_independent_count_not_final_query_filtered_count(entities):
    result=checked(inspect_bindings(entities,"SELECT * FROM objects WHERE name='Echo' AND unit_id=1"))[0]
    assert result['matched_rows']==2 and result['stats_scope']=='independent_equality'
    assert '不代表完整查询' in result['note']


def test_candidates_capped_count_remains_exact(tmp_path):
    path=tmp_path/'many.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE entries(id INTEGER PRIMARY KEY,label TEXT)')
        c.executemany('INSERT INTO entries VALUES(?,?)',[(i,'repeated') for i in range(120)])
    result=checked(inspect_bindings(source_for(path),"SELECT * FROM entries WHERE label='repeated'"))[0]
    assert result['matched_rows']==120 and len(result['candidates'])==8 and result['truncated']


def test_long_fields_and_report_size_are_bounded(tmp_path):
    path=tmp_path/'long.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE t(id INTEGER PRIMARY KEY,name TEXT,note TEXT)')
        c.executemany('INSERT INTO t VALUES(?,?,?)',[(i,'Echo','x'*2000) for i in range(10)])
    result=checked(inspect_bindings(source_for(path),"SELECT * FROM t WHERE name='Echo'"))[0]
    assert result['truncated'] and max(len(str(cell)) for row in result['candidates'] for cell in row)<=161
    assert len(json.dumps(result,ensure_ascii=False))<18000


@pytest.mark.parametrize('sql,parameters',[
    ('DELETE FROM objects',[]),
    ("SELECT * FROM objects WHERE name='Echo'; DELETE FROM objects",[]),
    ("SELECT * FROM sqlite_schema WHERE name='objects'",[]),
    ("SELECT * FROM other.objects WHERE name='Echo'",[]),
    ("SELECT * FROM missing WHERE name='Echo'",[]),
    ('SELECT * FROM objects WHERE name=?',{}),
    ('SELECT * FROM objects WHERE name=?',[]),
    ('SELECT * FROM objects WHERE name=$name',{'name':'Echo'}),
])
def test_unsafe_or_unmappable_sql_is_explicitly_skipped(entities,sql,parameters):
    before=Path(entities['data_path']).read_bytes()
    result=inspect_bindings(entities,sql,parameters)
    assert result and all(r['status']=='skipped' for r in result)
    assert Path(entities['data_path']).read_bytes()==before


def test_scope_cannot_be_widened_by_metadata_name(entities):
    restricted={**entities,'tables':[t for t in entities['tables'] if t['name']=='objects']}
    result=inspect_bindings(restricted,"SELECT * FROM other WHERE name='Echo'")
    assert result[0]['status']=='skipped' and result[0]['matched_rows'] is None


def test_explicit_cancellation_and_time_budget(entities,monkeypatch):
    with pytest.raises(InterruptedError):
        inspect_bindings(entities,"SELECT * FROM objects WHERE name='Echo'",cancelled=lambda:True)
    monkeypatch.setattr(entity_checks,'MAX_SECONDS',-1)
    result=inspect_bindings(entities,"SELECT * FROM objects WHERE name='Echo'")
    assert result[0]['status']=='skipped' and result[0]['matched_rows'] is None
