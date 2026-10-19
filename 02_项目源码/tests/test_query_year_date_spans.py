"""A selected year must not consume the prefix of a complete date output."""
import copy

import pytest

from backend.query_fact_renderer import render_query_fact
from backend.query_result_context import build_query_result_context, check_query_bindings


def date_output():
    evidence = {
        'id': 'E6', 'kind': 'table', 'source_id': 'boundary-fixture',
        'sha256': 'a' * 64, 'parser_version': 'test', 'stale': False,
        'query': {
            'sql': "SELECT strftime('%Y', day) AS reg_year, MIN(day) AS min_day, "
                   "MAX(day) AS max_day FROM orders GROUP BY strftime('%Y', day)",
            'parameters': [],
        },
        'query_result': {
            'columns': ['reg_year', 'min_day', 'max_day'],
            'rows': [['2025', '2025-01-01', '2025-05-01']],
            'truncated': False,
        },
    }
    evidence['query_result_context'] = build_query_result_context(evidence)
    return evidence


def selection(evidence, column=1):
    context = evidence['query_result_context']
    cells = context['rows'][0]['cells']
    return {'evidence_id': evidence['id'], 'context_id': context['context_alias'],
            'cell_id': cells[column]['cell_id'], 'entity_cells': [cells[0]['cell_id']]}


@pytest.mark.parametrize('column,expected', [(1, '2025-01-01'), (2, '2025-05-01')])
def test_canonical_fact_preserves_independent_year_and_complete_date(column, expected):
    evidence = date_output()
    before = copy.deepcopy(evidence)
    rendered = render_query_fact(selection(evidence, column), [evidence])
    claim = rendered['claim']
    assert f'「{expected}」' in claim['text'] and '「2025」' in claim['text']
    checked = check_query_bindings(claim['text'], claim['query_bindings'], [evidence])
    assert checked['valid']
    assert checked['bindings'][0]['semantic_review_required'] is True
    assert rendered['audit']['publication_status'] == 'not_adopted'
    assert evidence == before


def test_natural_statement_with_separate_year_and_full_date_passes():
    evidence = date_output()
    binding = render_query_fact(selection(evidence), [evidence])['claim']['query_bindings'][0]
    text = '2025年已完成订单的最早日期为2025-01-01。'
    binding['statement'] = text
    assert check_query_bindings(text, [binding], [evidence])['valid']


@pytest.mark.parametrize('text', [
    '2025年已完成订单的最早日期为2025-01-02。',  # wrong day
    '2025年已完成订单的最早日期为2024-01-01。',  # wrong year
    '2025年已完成订单的最早日期为2025-01。',     # missing day
    '2025年已完成订单的最早日期为01-01。',       # missing date year
    '2025年已完成订单的最早日期未知。',          # missing full date
    '最早日期为2025-01-01。',                    # date cannot stand in for entity year
    '2024年已完成订单的最早日期为2025-01-01。',  # wrong entity year
    '2025年最早日期为2025-01-01，另有8条。',     # unsupported additional value
])
def test_wrong_or_incomplete_date_or_entity_remains_rejected(text):
    evidence = date_output()
    binding = render_query_fact(selection(evidence), [evidence])['claim']['query_bindings'][0]
    binding['statement'] = text
    checked = check_query_bindings(text, [binding], [evidence])
    assert not checked['valid'] and checked['errors']


def test_year_prefix_in_other_identifier_cannot_supply_missing_entity():
    evidence = date_output()
    binding = render_query_fact(selection(evidence), [evidence])['claim']['query_bindings'][0]
    text = '编号2025-99的最早日期为2025-01-01。'
    binding['statement'] = text
    assert not check_query_bindings(text, [binding], [evidence])['valid']
