"""Finite original-rule checks for literal quantities in conversion requests.

This does not establish a source's applicability. It prevents an already
normalized input being converted twice, or a score/exchange rule being mistaken
for the calculator's fixed units. Other source-formula calculations are left to
their existing formula and semantic review gates.
"""
from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation, localcontext
import re


_NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
_SCORE = re.compile(r'积分|学分|评分|得分|分数|满分|百分制|成绩|分值')
_NEGATED_RULE = re.compile(r'不是|并非|不等于|不相当于|错误|错例|反例|不成立|假设|假定')
_EXTENDED_UNIT = re.compile(r'[A-Za-z0-9_/*·^²³+＋\-－×÷~～—−]')


def _same_request(parameter, request):
    proof = parameter.get('user_input') or {}
    return any(all(proof.get(key) == span.get(key) for key in ('start', 'end', 'quote', 'unit'))
               for span in request.get('input_spans', []))


def validate_direct_conversion(expression, parameters, output_unit, rule_quotes, question):
    """Validate a direct-request conversion, or return None when out of scope.

    ``parameters`` are resolved calculator audit records, never raw model
    values. Rules must contain a positive exact numeric equality between the
    actual source and target units. Accepted assertions remain subject to the
    normal independent semantic review before publication.
    """
    from .calculation_requirements import requirements
    from .formula_engine import FormulaError, parse_unit, supported_units

    direct = [p for p in parameters if isinstance(p, dict)
              and (p.get('user_input') or {}).get('parameter_kind') == 'request_quantity']
    if not direct:
        return None
    requests = [r for r in requirements(question) if r.get('kind') == 'conversion'
                and any(_same_request(p, r) for p in direct)]
    if not requests:
        return None
    if len(parameters) != 1 or len(direct) != 1:
        raise FormulaError('纯单位换算须恰好绑定本次问题中的一个原始数量，不能混入其他参数或金额')
    try:
        tree = ast.parse(expression, mode='eval')
    except (TypeError, ValueError, SyntaxError, RecursionError) as exc:
        raise FormulaError('纯单位换算表达式必须是单个已绑定参数名') from exc
    parameter = direct[0]
    if not isinstance(tree.body, ast.Name) or tree.body.id != parameter.get('name'):
        raise FormulaError('纯单位换算只允许单个参数表达式；参数已按原单位归一化，不能再次乘除换算系数')
    source_name, source_unit = parse_unit(parameter.get('unit'))
    target_name, target_unit = parse_unit(output_unit)
    if source_unit.dimensions != target_unit.dimensions:
        raise FormulaError('纯单位换算的输入与输出必须同量纲，不能把评分、积分或不同币种当作固定单位换算')
    currency = source_unit.dimensions == (('CNY', 1),)
    if currency and _SCORE.search(question):
        raise FormulaError('问题中的评分或积分不是人民币分，不能套用人民币固定换算系数')
    if not isinstance(rule_quotes, (list, tuple)) or any(not isinstance(q, str) for q in rule_quotes):
        raise FormulaError('纯单位换算须提供真实已读取的原文规则列表')
    units = supported_units()
    spellings = {u for u in units['units'] if u != '1'}
    spellings.update(a for a, canonical in units['aliases'].items() if a and canonical != '1')
    unit_pattern = '(?:' + '|'.join(re.escape(u) for u in sorted(spellings, key=len, reverse=True)) + ')'
    relation = re.compile(r'(?<![\d.A-Za-z_+＋\-－*/×÷.])(?P<a>' + _NUMBER + r')\s*(?P<au>' + unit_pattern
                          + r')\s*(?:=|＝|等于|相当于|折合)\s*(?P<b>' + _NUMBER
                          + r')\s*(?P<bu>' + unit_pattern + r')')
    matched = []
    for quote in rule_quotes:
        # Keep qualifiers in the same sentence; a comma must not hide that the
        # apparent equality is an example, a score rule, or an explicit denial.
        for clause in re.split(r'[。！？!?；;\n]', quote):
            for match in relation.finditer(clause):
                if _NEGATED_RULE.search(clause):
                    continue
                tail = clause[match.end():].lstrip()
                if (tail and (_EXTENDED_UNIT.match(tail) or tail.startswith(('钟', '数', '制', '值')))):
                    continue
                left_name, left = parse_unit(match['au'])
                right_name, right = parse_unit(match['bu'])
                if {left_name, right_name} != {source_name, target_name}:
                    continue
                if currency and _SCORE.search(clause):
                    raise FormulaError('所引原文是评分或积分兑换关系，不是人民币分与元的固定单位关系')
                if left.dimensions != right.dimensions or left.dimensions != source_unit.dimensions:
                    raise FormulaError('所引换算等式的单位量纲与本次输入输出不一致')
                if len(match['a']) > 100 or len(match['b']) > 100:
                    raise FormulaError('换算规则数值超出可核验范围')
                try:
                    a, b = Decimal(match['a'].replace(',', '')), Decimal(match['b'].replace(',', ''))
                    with localcontext() as context:
                        # Each parsed side has at most 100 characters and each
                        # built-in factor at most 40 significant digits.
                        context.prec = 160
                        consistent = a > 0 and b > 0 and a * left.factor == b * right.factor
                except InvalidOperation as exc:
                    raise FormulaError('换算规则必须含可核验的正数等式') from exc
                if not consistent:
                    raise FormulaError('所引原文换算关系与内置固定单位系数不一致；不能替换、猜测或沿用该系数')
                matched.append({'quote': match.group(), 'source_unit': source_name, 'output_unit': target_name})
    if not matched:
        raise FormulaError('未找到原文中与输入、输出单位一致的明确固定换算等式；仅出现单位词不能授权计算，需补充规则或澄清')
    return {'status': 'fixed_unit_relation_checked', 'expression': expression,
            'source_unit': source_name, 'output_unit': target_name,
            'relations': matched, 'semantic_review_required': True}
