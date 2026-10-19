"""Saved real receipts + synthetic safety boundaries; zero model calls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.formula_engine import calculate_formula
from backend.formula_unit_evidence import explicit_dimensionless_parameter
from test_formula_dimensionless_evidence import records, STATEMENT


def saved(phase):
    payload = json.loads((Path(__file__).parent / 'fixtures' / f'formula016-{phase}.json').read_text())
    return {e['id']: e for e in payload['evidence']}


@pytest.mark.parametrize('phase', ['r4', 'r5'])
def test_original_six_live_parameters_resolve_without_modifying_received_evidence(phase):
    evidence = saved(phase); original = deepcopy(evidence)
    parameters = [e for e in evidence.values() if e['kind'] == 'formula_parameter']
    assert len(parameters) == 6
    for row in parameters:
        proof = explicit_dimensionless_parameter(row, evidence)
        assert proof is not None and proof['normalized_unit'] == '1'
        assert proof['source_position']['page'] == 2
        assert proof['original_unit'] is None and row['parameter']['unit'] is None
        text = evidence[proof['evidence_id']]['quote']
        assert text[proof['start']:proof['end']] == proof['quote']
        assert text[proof['reviewed_quote_start']:proof['reviewed_quote_end']] == row['parameter']['quote']
        assert proof['reviewed_parameter_quote'] == row['parameter']['quote']
    assert evidence == original


@pytest.mark.parametrize('phase', ['r4', 'r5'])
def test_valid_synthetic_bindings_to_saved_receipts_compute_5_and_8(phase):
    evidence = saved(phase); original = deepcopy(evidence)
    rules = [e for e in evidence.values() if e['kind'] == 'formula_structure']
    assert len(rules) == 2
    for rule, expression, expected in zip(rules, ['(a+b)/c', 'a+b/c'], ['5', '8']):
        parameters = [e for e in evidence.values() if e['kind'] == 'formula_parameter' and e['formula_id'] == rule['formula_id']]
        bindings = {e['parameter']['symbol']: {'evidence_id': e['id'], 'formula_id': e['formula_id'], 'parameter_id': e['parameter_id'], 'unit': '1'} for e in parameters}
        output = calculate_formula(expression, bindings, evidence, '1', rule_evidence_ids=[rule['id']])
        assert output['value'] == expected
        assert len(output['parameters']) == 3
        assert all(p['unit_normalization']['normalized_unit'] == '1' for p in output['parameters'])
    assert evidence == original


@pytest.mark.parametrize('terminator', ['。', '；', ';'])
def test_full_affirmative_quote_accepts_only_present_declarative_terminator(terminator):
    evidence = records()
    evidence['T1']['quote'] = '原页标题。\n' + STATEMENT + terminator + '原页结尾。'
    evidence['E1']['parameter']['quote'] = STATEMENT + terminator
    proof = explicit_dimensionless_parameter(evidence['E1'], evidence)
    assert proof and proof['quote'] == '\n' + STATEMENT + terminator
    assert evidence['T1']['quote'][proof['start']:proof['end']] == proof['quote']


@pytest.mark.parametrize('quote', [
    STATEMENT + '？', STATEMENT + '!', STATEMENT + '。原页结尾。',
    'a=6。', '均为明确无量纲数。', STATEMENT[:-1] + '。',
    STATEMENT + '；', STATEMENT + '。。', STATEMENT.replace('、', '。') + '。',
])
def test_incomplete_cross_sentence_nonmatching_or_nonaffirmative_quote_is_rejected(quote):
    evidence = records(); evidence['E1']['parameter']['quote'] = quote
    assert explicit_dimensionless_parameter(evidence['E1'], evidence) is None


@pytest.mark.parametrize('source,quote', [
    ('本页a=6、b=4、c=2，并非均为明确无量纲数', STATEMENT+'。'),
    ('本页a=6、b=4、c=2。均为明确无量纲数', '本页a=6、b=4、c=2。均为明确无量纲数。'),
    ('假设\n'+STATEMENT, STATEMENT+'。'),
    (STATEMENT+'但仅限对照组', STATEMENT+'。'),
    ('本页a=6、b=4、c=2', '本页a=6、b=4、c=2。'),
    ('“'+STATEMENT+'”是错误声明', STATEMENT+'。'),
])
def test_no_internal_qualifier_negation_or_boundary_is_deleted(source, quote):
    evidence = records(source); evidence['E1']['parameter']['quote'] = quote
    assert explicit_dimensionless_parameter(evidence['E1'], evidence) is None


@pytest.mark.parametrize('field,value', [('source_id', 'other'), ('sha256', 'b'*64), ('parser_version', 'old'), ('page', 1)])
def test_punctuation_support_cannot_cross_original_position(field, value):
    evidence = records(); evidence['E1']['parameter']['quote'] = STATEMENT+'。'
    evidence['T1'][field] = value
    assert explicit_dimensionless_parameter(evidence['E1'], evidence) is None
