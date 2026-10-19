"""Fixed conversion rules, pure offline assertions; no model/network calls."""
import pytest

from backend.calculation_requirements import direct_quantities
from backend.conversion_rules import validate_direct_conversion
from backend.formula_engine import FormulaError


def parameter(question='1000分是多少钱', **updates):
    literal = direct_quantities(question)[0]
    return {'name': 'a', 'unit': literal['unit'], 'value': literal['number'],
            'user_input': {**literal, 'parameter_kind': 'request_quantity'}, **updates}


def check(expression='a', rules=None, question='1000分是多少钱', output='元', parameters=None):
    return validate_direct_conversion(expression, parameters or [parameter(question)], output,
        ['金额原始单位为分。100分=1元。'] if rules is None else rules, question)


@pytest.mark.parametrize('rule', ['金额原始单位为分。100分=1元。', '1元=100分。',
    '人民币100分等于1元。', '1元相当于100分。', '100分折合1元。', '1,000分＝10元。'])
def test_exact_rule_forward_reverse_and_plain_text_forms(rule):
    result = check(rules=[rule])
    assert result['status'] == 'fixed_unit_relation_checked'
    assert result['source_unit'] == '分' and result['output_unit'] == '元'
    assert result['semantic_review_required'] is True


@pytest.mark.parametrize('expression', ['a/100', 'a*100', 'a+0', 'a*1', 'abs(a)', 'b'])
def test_no_double_conversion_or_silent_extra_operation(expression):
    with pytest.raises(FormulaError, match='单个参数|再次'):
        check(expression=expression)


@pytest.mark.parametrize('rule', ['200分=1元。', '1元=200分。', '100分=2元。',
    '100分=1元。200分=1元。', '0分=0元。'])
def test_conflicting_or_zero_relation_rejected_even_if_another_rule_matches(rule):
    with pytest.raises(FormulaError, match='系数不一致'):
        check(rules=[rule])


@pytest.mark.parametrize('rule', ['单位为分，输出单位为元。', '100分不等于1元。',
    '错误示例100分=1元。', '不是100分=1元。', '假设100分=1元。', '100分约等于1元。',
    '100分=1元/100。', '100分=1元+1元。', '100分=1元~2元。', '-100分=1元。',
    '2*100分=1元。', '1+100分=1元。'])
def test_unit_names_denials_approximation_and_partial_formula_do_not_authorize(rule):
    with pytest.raises(FormulaError, match='明确固定换算等式'):
        check(rules=[rule])


@pytest.mark.parametrize('rule', ['积分规则：100分=1元。', '评分100分=1元。',
    '100分=1元，按积分活动适用。'])
def test_score_rule_is_not_cny_fixed_conversion(rule):
    with pytest.raises(FormulaError, match='评分|积分'):
        check(rules=[rule])


def test_fixed_distance_mass_time_and_pressure_relations():
    for question, output, rule in [
        ('2千米等于多少米？', 'm', '1千米=1000米。'),
        ('2吨换算为千克。', 'kg', '1吨等于1000千克。'),
        ('2小时等于多少分钟？', '分钟', '1小时=60分钟。'),
        ('2MPa换算为kPa。', 'kPa', '1MPa=1000kPa。'),
    ]:
        assert check(question=question, output=output, rules=[rule])['semantic_review_required']


def test_foreign_currency_or_dimension_mismatch_not_fixed_conversion():
    with pytest.raises(FormulaError, match='同量纲'):
        check(output='美元', rules=['100分=1美元。'])
    with pytest.raises(FormulaError, match='同量纲'):
        check(output='分钟', rules=['100分=1分钟。'])


def test_source_formula_and_non_conversion_request_are_not_intercepted():
    assert validate_direct_conversion('a/100', [{'name': 'a', 'unit': '分', 'value': '1000'}],
        '元', [], '1000分是多少钱') is None
    q = '计算10元加20元。'
    assert validate_direct_conversion('a+b', [parameter(q)], '元', [], q) is None


def test_more_than_one_parameter_is_not_a_pure_conversion():
    with pytest.raises(FormulaError, match='恰好'):
        check(parameters=[parameter(), {**parameter(), 'name': 'b'}])


def test_unrelated_conversion_clause_does_not_change_other_calculation():
    q = '1000分等于多少元？计算10元加20元。'
    literal = direct_quantities(q)[1]
    p = {'name': 'a', 'unit': literal['unit'], 'value': literal['number'],
         'user_input': {**literal, 'parameter_kind': 'request_quantity'}}
    assert validate_direct_conversion('a+1', [p], '元', [], q) is None
