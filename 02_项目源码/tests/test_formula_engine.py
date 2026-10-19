"""Offline arithmetic/provenance tests. All sources here are synthetic."""
from decimal import Decimal
import pytest
from backend.formula_engine import calculate_formula, FormulaError, parse_unit, supported_units


def text(identifier, quote):
    return {'id': identifier, 'kind': 'text', 'quote': quote}


def calculate(expression='amount*rate', **kwargs):
    evidence = {'E1': text('E1', '金额12万元；比例5%。按金额乘比例计算。')}
    bindings = {'amount': {'evidence_id': 'E1', 'quote': '12万元', 'unit': '万元'},
                'rate': {'evidence_id': 'E1', 'quote': '5%', 'unit': '%'}}
    return calculate_formula(expression, kwargs.pop('bindings', bindings), kwargs.pop('evidence', evidence),
                             kwargs.pop('output_unit', '元'), rule_evidence_ids=['E1'], **kwargs)


def test_decimal_percent_and_currency_conversion():
    result = calculate()
    assert result['value'] == '6000.00'
    assert result['display_value'] == '6000.00' and result['unit'] == '元'
    assert result['parameters'][0]['binding']['start'] == 2
    assert result['evidence_ids'] == ['E1']


def test_energy_intensity_and_area_conversion():
    evidence = {'E1': text('E1', '消耗2MWh，面积200平方米，单位面积能耗为能耗除以面积。')}
    result = calculate_formula('energy/area', {'energy': {'evidence_id': 'E1', 'quote': '2MWh', 'unit': 'MWh'},
                                              'area': {'evidence_id': 'E1', 'quote': '200平方米', 'unit': 'm²'}},
                               evidence, 'kWh/m²', rule_evidence_ids=['E1'])
    assert result['display_value'] == '10.00'
    assert parse_unit('平方千米')[1].factor == 1000000


def test_growth_percentage_and_negative_value():
    evidence = {'E1': text('E1', '本期80元，基期100元，增长率=(本期-基期)/基期。')}
    result = calculate_formula('(a-b)/b', {'a': {'evidence_id': 'E1', 'quote': '80元', 'unit': '元'},
                                         'b': {'evidence_id': 'E1', 'quote': '100元', 'unit': '元'}}, evidence,
                               '%', rule_evidence_ids=['E1'])
    assert result['display_value'] == '-20.00'


def test_sqrt_and_power_dimensions():
    evidence = {'E1': text('E1', '面积9平方米，边长取面积平方根。')}
    result = calculate_formula('sqrt(a)', {'a': {'evidence_id': 'E1', 'quote': '9平方米', 'unit': 'm²'}}, evidence, 'm', rule_evidence_ids=['E1'])
    assert result['display_value'] == '3.00'
    result = calculate_formula('a**2', {'a': {'calculation_id': 'K1', 'unit': 'm'}}, evidence, 'm²', rule_evidence_ids=['E1'], prior_calculations={'K1': result})
    assert result['display_value'] == '9.00'


@pytest.mark.parametrize('expression', ['__import__("os")', 'amount.__class__', '[amount][0]', '(x for x in [amount])', 'amount if rate else 0', 'amount//rate', 'amount*99999999', 'open("x")'])
def test_disallowed_ast(expression):
    with pytest.raises(FormulaError):
        calculate(expression)


@pytest.mark.parametrize('unit', ['香蕉币', 'USD-CNY', '万元月', '月', '年', 'm³', 'kWh/未知'])
def test_unknown_units_fail_closed(unit):
    with pytest.raises(FormulaError):
        parse_unit(unit)


def test_unit_mismatch_and_zero_denominator():
    with pytest.raises(FormulaError, match='量纲'):
        calculate('amount+rate')
    with pytest.raises(FormulaError, match='量纲'):
        calculate(output_unit='m²')
    with pytest.raises(FormulaError, match='分母为零'):
        calculate('amount/(rate-rate)')


def test_no_model_value_no_null_no_unsupported_unit_proof():
    with pytest.raises(FormulaError, match='填写的数值'):
        calculate(bindings={'amount': {'value': 100, 'evidence_id': 'E1', 'quote': '12万元', 'unit': '万元'}})
    evidence = {'E1': text('E1', '按金额乘比例。'), 'E2': {'kind': 'table', 'query_result': {'columns': ['amount'], 'rows': [[None]]}}}
    with pytest.raises(FormulaError, match='空值'):
        calculate('a', evidence=evidence, bindings={'a': {'evidence_id': 'E2', 'row': 0, 'column': 'amount', 'unit': '元'}})
    evidence['E2']['query_result']['rows'] = [['10']]
    with pytest.raises(FormulaError, match='单位'):
        calculate('a', evidence=evidence, bindings={'a': {'evidence_id': 'E2', 'row': 0, 'column': 'amount', 'unit': '元'}})


def test_sql_cell_binds_real_output_and_original_header_unit():
    evidence = {'E1': text('E1', '原金额直接返回。'), 'E2': {'kind': 'table', 'query_result': {'columns': ['amount_yuan'], 'rows': [['12.50']]}, 'original_columns': ['amount_yuan']}}
    result = calculate('a', evidence=evidence, bindings={'a': {'evidence_id': 'E2', 'row': 0, 'column': 'amount_yuan', 'unit': '元'}})
    assert result['display_value'] == '12.50'
    evidence['E2']['original_columns'] = ['other']
    with pytest.raises(FormulaError, match='原始字段'):
        calculate('a', evidence=evidence, bindings={'a': {'evidence_id': 'E2', 'row': 0, 'column': 'amount_yuan', 'unit': '元'}})


def test_explicit_unit_evidence_and_forged_quote():
    evidence = {'E1': text('E1', '数据库金额单位为万元。'), 'E2': {'kind': 'table', 'query_result': {'columns': ['amount'], 'rows': [['1.23']]}}}
    result = calculate('a', evidence=evidence, bindings={'a': {'evidence_id': 'E2', 'row': 0, 'column': 'amount', 'unit': '万元', 'unit_evidence_id': 'E1'}})
    assert result['display_value'] == '12300.00'
    with pytest.raises(FormulaError, match='精确'):
        calculate(bindings={'a': {'evidence_id': 'E1', 'quote': '50%', 'unit': '%'}})


def test_unit_cannot_relabel_visible_percent_or_wanyuan():
    with pytest.raises(FormulaError, match='单位'):
        calculate(bindings={'a': {'evidence_id': 'E1', 'quote': '5%', 'unit': '元', 'unit_evidence_id': 'E1'}})
    with pytest.raises(FormulaError, match='单位'):
        calculate(bindings={'a': {'evidence_id': 'E1', 'quote': '12万元', 'unit': '元'}})
    with pytest.raises(FormulaError, match='无量纲'):
        calculate(bindings={'a': {'evidence_id': 'E1', 'quote': '12', 'unit': '1'}})


def test_fragment_cannot_cut_out_digit_and_duplicate_needs_offset():
    with pytest.raises(FormulaError, match='完整'):
        calculate(bindings={'a': {'evidence_id': 'E1', 'quote': '2万元', 'unit': '万元'}})
    with pytest.raises(FormulaError, match='多次'):
        calculate('a', evidence={'E1': text('E1', '金额5元，金额5元。')}, bindings={'a': {'evidence_id': 'E1', 'quote': '5元', 'unit': '元'}})


def test_rule_required_and_literal_user_formula_allowed():
    evidence = {'E1': text('E1', '金额3元。')}
    binding = {'a': {'evidence_id': 'E1', 'quote': '3元', 'unit': '元'}}
    with pytest.raises(FormulaError, match='规则'):
        calculate_formula('a', binding, evidence, '元')
    assert calculate_formula('a*2', binding, evidence, '元', user_formula='按a*2计算')['display_value'] == '6.00'


def test_cancellation_and_resource_limits():
    with pytest.raises(InterruptedError):
        calculate(cancelled=lambda: True)
    with pytest.raises(FormulaError):
        calculate('amount' + '+rate'*100)
    assert '元' in supported_units()['units']


def test_english_unit_tokens_are_not_found_inside_unrelated_words():
    evidence = {'E1': text('E1', 'business rules'), 'E2': {'kind': 'table', 'query_result': {'columns': ['duration'], 'rows': [['5']]}}}
    with pytest.raises(FormulaError, match='单位'):
        calculate_formula('a', {'a': {'evidence_id': 'E2', 'row': 0, 'column': 'duration', 'unit': '秒', 'unit_evidence_id': 'E1'}}, evidence, '秒', rule_evidence_ids=['E1'])


def test_zero_power_zero_and_invalid_rule_shapes_are_rejected():
    with pytest.raises(FormulaError):
        calculate('(rate-rate)**0')
    with pytest.raises(FormulaError, match='格式'):
        calculate_formula('x', {'x': {'evidence_id': 'E1', 'quote': '1', 'unit': '1'}}, {'E1': text('E1', '1')}, rule_evidence_ids=[{}])


def test_chinese_bulletin_number_and_yiyuan_conversion():
    evidence = {'E1': text('E1', '总量1312.9亿元。按亿元换算为万元。')}
    result = calculate_formula('a', {'a': {'evidence_id': 'E1', 'quote': '1312.9亿元', 'unit': '亿元'}}, evidence, '万元', rule_evidence_ids=['E1'])
    assert result['display_value'] == '13129000.00'


def test_variable_substring_in_question_is_not_a_user_formula():
    evidence = {'E1': text('E1', '数值5。')}
    with pytest.raises(FormulaError, match='规则'):
        calculate_formula('a', {'a': {'evidence_id': 'E1', 'quote': '5', 'unit': '1'}}, evidence, user_formula='What does this data show?')


def test_mu_conversion_constant_keeps_forty_significant_digits():
    from decimal import localcontext
    _, unit = parse_unit('亩')
    with localcontext() as context:
        context.prec = 40
        assert unit.factor == Decimal(2000) / Decimal(3)
    assert len(unit.factor.as_tuple().digits) == 40


def test_sum_alias_preserves_verified_original_column_unit():
    from backend.formula_engine import infer_sql_output_units
    source={'tables':[{'name':'sales','columns':[{'name':'sales_yuan'},{'name':'cost_yuan'},{'name':'rate_percent'}]}]}
    lineage=infer_sql_output_units(source,'SELECT SUM(sales_yuan) AS total_sales FROM sales',['total_sales'])
    assert lineage['total_sales']=={'unit':'元','source_columns':['sales.sales_yuan'],'method':'supported_sql_expression'}
    evidence={'E1':text('E1','按金额返回。'), 'E2':{'kind':'table','column_units':lineage,'query_result':{'columns':['total_sales'],'rows':[['12.5']]}}}
    result=calculate_formula('a',{'a':{'evidence_id':'E2','row':0,'column':'total_sales','unit':'元'}},evidence,'元',rule_evidence_ids=['E1'])
    assert result['display_value']=='12.50'
    with pytest.raises(FormulaError,match='不能改标'):
        calculate_formula('a',{'a':{'evidence_id':'E2','row':0,'column':'total_sales','unit':'万元'}},evidence,'元',rule_evidence_ids=['E1'])
    assert infer_sql_output_units(source,'SELECT SUM(sales_yuan*rate_percent) AS total FROM sales',['total'])=={}
    assert infer_sql_output_units(source,'SELECT 100 AS sales_yuan FROM sales',['sales_yuan'])=={}
    assert infer_sql_output_units(source,'SELECT DECIMAL_SUM(sales_yuan) AS total FROM sales',['total'])['total']['unit']=='元'


def test_percent_word_unit_evidence_and_candidate_row_guard():
    evidence={'E1':text('E1','金额用元，比例用百分数。'), 'E2':{'kind':'table','query_result':{'columns':['rate'],'rows':[[2]]}}}
    result=calculate_formula('r',{'r':{'evidence_id':'E2','row':0,'column':'rate','unit':'%','unit_evidence_id':'E1'}},evidence,'%',rule_evidence_ids=['E1'])
    assert result['display_value']=='2.00'
    evidence['E2']['query_result']['selection_required_for_scalar']=True
    with pytest.raises(FormulaError,match='未筛选'):
        calculate_formula('r',{'r':{'evidence_id':'E2','row':0,'column':'rate','unit':'%','unit_evidence_id':'E1'}},evidence,'%',rule_evidence_ids=['E1'])


def test_sql_exact_whole_cell_quote_normalizes_to_real_position():
    evidence={'E1':text('E1','金额直接返回。'),'E2':{'kind':'table','query_result':{'columns':['指标','amount_yuan'],'rows':[['甲','30.7'],['乙','61.1']]},'original_columns':['amount_yuan']}}
    result=calculate('a',evidence=evidence,bindings={'a':{'evidence_id':'E2','quote':'30.7','unit':'元'}})
    binding=result['parameters'][0]['binding']
    assert binding['row']==0 and binding['column']=='amount_yuan'
    assert binding['resolved_from']=='unique_exact_cell_quote' and result['display_value']=='30.70'
    for quote in ['0.7','30.70',' 30.7','甲']:
        with pytest.raises(FormulaError):
            calculate('a',evidence=evidence,bindings={'a':{'evidence_id':'E2','quote':quote,'unit':'元'}})
    evidence['E2']['query_result']['rows'].append(['丙','30.7'])
    with pytest.raises(FormulaError,match='匹配 2 格'):
        calculate('a',evidence=evidence,bindings={'a':{'evidence_id':'E2','quote':'30.7','unit':'元'}})


def test_sql_bad_position_has_specific_evidence_and_shape_feedback():
    evidence={'E1':text('E1','直接返回。'),'E2':{'kind':'table','query_result':{'columns':['amount_yuan'],'rows':[['12']]},'original_columns':['amount_yuan']}}
    with pytest.raises(FormulaError,match='证据 E2 是SQL查询结果.*amount_yuan.*1 行'):
        calculate('a',evidence=evidence,bindings={'a':{'evidence_id':'E2','row':9,'column':'missing','unit':'元'}})


def test_power_conversion_preserves_exact_finite_energy_rate():
    from backend.formula_engine import evaluate_arithmetic
    values={'a':{'value':'16','unit':'kWh'},'b':{'value':'8','unit':'kWh'},'c':{'value':'4','unit':'h'}}
    for unit,expected in [('kW','6'),('W','6000'),('MW','0.006')]:
        result=evaluate_arithmetic('(a+b)/c',values,unit,precision=3)
        assert Decimal(result['value'])==Decimal(expected)
    assert Decimal(evaluate_arithmetic('p*t',{'p':{'value':'6','unit':'kW'},'t':{'value':'4','unit':'h'}},'kWh')['value'])==Decimal('24')
