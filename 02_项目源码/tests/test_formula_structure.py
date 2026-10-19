"""Synthetic OOXML fixtures exercise syntax, provenance and negative boundaries."""
import copy
from xml.etree import ElementTree as ET

import pytest

from backend import formula_structure as fs


def run(text):
    return '<m:r><m:t>' + text + '</m:t></m:r>'


def formula(body):
    node = ET.fromstring(f'<m:oMath xmlns:m="{fs.M}">{body}</m:oMath>')
    return fs.from_omml(node, identifier='F1', location={'page': 1, 'start': 0, 'end': 10, 'page_unit': 'logical_section'})


def fraction(a='a', b='b', properties=''):
    return '<m:f>' + properties + '<m:num>' + run(a) + '</m:num><m:den>' + run(b) + '</m:den></m:f>'


def test_fraction_preserves_tree_and_separates_display_from_arithmetic():
    record = formula(run('R=') + fraction())
    assert record['structure_status'] == 'parsed'
    assert record['tree']['children'][1]['type'] == 'fraction'
    assert record['latex'] == r'R=\frac{a}{b}'
    assert 'mfrac' in record['mathml']
    assert record['computation_ast']['right']['operator'] == '/'
    assert record['computation_ast']['right']['left'] == {'type': 'symbol', 'name': 'a'}
    assert record['verified'] is False and record['execution_eligible'] is False
    assert record['xml_sha256'] and '<ns0:num>' in record['original_xml']


@pytest.mark.parametrize('tag,sub,sup', [('sSub', 'i', None), ('sSup', None, '2'), ('sSubSup', 'i', '2')])
def test_sub_and_sup_are_not_flattened(tag, sub, sup):
    body = f'<m:{tag}><m:e>{run("x")}</m:e>'
    body += f'<m:sub>{run(sub)}</m:sub>' if sub else ''
    body += f'<m:sup>{run(sup)}</m:sup>' if sup else ''
    record = formula(body + f'</m:{tag}>')
    assert record['tree']['type'] == 'script'
    assert record['computation_ast']
    if sub and sup:
        assert record['computation_ast']['left'] == {'type': 'symbol', 'name': 'x', 'subscript': 'i'}
    elif sub:
        assert record['computation_ast']['subscript'] == 'i'


@pytest.mark.parametrize('degree,hidden,expected', [('', False, 2), ('3', False, 3), ('7', True, 2)])
def test_roots_preserve_degree_and_hide_property(degree, hidden, expected):
    record = formula('<m:rad><m:radPr><m:degHide m:val="' + ('1' if hidden else '0') + '"/></m:radPr><m:deg>' + run(degree) + '</m:deg><m:e>' + run('x+1') + '</m:e></m:rad>')
    assert record['tree']['degree_hidden'] is hidden
    assert record['computation_ast']['degree'] == expected
    assert record['computation_ast']['radicand']['operator'] == '+'


def test_explicit_finite_sum_preserves_index_and_bounds_without_evaluating():
    record = formula('<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr><m:sub>' + run('i=1') + '</m:sub><m:sup>' + run('n') + '</m:sup><m:e>' + run('i*2') + '</m:e></m:nary>')
    assert record['computation_ast']['type'] == 'finite_sum'
    assert record['computation_ast']['index'] == 'i'
    assert record['computation_ast']['execution_term_limit'] == 10000
    assert not record['execution_eligible']


@pytest.mark.parametrize('operator,lower,upper', [('∫', '0', '1'), ('∑', 'i', 'n'), ('∑', 'i=1', '1.5')])
def test_unbounded_or_unsupported_operator_stays_display_only(operator, lower, upper):
    record = formula(f'<m:nary><m:naryPr><m:chr m:val="{operator}"/></m:naryPr><m:sub>{run(lower)}</m:sub><m:sup>{run(upper)}</m:sup><m:e>{run("x")}</m:e></m:nary>')
    assert record['tree']['type'] == 'nary'
    assert record['computation_ast'] is None and record['issues']


def test_piecewise_retains_conditions_without_guessing_a_calculation():
    matrix = '<m:m>' + ''.join('<m:mr><m:e>' + run(v) + '</m:e><m:e>' + run(c) + '</m:e></m:mr>' for v, c in [('x', 'x≥0'), ('-x', 'x&lt;0')]) + '</m:m>'
    record = formula('<m:d><m:dPr><m:begChr m:val="{"/><m:endChr m:val=""/></m:dPr><m:e>' + matrix + '</m:e></m:d>')
    assert record['tree']['type'] == 'cases'
    assert record['tree']['branches'][1]['condition']['text'] == 'x<0'
    assert r'\begin{cases}' in record['latex'] and 'mtable' in record['mathml']
    assert record['computation_ast'] is None


def test_nested_parentheses_preserve_operator_precedence():
    record = formula('<m:d><m:e>' + run('a+b') + '</m:e></m:d>' + run('*c'))
    tree = record['computation_ast']
    assert tree['operator'] == '*' and tree['left']['operator'] == '+'


@pytest.mark.parametrize('text', ['__import__(x)', 'a.b', 'x[0]', 'x%3', 'OMMLATOM0', '1e9', '2 x', 'a=b=c'])
def test_not_permitted_to_turn_ambiguous_or_executable_text_into_ast(text):
    record = formula(run(text))
    assert record['computation_ast'] is None
    assert not record['execution_eligible'] and record['issues']


def test_nobar_fraction_is_not_division_and_unknown_tag_is_not_dropped():
    no_bar = formula(fraction(properties='<m:fPr><m:type m:val="noBar"/></m:fPr>'))
    assert no_bar['computation_ast'] is None
    unknown = formula('<m:phant><m:e>' + run('999') + '</m:e></m:phant>' + run('+1'))
    assert unknown['structure_status'] == 'partial'
    assert unknown['unsupported_tags'] == ['phant']
    assert unknown['computation_ast'] is None and '999' in unknown['original_xml']


def test_missing_denominator_and_budget_have_visible_unknown_state(monkeypatch):
    invalid = formula('<m:f><m:num>' + run('a') + '</m:num></m:f>')
    assert invalid['structure_status'] == 'unknown'
    assert invalid['issues'] == ['missing_den']
    monkeypatch.setattr(fs, 'MAX_XML', 20)
    bounded = formula(run('a+b'))
    assert bounded['xml_truncated'] and bounded['tree'] is None
    assert bounded['computation_status'] == 'unavailable'


def test_deep_omml_is_bounded_before_recursive_serialization():
    record = formula('<m:e>' * 80 + run('1') + '</m:e>' * 80)
    assert record['structure_status'] == 'unknown'
    assert record['issues'] == ['formula_tree_limit']
    assert record['xml_truncated'] is True


@pytest.mark.parametrize('origin', ['pdf_native_text', 'ocr_text'])
def test_pdf_and_scanned_linear_tokens_never_become_verified_structure(origin):
    record = fs.from_text('x 2 = a b', identifier='F1', location={'page': 1, 'bbox': [.1, .2, .8, .3]}, origin=origin)
    assert record['tree'] is None and record['computation_ast'] is None
    assert record['text'] == 'x 2 = a b'
    assert record['structure_status'] == 'unknown' and not record['verified']


def test_parameter_units_and_conflicts_are_exact_source_candidates():
    text = '说明 a=12万元，b=3小时。\n更正 a=13万元。\n未说明 c=4。'
    blocks, offset = [], 0
    for line in text.split('\n'):
        blocks.append({'text': line, 'start': offset, 'end': offset + len(line), 'kind': 'text'})
        offset += len(line) + 1
    pages = [{'number': 1, 'text': text, 'blocks': blocks}]
    before = copy.deepcopy(pages)
    candidates = fs.parameter_candidates({'source_position': {'page': 1, 'start': 0, 'end': 1, 'page_unit': 'logical_section'}}, pages)
    assert [c['value_text'] for c in candidates] == ['12', '3', '13', '4']
    assert [c['unit_text'] for c in candidates] == ['万元', '小时', '万元', None]
    assert candidates[0]['conflict'] is True and candidates[1]['conflict'] is False
    assert candidates[-1]['unit_status'] == 'unknown'
    for candidate in candidates:
        p = candidate['evidence']
        assert text[p['start']:p['end']] == p['quote']
        assert candidate['binding_status'] == 'candidate_requires_review'
    assert pages == before


def test_compound_unit_is_not_truncated_and_unknown_suffix_is_not_accepted():
    text = 'a=12万元/人，b=3kg/unknown，c=4kWh/m²。'
    pages = [{'number': 1, 'text': text, 'blocks': [{'text': text, 'start': 0, 'end': len(text)}]}]
    candidates = fs.parameter_candidates({'source_position': {'page': 1}}, pages)
    assert [(c['symbol'], c['unit_text']) for c in candidates] == [('a', '万元/人'), ('c', 'kWh/m²')]
