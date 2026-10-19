"""Offline intent and mechanical completeness gates; no provider requests."""
import copy
import hashlib

import pytest

from backend import calculation_requirements as cr
from backend import completeness


@pytest.mark.parametrize('question,kind,unit,numbers', [
    ('1000分是多少钱', 'conversion', '元', ['1000']),
    ('2500分等于多少元？', 'conversion', '元', ['2500']),
    ('2.5公里是多少米？', 'conversion', 'm', ['2.5']),
    ('1000分和2500分分别是多少元？', 'conversion', '元', ['1000', '2500']),
    ('将1,200.50元换算成万元。', 'conversion', '万元', ['1,200.50']),
    ('1000分钟是多少小时？', 'conversion', '小时', ['1000']),
    ('请计算两年的差值。', 'arithmetic', None, []),
    ('请计算平均值并说明依据。', 'arithmetic', None, []),
    ('1000÷100是多少？', 'arithmetic', None, ['1000', '100']),
    ('12+8等于多少？', 'arithmetic', None, ['12', '8']),
    ('这三年的平均收入是多少？', 'arithmetic', None, []),
    ('2023年到2024年收入增长了多少？', 'arithmetic', None, []),
])
def test_bounded_intent_and_original_spans(question, kind, unit, numbers):
    req = cr.requirements(question)[0]
    assert req['kind'] == kind and req['output_unit'] == unit
    assert [v['number'] for v in req['input_spans']] == numbers
    assert all(question[v['start']:v['end']] == v['quote'] for v in req['input_spans'])
    assert cr.has_calculation_request(question)


@pytest.mark.parametrize('question', [
    '2024年同比增长率是多少？', '2024年收入多少万元？', '该公司有哪些业务？',
    '不要计算，只引用原文金额。', '不用换算1000分是多少钱，只引用原文。',
    '请解释“1000分是多少钱”这句话。', '标题叫"12+8等于多少"，请引用标题。',
    '请说明计算公式与依据。', '这台计算器是什么型号？',
    '2024-2025年收入是多少？', '日期2024-01-01的原文是什么？',
    '请引用资料编号12+8。', '电话号码010-12345678是什么？',
    '资料中2024/01/01的记录有哪些？', '2024-01-01',
    '按不变价格计算，增加值增长多少？',
    '给出按2020年不变价格计算的同比方向及幅度。',
])
def test_plain_facts_quotations_and_negative_requests_remain_light(question):
    assert cr.requirements(question) == []
    assert cr.direct_quantities(question) == []


@pytest.mark.parametrize('question', ['1000积分是多少钱？', '评分1000分是多少钱？', '1000学分等于多少元？'])
def test_score_units_are_never_monetary_cents(question):
    assert cr.has_calculation_request(question)
    assert cr.direct_quantities(question) == []


def test_conjoined_and_repeated_clauses_keep_absolute_original_offsets():
    q = '1000分是多少元？2.5公里是多少米？1000分是多少元？'
    reqs = cr.requirements(q)
    assert [v['question_id'] for v in reqs] == ['Q1', 'Q2', 'Q3']
    assert [v['input_spans'][0]['start'] for v in reqs] == [0, q.index('2.5'), q.rindex('1000')]
    assert len(cr.direct_quantities(q)) == 3


def context(question):
    coverage = completeness.assess(completeness.question_parts(question), [
        {'question_id': p['id'], 'status': 'answered', 'claim_ids': ['C1'], 'evidence_ids': ['E1']}
        for p in completeness.question_parts(question)], ['C1'], ['E1'])
    return coverage


def proof(question, index=0):
    value = cr.direct_quantities(question)[index]
    return {**value, 'source_text': question,
            'source_sha256': hashlib.sha256(question.encode()).hexdigest(), 'origin': 'user_question'}


def calculation(question, index=0, value='10', unit='元', expression='x'):
    return {'id': 'K1', 'expression': expression, 'value': value, 'display_value': value + '.00', 'unit': unit,
            'parameters': [{'name': 'x', 'user_input': proof(question, index), 'binding': {}}]}


def test_rule_only_is_incomplete_even_if_semantic_review_claimed_full_coverage():
    q = '1000分是多少钱'
    coverage = context(q); before = copy.deepcopy(coverage)
    new, audit = cr.assess_coverage(q, [{'id': 'C1', 'text': '100分=1元。', 'evidence_ids': ['E1']}], [], coverage)
    assert audit['status'] == 'incomplete' and new['complete'] is False
    assert new['items'][0]['status'] == 'evidence_available'
    assert coverage == before


def test_published_result_original_input_and_target_unit_are_required():
    q = '1000分是多少钱'
    k = calculation(q)
    claim = {'id': 'C1', 'text': '换算结果是10元。', 'calculation_ids': ['K1']}
    new, audit = cr.assess_coverage(q, [claim], [k], context(q))
    assert audit['status'] == 'passed' and new['complete'] is True
    for wrong in [
        {**claim, 'text': '100分=1元。'},
        {**claim, 'text': '换算结果是10。'},
        {**claim, 'text': '换算结果是100元。'},
        {**claim, 'calculation_ids': ['K999']},
    ]:
        assert cr.assess_coverage(q, [wrong], [k], context(q))[1]['status'] == 'incomplete'


@pytest.mark.parametrize('change', [
    {'source_text': 'normalized rather than original'}, {'source_sha256': 'forged'},
    {'quote': '100分'}, {'start': False}, {'end': 999}, {'unit': '元'},
])
def test_forged_or_rewritten_quantity_proofs_cannot_complete(change):
    q = '1000分是多少钱'; k = calculation(q)
    k['parameters'][0]['user_input'].update(change)
    claim = {'id': 'C1', 'text': '结果10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_transitive_inputs_are_accepted_but_unused_or_wrong_target_k_is_not():
    q = '1000分是多少钱'; parent = calculation(q, value='1000', unit='分')
    child = {'id': 'K2', 'expression': 'x', 'value': '10', 'display_value': '10.00', 'unit': '元',
             'parameters': [{'name': 'x', 'binding': {'calculation_id': 'K1'}}]}
    claim = {'id': 'C1', 'text': '结果10人民币元。', 'calculation_ids': ['K2']}
    assert cr.assess_coverage(q, [claim], [parent, child], context(q))[1]['status'] == 'passed'
    child['unit'] = '美元'; claim['text'] = '结果10美元。'
    assert cr.assess_coverage(q, [claim], [parent, child], context(q))[1]['status'] == 'incomplete'


def test_separate_conversions_need_each_input_not_one_unrelated_or_summed_result():
    q = '1000分和2500分分别是多少元？'
    one = calculation(q); two = calculation(q, 1, value='25'); two['id'] = 'K2'
    claim = {'id': 'C1', 'text': '分别为10元和25元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [one], context(q))[1]['status'] == 'incomplete'
    claim['calculation_ids'].append('K2')
    assert cr.assess_coverage(q, [claim], [one, two], context(q))[1]['status'] == 'passed'
    summed = {**one, 'expression': 'x+y', 'value': '35', 'display_value': '35.00',
              'parameters': one['parameters'] + two['parameters']}
    assert cr.assess_coverage(q, [{**claim, 'text': '35元。', 'calculation_ids': ['K1']}], [summed], context(q))[1]['status'] == 'incomplete'


def test_arithmetic_requires_both_raw_operator_inputs_and_visible_result():
    q = '12+8等于多少？'
    k = calculation(q, value='20', unit='1', expression='a+b')
    k['parameters'][0]['name'] = 'a'
    k['parameters'].append({'name': 'b', 'user_input': proof(q, 1), 'binding': {}})
    claim = {'id': 'C1', 'text': '计算结果为20。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'passed'
    k['parameters'].pop()
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_unknown_conversion_cannot_complete_via_an_unrelated_k():
    q = '1000积分是多少钱？'
    k = {'id': 'K1', 'expression': 'x', 'unit': '元', 'value': '10', 'parameters': []}
    claim = {'id': 'C1', 'text': '10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_existing_unknown_or_conflicted_coverage_is_never_upgraded():
    q = '1000分是多少钱'; k = calculation(q)
    claim = {'id': 'C1', 'text': '10元。', 'calculation_ids': ['K1']}
    for coverage in [None, {'status': 'unknown', 'complete': None, 'items': []},
                     {'status': 'conflict', 'complete': False, 'items': []}]:
        new, audit = cr.assess_coverage(q, [claim], [k], coverage)
        assert new['complete'] is False and audit['status'] == 'incomplete'


def test_non_calculation_gate_does_not_reclassify_existing_projection():
    coverage = {'status': 'not_recorded', 'complete': None, 'items': []}
    new, audit = cr.assess_coverage('列出业务名称。', [], [], coverage)
    assert new == coverage and audit['status'] == 'not_required'


@pytest.mark.parametrize('question', ['12+8', '(12+8)', '请计算2024-2025的结果。'])
def test_pure_or_explicit_math_is_not_rejected_as_an_identifier(question):
    assert cr.has_calculation_request(question)


def test_negative_unitless_results_keep_their_sign():
    q = '8-12等于多少？'; k = calculation(q, value='-4', unit='1', expression='a-b')
    k['parameters'][0]['name'] = 'a'
    k['parameters'].append({'name': 'b', 'user_input': proof(q, 1), 'binding': {}})
    claim = {'id': 'C1', 'text': '计算结果是-4。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'passed'
    claim['text'] = '计算结果是4。'
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_unused_bindings_do_not_satisfy_a_second_requested_conversion():
    q = '1000分和2500分分别是多少元？'; k = calculation(q)
    k['parameters'].append({'name': 'unused', 'user_input': proof(q, 1), 'binding': {}})
    claim = {'id': 'C1', 'text': '计算结果10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_unused_arithmetic_parameters_and_prior_calculations_do_not_satisfy_inputs():
    q = '12+8等于多少？'; k = calculation(q, value='12', unit='1', expression='a')
    k['parameters'][0]['name'] = 'a'
    k['parameters'].append({'name': 'unused', 'user_input': proof(q, 1), 'binding': {}})
    claim = {'id': 'C1', 'text': '计算结果12。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'
    prior = calculation(q, 1, value='8', unit='1'); prior['id'] = 'K2'
    k['parameters'][1] = {'name': 'unused', 'binding': {'calculation_id': 'K2'}}
    assert cr.assess_coverage(q, [claim], [k, prior], context(q))[1]['status'] == 'incomplete'


def test_multiple_target_units_each_need_a_published_k():
    q = '将1000分分别用元和万元表示。'
    reqs = cr.requirements(q)
    assert [r['output_unit'] for r in reqs] == ['元', '万元']
    assert [r['question_id'] for r in reqs] == ['Q1', 'Q1']
    assert len(cr.direct_quantities(q)) == 1
    one = calculation(q); two = calculation(q, value='0.001', unit='万元'); two['id'] = 'K2'; two['display_value'] = '0.001'
    claim = {'id': 'C1', 'text': '结果10元，相当于0.001万元。', 'calculation_ids': ['K1', 'K2']}
    assert cr.assess_coverage(q, [claim], [one, two], context(q))[1]['status'] == 'passed'
    claim['calculation_ids'].pop()
    assert cr.assess_coverage(q, [claim], [one, two], context(q))[1]['status'] == 'incomplete'


def test_same_sentence_followup_quantity_is_not_silently_omitted():
    q = '1000分是多少钱，2500分呢？'
    assert [v['number'] for v in cr.direct_quantities(q)] == ['1000', '2500']
    k = calculation(q)
    claim = {'id': 'C1', 'text': '结果10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_conversion_with_undetermined_target_is_not_accepted_as_any_k():
    q = '请换算1000分。'; k = calculation(q)
    claim = {'id': 'C1', 'text': '结果10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'


def test_quantity_times_price_request_is_arithmetic_not_individual_conversions():
    q = '单价2元，买5件是多少钱？'
    req = cr.requirements(q)[0]
    assert req['kind'] == 'arithmetic' and req['output_unit'] == '元'
    assert [v['unit'] for v in req['input_spans']] == ['元', '件']


def test_revoked_percentage_is_not_recreated_as_a_new_request_operand():
    q = '撤销刚才减少10%的假设，恢复2024年的实际登记金额，分别用分和元表示，说明已经撤销什么、其他哪些口径仍然保留。'
    assert cr.direct_quantities(q) == []
    assert all(not r['input_spans'] for r in cr.requirements(q))


@pytest.mark.parametrize('question', [
    '将登记金额从原始单位换算为元。',
    '请按表达式a将2024年甲收入换算为元。',
])
def test_source_quantity_conversion_does_not_require_an_invented_user_quantity(question):
    assert cr.direct_quantities(question) == []
    k = {'id': 'K1', 'expression': 'a', 'unit': '元', 'value': '560', 'display_value': '560.00',
         'parameters': [{'name': 'a', 'value': '56000', 'unit': '分', 'evidence_ids': ['E1'],
                         'binding': {'evidence_id': 'E1'}}]}
    claim = {'id': 'C1', 'text': '登记金额折合560元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(question, [claim], [k], context(question))[1]['status'] == 'passed'
    assert cr.assess_coverage(question, [{**claim, 'text': '100分=1元。'}], [k], context(question))[1]['status'] == 'incomplete'


def test_percentage_adjustment_then_multiple_presentations_is_arithmetic():
    q = '仅假设登记金额减少10%，计算假设金额，分别用分和元表示。'
    reqs = cr.requirements(q)
    assert reqs and all(r['kind'] == 'arithmetic' for r in reqs)
    assert [v['quote'] for v in cr.direct_quantities(q)] == ['10%']


def test_percentage_formula_and_accounting_year_are_not_extra_operands_or_targets():
    q = '仍针对2024年，仅假设刚才的登记金额减少10%，按原金额乘以(1-10%)计算假设金额，分别用分和元表示。明确这不是实际登记结果，不修改原始数据。'
    reqs = cr.requirements(q)
    assert [r['output_unit'] for r in reqs] == ['分', '元']
    assert all(r['kind'] == 'arithmetic' for r in reqs)
    assert [v['quote'] for v in cr.direct_quantities(q)] == ['10%']


@pytest.mark.parametrize('question', ['将1000英里换算成米。', '将1000换算为元。'])
def test_unknown_bare_literal_is_not_excused_as_a_source_quantity(question):
    k = {'id': 'K1', 'expression': 'a', 'unit': '元', 'value': '10', 'parameters': [{'name': 'a'}]}
    claim = {'id': 'C1', 'text': '10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(question, [claim], [k], context(question))[1]['status'] == 'incomplete'


@pytest.mark.parametrize('prefix', ['按', '已知', '根据原文'])
def test_rule_premise_is_not_an_extra_conversion_operand(prefix):
    q = prefix + '100分=1元，1000分是多少钱'
    reqs = cr.requirements(q)
    assert reqs[0]['kind'] == 'conversion' and reqs[0]['output_unit'] == '元'
    assert [v['quote'] for v in cr.direct_quantities(q)] == ['1000分']
    span = cr.direct_quantities(q)[0]
    assert span['start'] == q.index('1000') and q[span['start']:span['end']] == '1000分'


def test_premise_with_the_same_requested_amount_keeps_the_actual_question_occurrence():
    q = '按1000分=10元的换算关系，1000分是多少钱？'
    values = cr.direct_quantities(q)
    assert len(values) == 1 and values[0]['start'] == q.rindex('1000')
    assert values[0]['quote'] == '1000分'


@pytest.mark.parametrize('question,expected', [
    ('按1000分换算成元。', ['1000分']),
    ('已知1000分是多少钱？', ['1000分']),
    ('按1000分=10元计算结果。', ['1000分', '10元']),
])
def test_premise_mask_never_deletes_an_actual_unsplit_question_amount(question, expected):
    assert [v['quote'] for v in cr.direct_quantities(question)] == expected


@pytest.mark.parametrize('operation', ['再减', '再减少', '再增加', '然后减去', '随后降低'])
def test_conversion_followed_by_another_operation_is_arithmetic(operation):
    q = '把1000分换算成元' + operation + '10%是多少元？'
    req = cr.requirements(q)[0]
    assert req['kind'] == 'arithmetic' and req['output_unit'] == '元'
    assert [v['quote'] for v in cr.direct_quantities(q)] == ['1000分', '10%']


@pytest.mark.parametrize('question,quotes,units,target', [
    ('120元+80元一共多少钱', ['120元', '80元'], ['元', '元'], '元'),
    ('1000分+500分等于多少元', ['1000分', '500分'], ['分', '分'], '元'),
    ('1000分/100是多少钱', ['1000分', '100'], ['分', '1'], '元'),
    ('2米+30厘米是多少米', ['2米', '30厘米'], ['m', 'cm'], 'm'),
    ('8元-12元等于多少元？', ['8元', '12元'], ['元', '元'], '元'),
])
def test_explicit_arithmetic_with_units_keeps_every_operand_and_operator(question, quotes, units, target):
    req = cr.requirements(question)[0]
    assert req['kind'] == 'arithmetic' and req['output_unit'] == target
    values = cr.direct_quantities(question)
    assert [v['quote'] for v in values] == quotes
    assert [v['unit'] for v in values] == units
    assert all(question[v['start']:v['end']] == v['quote'] for v in values)
    # Exact-span replacement leaves the user's operator available to AST check.
    replaced = question
    for v in reversed(values):
        replaced = replaced[:v['start']] + 'x' + replaced[v['end']:]
    assert any(op in replaced for op in '+-/')


def test_rule_premise_coefficients_do_not_enter_a_typed_expression_request():
    q = '按100分=1元，1000分/100是多少钱？'
    assert [v['quote'] for v in cr.direct_quantities(q)] == ['1000分', '100']
    assert [v['unit'] for v in cr.direct_quantities(q)] == ['分', '1']
    assert cr.requirements(q)[0]['kind'] == 'arithmetic'


def test_typed_expression_gate_requires_the_unitless_divisor_too():
    q = '1000分/100是多少钱？'
    k = calculation(q, value='0.1', unit='元', expression='a/b'); k['display_value'] = '0.10'
    k['parameters'][0]['name'] = 'a'
    claim = {'id': 'C1', 'text': '计算结果为0.10元。', 'calculation_ids': ['K1']}
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'incomplete'
    k['parameters'].append({'name': 'b', 'user_input': proof(q, 1), 'binding': {}})
    assert cr.assess_coverage(q, [claim], [k], context(q))[1]['status'] == 'passed'
