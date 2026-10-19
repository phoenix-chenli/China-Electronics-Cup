"""Actual SQLite results keep table structure and fail closed on missing review."""
import copy
import json
import sqlite3
import threading

import pytest

from backend.answer_service import AnswerService
from backend.parsers import sqlite_tables
from backend.query_planner import answer_database
from backend.schema_catalog import build_catalog
from backend.sql_engine import (MAX_TABLE_CONTEXT_CHARS, attach_table_contexts,
                                execute_sql, table_review_requirements)
from backend.table_semantics import build_table_semantics


class Responses:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def __call__(self, messages, purpose):
        self.calls.append((purpose, json.loads(messages[-1]['content'])))
        return self.values.pop(0)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'native-table.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE T("列1" TEXT,"列2" TEXT)')
        connection.executemany('INSERT INTO T VALUES(?,?)', [('项目', '金额（万元）'), ('甲', '10'), ('乙', '20')])
    values = [['项目', '金额（万元）'], ['甲', '10'], ['乙', '20']]
    table = build_table_semantics({'table_name': 'T', 'row_count': 3, 'column_count': 2,
        'cells': [{'row': r, 'col': c, 'text': value, 'rowspan': 1, 'colspan': 1}
                  for r, row in enumerate(values, 1) for c, value in enumerate(row, 1)]},
        format='docx', source_position={'page': 1}, explicit_header_rows=[1])
    tables, warnings = sqlite_tables(path)
    return {'id': 's', 'name': '原表.docx', 'sha256': 'fixture', 'parser_version': 'v1',
            'status': 'ready', 'archived': False,
            'format': 'docx', 'data_path': str(path), 'tables': tables, 'warnings': warnings,
            'metadata': {'table_semantics': [table], 'table_layouts': [{'name': 'T'}]}}


DETAIL_SQL = 'SELECT "列1","列2" FROM T WHERE "列1"=\'甲\''
AGGREGATE_SQL = 'SELECT SUM(CAST("列2" AS REAL)) AS 金额_万元 FROM T WHERE "列1"!=\'项目\''


def db_plan(sql=DETAIL_SQL):
    return {'status': 'ready', 'normalized_question': '甲的金额', 'intent': {'metric': '金额'},
            'sql': sql, 'parameters': []}


def table_plan(aggregate=False):
    plan = {'table': 'T', 'filters': [{'column': '列1', 'op': 'ne' if aggregate else 'eq',
                                     'value': '项目' if aggregate else '甲'}], 'limit': 10}
    if aggregate:
        plan['aggregates'] = [{'op': 'sum', 'column': '列2', 'alias': '金额_万元'}]
    else:
        plan['select'] = ['列1', '列2']
    return {'status': 'ready', 'plan': plan}


def call_table(source, gateway):
    return AnswerService._table(None, source, {'question': '甲的金额', 'table_name': 'T'},
                                gateway, threading.Event(), lambda _: None)


def accepted():
    return {'supported': True, 'interpretation_supported': True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True},
            'table_relations': [{'id': 'C1', 'supported': True}]}


def test_sql_detail_uses_genuine_source_rows_and_catalog_has_only_header_context(source):
    catalog = build_catalog(source)
    context = catalog['tables'][0]['table_context']['tables'][0]
    assert context['selection_state'] == 'table_only'
    assert context['selected_cell_ids'] == []
    assert {cell['row'] for cell in context['cells']} == {1}
    computed = execute_sql(source, DETAIL_SQL)
    record = computed['evidence'][0]
    assert record['lineage'] == 'row' and record['row_numbers'] == [2]
    table = record['table_context']['tables'][0]
    assert table['selected_cell_ids'] == ['R2C1', 'R2C2']
    assert {c['row'] for c in table['cells']} == {1, 2}
    assert table['units'][0]['value'] == '万元'


def test_sql_aggregate_never_fabricates_row_contributions(source):
    result = execute_sql(source, AGGREGATE_SQL)
    assert result['rows'] == [[30.0]]
    evidence = result['evidence'][0]
    assert evidence['lineage'] == 'table' and evidence['row_numbers'] == []
    table = evidence['table_context']['tables'][0]
    assert table['selection_state'] == 'table_only' and table['selected_cell_ids'] == []
    assert {cell['row'] for cell in table['cells']} == {1}


@pytest.mark.parametrize('runner', ['single', 'database'])
def test_table_structure_requires_explicit_independent_relation_review(source, runner):
    gateway = Responses(table_plan() if runner == 'single' else db_plan(), accepted())
    status, result = call_table(source, gateway) if runner == 'single' else answer_database(source, '甲的金额', [], gateway)
    assert status == 'completed' and result['rows'] == [['甲', '10']]
    assert result['verification']['table_relations'] == [{'id': 'C1', 'supported': True}]
    assert gateway.calls[-1][1]['table_relation_claim_ids'] == ['C1']
    assert 'table_context' in result['evidence'][0]


@pytest.mark.parametrize('review', [
    {'supported': True, 'interpretation_supported': True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}},
    {'supported': True, 'interpretation_supported': True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}, 'table_relations': [{'id': 'C1', 'supported': False}]},
    {'supported': True, 'interpretation_supported': True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}, 'table_relations': [{'id': 'C2', 'supported': True}]},
    {'supported': False, 'reason': '单位口径不一致', 'failure_kind': 'invalid_plan'},
])
@pytest.mark.parametrize('runner', ['single', 'database'])
def test_rejected_review_preserves_actual_query_rows_and_evidence_without_publishing(source, review, runner):
    # A received invalid_plan review now permits a new database candidate.
    # This fixture deliberately repeats the original AST, so recovery must stop
    # without another execution/review or upgrading the rejected result.
    recovery = runner == 'database' and review.get('failure_kind') == 'invalid_plan'
    gateway = Responses(table_plan() if runner == 'single' else db_plan(), review,
                        *([db_plan()] if recovery else []))
    status, result = call_table(source, gateway) if runner == 'single' else answer_database(source, '甲的金额', [], gateway)
    assert status == 'failed' and result['claims'] == [] and not result.get('rows')
    assert result['query']['sql']
    assert result['query']['executed_result']['rows'] == [['甲', '10']]
    assert result['evidence'][0]['table_context']['has_table']
    assert len(gateway.calls) == (3 if recovery else 2)
    assert gateway.values == []
    if recovery:
        assert [purpose for purpose, _ in gateway.calls] == ['table', 'verify', 'table']
        feedback = gateway.calls[-1][1]
        assert feedback['received_review'] == review
        assert feedback['rejected_plan']['sql'] == DETAIL_SQL
        assert feedback['question'] == '甲的金额'
        assert result['query']['semantic_recovery_stop'] == 'repeated_ast_and_parameters'
        reviews = result['query_diagnostics']['semantic_reviews']
        assert len(reviews) == 1 and reviews[0]['review_response'] == review
        assert reviews[0]['status'] == 'failed'
        assert reviews[0]['execution_preview']['rows'] == [['甲', '10']]


@pytest.mark.parametrize('failure', ['unknown', 'truncated', 'structure_unknown', 'legacy', 'missing'])
@pytest.mark.parametrize('runner', ['single', 'database'])
def test_unresolved_structure_cannot_be_overridden_by_a_positive_model_response(source, failure, runner):
    table = source['metadata']['table_semantics'][0]
    if failure == 'unknown':
        table['header_basis'] = 'unknown'
    elif failure in ('truncated', 'structure_unknown'):
        table[failure] = True
    elif failure == 'legacy':
        source['metadata'].pop('table_semantics')
    else:
        source['metadata'] = {}
    gateway = Responses(table_plan() if runner == 'single' else db_plan(), accepted())
    status, result = call_table(source, gateway) if runner == 'single' else answer_database(source, '甲的金额', [], gateway)
    assert status == 'failed' and result['claims'] == []
    assert result['query']['executed_result']['rows'] == [['甲', '10']]
    assert result['evidence'][0]['table_context']['has_table']
    assert len(gateway.calls) == 1  # No pointless verification or repeated request.


def test_single_table_aggregate_requests_table_level_review(source):
    gateway = Responses(table_plan(aggregate=True), accepted())
    status, result = call_table(source, gateway)
    assert status == 'completed' and result['rows'] == [['30']]
    table = result['evidence'][0]['table_context']['tables'][0]
    assert table['selection_state'] == 'table_only' and table['selected_cell_ids'] == []


@pytest.mark.parametrize('format', ['csv', 'sqlite'])
def test_plain_csv_and_database_keep_existing_review_contract(source, format):
    source['format'] = format
    source['metadata'] = {}
    gateway = Responses(db_plan(), {'supported': True, 'interpretation_supported': True,'scope_review':{'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':True}})
    status, result = answer_database(source, '甲的金额', [], gateway)
    assert status == 'completed' and result['rows'] == [['甲', '10']]
    assert 'table_context' not in result['evidence'][0]
    assert gateway.calls[-1][1]['table_relation_claim_ids'] == []


def test_total_structure_budget_keeps_explicit_boundaries_for_all_tables(source):
    prototype = source['metadata']['table_semantics'][0]
    tables = []
    for index in range(80):
        table = copy.deepcopy(prototype)
        table['table_name'] = f'T{index}'
        tables.append(table)
    source['metadata']['table_semantics'] = tables
    evidence = attach_table_contexts(source, [{'table_name': t['table_name'], 'lineage': 'table'} for t in tables])
    contexts = [e['table_context'] for e in evidence]
    assert len(json.dumps(contexts, ensure_ascii=False)) <= MAX_TABLE_CONTEXT_CHARS
    assert all(c['has_table'] for c in contexts) and any(c.get('truncated') for c in contexts)
    required, errors = table_review_requirements(evidence)
    assert required == ['C1'] and errors
