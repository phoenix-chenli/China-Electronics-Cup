"""Synthetic source-bound tests; a separate script replays the saved live record."""
from copy import deepcopy

import pytest

from backend.formula_engine import FormulaError, calculate_formula
from backend.formula_unit_evidence import explicit_dimensionless_parameter


POSITION = {'source_id': 'source-A', 'sha256': 'a'*64, 'parser_version': 'parser-1', 'page': 2}
STATEMENT = '本页a=6、b=4、c=2，均为明确无量纲数'


def records(text=None):
    evidence = {'T1': {'id': 'T1', 'kind': 'text', **POSITION,
        'quote': '原页标题。' + (STATEMENT if text is None else text) + '。原页结尾。'}}
    for index, (symbol, value) in enumerate((('a', '6'), ('b', '4'), ('c', '2')), 1):
        identity = 'E'+str(index)
        evidence[identity] = {'id': identity, 'kind': 'formula_parameter', **POSITION,
            'provenance': 'separate_formula_visual_read_and_review', 'visual_checked': True,
            'formula_verified': True, 'formula_id': 'F1', 'parameter_id': 'P'+str(index),
            'formula_parameter_conflicts': [], 'stale': False,
            'parameter': {'id': 'P'+str(index), 'symbol': symbol, 'value': value, 'unit': None,
                'unit_status': 'unknown', 'quote': symbol+'='+value,
                'unit_evidence': '均为明确无量纲数', 'verified': True,
                'binding_status': 'visually_supported_candidate', 'source_position': dict(POSITION)}}
    return evidence


def run(evidence, expression='(a+b)/c', unit='1'):
    names = ['a', 'b', 'c'] if expression != 'a' else ['a']
    bindings = {symbol: {'evidence_id': 'E'+str(index), 'formula_id': 'F1',
        'parameter_id': 'P'+str(index), 'unit': unit} for index, symbol in enumerate(names, 1)}
    return calculate_formula(expression, bindings, evidence, unit, rule_evidence_ids=['T1'])


def test_explicit_same_page_declaration_calculates_and_preserves_original_null():
    evidence = records(); original = deepcopy(evidence)
    first = run(evidence); second = run(evidence, 'a+b/c')
    assert first['value'] == '5' and second['value'] == '8'
    assert evidence == original
    for parameter in first['parameters']:
        receipt = parameter['unit_normalization']; text = evidence['T1']['quote']
        assert receipt['original_unit'] is None and receipt['normalized_unit'] == '1'
        assert receipt['meaning'] == 'dimensionless_explicitly_declared'
        assert text[receipt['start']:receipt['end']] == receipt['quote'] == STATEMENT
        assert text[receipt['assignment_start']:receipt['assignment_end']] == parameter['name']+'='+parameter['value']
        assert receipt['source_position'] == POSITION and receipt['offset_scope'] == 'evidence_quote'
        assert receipt['evidence_id'] in parameter['evidence_ids'] == [parameter['binding']['evidence_id'], 'T1']
    assert first['evidence_ids'] == ['T1', 'E1', 'E2', 'E3']


@pytest.mark.parametrize('text', [
    '本页a=6、b=4、c=2',
    '本页a=6、b=4、c=2，并非均为明确无量纲数',
    '本页a=6、b=4、c=2，不能认为均为明确无量纲数',
    '若本页a=6、b=4、c=2，均为明确无量纲数',
    '假设本页a=6、b=4、c=2，均为明确无量纲数',
    '示例本页a=6、b=4、c=2，均为明确无量纲数',
    '仅对照组本页a=6、b=4、c=2，均为明确无量纲数',
    '仅对照组：\n本页a=6、b=4、c=2，均为明确无量纲数',
    '假设\n本页a=6、b=4、c=2，均为明确无量纲数',
    '本页a=6 m、b=4、c=2，均为明确无量纲数',
    '本页a=6、b=4、c=2，均为明确无量纲数但仅适用历史实验',
    '本页x=6、y=4、z=2，均为明确无量纲数。a=6、b=4、c=2',
    '本页a=7、b=4、c=2，均为明确无量纲数',
    '本页a=6、a=6、b=4、c=2，均为明确无量纲数',
    '本页a=6、b=4、c=2，均为明确无量纲数。另一实验a=6 m',
    '“本页a=6、b=4、c=2，均为明确无量纲数”属于错误说明',
])
def test_missing_negative_conditional_units_and_wrong_scope_never_normalize(text):
    with pytest.raises(FormulaError, match='不能默认无量纲'):
        run(records(text))


@pytest.mark.parametrize('field,value', [('source_id', 'other'), ('sha256', 'b'*64),
    ('parser_version', 'other'), ('page', 3), ('stale', True)])
def test_text_evidence_must_match_same_original_revision_and_page(field, value):
    evidence = records(); evidence['T1'][field] = value
    with pytest.raises(FormulaError, match='不能默认无量纲'): run(evidence)


@pytest.mark.parametrize('mutation', ['not_verified', 'formula_unverified', 'wrong_provenance',
    'no_visual_check', 'position_mismatch', 'missing_position', 'conflict', 'wrong_symbol',
    'scope', 'period', 'bare_unit_word', 'foreign_unit_evidence', 'missing_quote', 'missing_text'])
def test_original_independent_review_and_exact_parameter_relation_required(mutation):
    evidence = records(); record = evidence['E1']; item = record['parameter']
    if mutation == 'not_verified': item['verified'] = False
    elif mutation == 'formula_unverified': record['formula_verified'] = False
    elif mutation == 'wrong_provenance': record['provenance'] = 'model_candidate'
    elif mutation == 'no_visual_check': record['visual_checked'] = False
    elif mutation == 'position_mismatch': item['source_position']['page'] = 1
    elif mutation == 'missing_position': item['source_position'].clear()
    elif mutation == 'conflict': record['formula_parameter_conflicts'] = ['a']
    elif mutation == 'wrong_symbol': item['symbol'] = 'x'
    elif mutation == 'scope': item['scope'] = '对照组'
    elif mutation == 'period': item['period'] = '2024年'
    elif mutation == 'bare_unit_word': item['unit_evidence'] = '无量纲'
    elif mutation == 'foreign_unit_evidence': item['unit_evidence'] = 'x、y均为明确无量纲数'
    elif mutation == 'missing_quote': item['quote'] = 'a=666'
    else: evidence.pop('T1')
    with pytest.raises(FormulaError): run(evidence)


def test_existing_explicit_unit_cannot_be_reclassified_by_dimensionless_prose():
    evidence = records(); evidence['E1']['parameter']['unit'] = 'm'
    evidence['E1']['parameter']['unit_status'] = 'explicit'
    with pytest.raises(FormulaError, match='单位不一致'): run(evidence, 'a')
    assert explicit_dimensionless_parameter(evidence['E1'], evidence) is None


def test_arbitrary_null_without_page_evidence_or_user_dimensionless_request_is_rejected():
    evidence = records(); evidence['T1']['quote'] = '按a直接计算。'
    binding = {'a': {'evidence_id': 'E1', 'formula_id': 'F1', 'parameter_id': 'P1', 'unit': '1'}}
    with pytest.raises(FormulaError, match='不能默认无量纲'):
        calculate_formula('a', binding, evidence, '1', user_formula='假设参数无量纲，按a计算')


@pytest.mark.parametrize('kind', ['one_text', 'total_text', 'records'])
def test_source_scan_budget_fails_closed_not_partial_proof(kind):
    evidence = records()
    if kind == 'one_text': evidence['T1']['quote'] += 'x'*26000
    elif kind == 'total_text':
        for index in range(3): evidence['T'+str(index+2)] = {**evidence['T1'], 'quote': 'x'*25000}
    else:
        for index in range(129): evidence['irrelevant'+str(index)] = {'kind': 'other'}
    with pytest.raises(FormulaError, match='不能默认无量纲'): run(evidence)


def test_cancel_and_output_dimension_gates_still_apply():
    evidence = records()
    with pytest.raises(InterruptedError):
        calculate_formula('a', {}, evidence, cancelled=lambda: True)
    with pytest.raises(FormulaError, match='量纲'):
        value = run(evidence)
        calculate_formula('a', {'a': {'calculation_id': 'K1', 'unit': '1'}}, evidence, 'm',
                          prior_calculations={'K1': value}, rule_evidence_ids=['T1'])


def test_another_symbol_and_value_work_without_question_specific_rules():
    evidence = records('参数z_1=−2.5、b=4、c=2，均为明确无量纲数')
    item = evidence['E1']['parameter']; item.update(symbol='z_1', value='−2.5', quote='z_1=−2.5')
    output = calculate_formula('x', {'x': {'evidence_id': 'E1', 'formula_id': 'F1',
        'parameter_id': 'P1', 'symbol': 'z_1', 'unit': '无量纲'}}, evidence, '1', rule_evidence_ids=['T1'])
    assert output['value'] == '-2.5' and output['parameters'][0]['unit_normalization']['symbol'] == 'z_1'
