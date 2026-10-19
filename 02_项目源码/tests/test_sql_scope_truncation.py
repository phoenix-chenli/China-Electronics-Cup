"""Synthetic AST summaries only; no execution, reviewer, network or SQL edits."""
import pytest
from backend.sql_answer_contract import build_answer_scope


def scope(sql, columns=40):
    catalog={'tables':[{'name':'t','primary_key':['id'],
                        'columns':[{'name':'id'}]+[{'name':f'c{i}'} for i in range(columns-1)]}],
             'relationships':[]}
    return build_answer_scope(sql,{},catalog,{'sha256':'a'*64})


def test_thirty_three_actual_projections_have_an_explicit_summary_limit():
    sql='SELECT '+','.join(f'id AS c{i}' for i in range(33))+' FROM t'
    result=scope(sql)
    assert len(result['sql_scopes'][0]['projections'])==32
    assert result['truncated'] is True and result['sql_scopes'][0]['truncated'] is True
    assert sql.endswith('id AS c32 FROM t')


def test_small_complete_summary_is_not_marked_truncated():
    result=scope('SELECT id,c0 FROM t WHERE c0 IS NOT NULL GROUP BY id,c0')
    assert result['truncated'] is False and result['sql_scopes'][0]['truncated'] is False


@pytest.mark.parametrize('sql', [
    'SELECT id FROM t GROUP BY '+','.join(f'c{i}' for i in range(17)),
    'SELECT '+"'"+'x'*501+"' FROM t",
    'SELECT id FROM t GROUP BY '+"'"+'x'*501+"'",
    'SELECT id FROM t WHERE '+ ' OR '.join(f'c{i} IS NULL' for i in range(17)),
    'SELECT id FROM t WHERE '+"'"+'x'*301+"' IS NULL",
])
def test_each_local_summary_slice_is_visible(sql):
    result=scope(sql)
    assert result['truncated'] is True and result['sql_scopes'][0]['truncated'] is True


def test_outer_correlation_slice_is_marked_inside_quantifier_and_at_top():
    sql='SELECT a.id FROM t a WHERE EXISTS(SELECT b.id FROM t b WHERE '+ ' AND '.join(f'b.c{i}=a.c{i}' for i in range(17))+')'
    result=scope(sql);summary=result['sql_scopes'][0];quantifier=summary['quantifiers'][0]
    assert len(quantifier['outer_correlations'])==16
    assert quantifier['truncated'] and summary['truncated'] and result['truncated']


def test_quantifier_count_slice_is_marked():
    sql='SELECT a.id FROM t a WHERE '+ ' OR '.join(f'EXISTS(SELECT b.id FROM t b WHERE b.id=a.id AND b.c0={i})' for i in range(17))
    result=scope(sql)
    assert len(result['sql_scopes'][0]['quantifiers'])==16
    assert result['sql_scopes'][0]['truncated'] and result['truncated']


def test_column_inventory_slice_is_visible_at_top():
    result=scope('SELECT id FROM t',columns=41)
    assert len(result['source_tables'][0]['columns'])==40
    assert result['source_tables'][0]['columns_truncated'] and result['truncated']
