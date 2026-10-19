"""Offline feedback checks: no alias permissions or automatic publications."""
import copy
import json

from backend.cross_source_engine import _binding_repair_contract
from backend.table_bindings import check_table_bindings
from backend.table_context import context_for_table
from test_cross_source_engine import sources, structured_pdf_table


def example(sources):
    structured_pdf_table(sources, rows=[
        ['Station', 'Annual average', 'Monthly average', 'Notes'],
        ['East', '12', '12', 'ordinary'],
        ['West', '12', '12', 'ordinary'],
    ])
    evidence = {'E1': {'id': 'E1', 'kind': 'text', 'source_id': 'db', 'sha256': 'dbhash',
        'parser_version': 'test', 'quote': 'East | 12 | 12 | ordinary',
        'table_context': context_for_table(sources[0], 'orders', [2])}}
    text = '东站的全年平均值为12。'
    candidate = {'text': text, 'evidence_ids': ['E1'], 'table_bindings': [{
        'evidence_id': 'E1', 'table_name': 'orders', 'cell_id': 'R2C2', 'value': '12',
        'statement': text, 'column_label': 'Annual average', 'row_label': 'East'}]}
    return evidence, candidate


def test_exact_cell_feedback_identifies_missing_source_labels_without_adopting_translation(sources):
    evidence, candidate = example(sources)
    before = copy.deepcopy((evidence, candidate))
    feedback = _binding_repair_contract(candidate, evidence, 2)
    contract = feedback['binding_diagnostics'][0]['source_label_contract']
    assert contract['missing_column_labels'] == ['Annual average']
    assert contract['missing_row_labels'] == ['East']
    assert (contract['source_id'], contract['sha256'], contract['evidence_id'], contract['cell_id']) == ('db', 'dbhash', 'E1', 'R2C2')
    assert contract['publication_status'] == 'repair_diagnostic_not_semantic_approval'
    assert len(json.dumps(feedback, ensure_ascii=False)) <= 6500
    assert (evidence, candidate) == before
    assert not check_table_bindings(candidate['text'], candidate['table_bindings'], list(evidence.values()))['valid']


def test_explicit_original_labels_allow_candidate_but_monthly_scope_still_cannot_replace_annual(sources):
    evidence, candidate = example(sources)
    candidate['text'] = '东站（East）的全年平均值（Annual average）为12。'
    candidate['table_bindings'][0]['statement'] = candidate['text']
    assert check_table_bindings(candidate['text'], candidate['table_bindings'], list(evidence.values()))['valid']
    candidate['text'] = candidate['text'].replace('Annual average', 'Monthly average')
    candidate['table_bindings'][0]['statement'] = candidate['text']
    assert not check_table_bindings(candidate['text'], candidate['table_bindings'], list(evidence.values()))['valid']


def test_unknown_or_uncited_cell_never_receives_verified_label_contract(sources):
    evidence, candidate = example(sources)
    for mutation in ('unknown_cell', 'uncited', 'wrong_value'):
        current = copy.deepcopy(candidate)
        if mutation == 'unknown_cell': current['table_bindings'][0]['cell_id'] = 'R99C2'
        elif mutation == 'uncited': current['evidence_ids'] = []
        elif mutation == 'wrong_column': current['table_bindings'][0]['column_label'] = 'Monthly average'
        elif mutation == 'wrong_value': current['table_bindings'][0]['value'] = '13'
        feedback = _binding_repair_contract(current, evidence, 1)
        assert 'source_label_contract' not in feedback['binding_diagnostics'][0]


def test_equal_numeric_value_other_row_is_not_source_identity(sources):
    evidence, candidate = example(sources)
    candidate['table_bindings'][0]['cell_id'] = 'R3C2'
    candidate['table_bindings'][0]['row_label'] = 'West'
    feedback = _binding_repair_contract(candidate, evidence, 1)
    assert 'source_label_contract' not in feedback['binding_diagnostics'][0]
    assert not check_table_bindings(candidate['text'], candidate['table_bindings'], list(evidence.values()))['valid']


def test_already_explicit_labels_have_no_spurious_missing_terms(sources):
    evidence, candidate = example(sources)
    candidate['text'] = 'East的Annual average为12。'
    candidate['table_bindings'][0]['statement'] = candidate['text']
    feedback = _binding_repair_contract(candidate, evidence, 1)
    contract = feedback['binding_diagnostics'][0]['source_label_contract']
    assert contract['missing_column_labels'] == contract['missing_row_labels'] == []


def test_misnamed_column_or_row_is_diagnosed_from_unique_real_cell_not_accepted(sources):
    evidence, candidate = example(sources)
    candidate['table_bindings'][0]['column_label'] = 'Station / Annual average'
    candidate['table_bindings'][0]['row_label'] = 'West'
    before = copy.deepcopy((candidate, evidence))
    feedback = _binding_repair_contract(candidate, evidence, 1)
    contract = feedback['binding_diagnostics'][0]['source_label_contract']
    assert contract['expected_column_label'] == 'Annual average'
    assert contract['provided_column_label'] == 'Station / Annual average'
    assert contract['expected_row_label'] == 'East'
    assert contract['provided_row_label'] == 'West'
    assert contract['column_label_matches_source'] is False
    assert contract['row_label_matches_source'] is False
    assert not check_table_bindings(candidate['text'], candidate['table_bindings'], list(evidence.values()))['valid']
    assert (candidate, evidence) == before


def test_ambiguous_same_cell_identity_never_produces_label_contract(sources):
    evidence, candidate = example(sources)
    table = evidence['E1']['table_context']['tables'][0]
    conflicting = copy.deepcopy(table)
    conflicting['page'] = 2
    evidence['E1']['table_context']['tables'].append(conflicting)
    feedback = _binding_repair_contract(candidate, evidence, 1)
    assert 'source_label_contract' not in feedback['binding_diagnostics'][0]


def test_same_value_in_different_cited_source_is_not_used_to_repair_unknown_evidence(sources):
    evidence, candidate = example(sources)
    evidence['E2'] = copy.deepcopy(evidence['E1'])
    evidence['E2'].update(id='E2', source_id='other', sha256='otherhash')
    candidate['evidence_ids'] = ['E1', 'E2']
    candidate['table_bindings'][0]['evidence_id'] = 'E999'
    feedback = _binding_repair_contract(candidate, evidence, 1)
    assert 'source_label_contract' not in feedback['binding_diagnostics'][0]
