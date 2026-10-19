"""Bounded calculation obligations; evidence rules and model opinions are not results.

This module identifies requests and validates provenance of already executed K
records. It does not calculate answers, accept model supplied numbers, infer
exchange rates, or turn historical model completeness into verified arithmetic.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import re
from decimal import Decimal, InvalidOperation

from . import completeness

VERSION = 'calculation-completeness-v1'
_UNSIGNED = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?'
_NUMBER = r'[-+]?(?:' + _UNSIGNED + r')'
_MATH = re.compile(r'(?<![A-Za-z0-9_.])(?:\(?\s*[-+]?(?:' + _UNSIGNED + r')\s*\)?)(?:\s*[+＋\-－*×÷/]\s*\(?\s*[-+]?(?:' + _UNSIGNED + r')\s*\)?)+(?![A-Za-z0-9_.])')
_NUMBER_TOKEN = re.compile(r'(?<![\d.])(?:' + _UNSIGNED + r')(?![\d.])')
_SIGNED_TOKEN = re.compile(r'(?<![\d.])' + _NUMBER + r'(?![\d.])')
_NEGATIVE = re.compile(r'(?:不要|不用|无需|无须|不必|不需要|禁止|别)\s*(?:额外|自行|再)?\s*(?:计算|换算|算出|核算|算一下|算一算)|\b(?:do\s+not|don.t)\s+(?:calculate|compute|convert)\b', re.I)
_ARITHMETIC = re.compile(r'计算(?!器)|核算|算出|算一下|算一算|求(?:出)?(?:差值|差额|比值|比例|总和|和|平均值|平均数)|相加|相减|相除|除以|乘以|加起来|合起来|(?:相差|差值|差额|高出|多出|少了|增加|减少)(?:了|是|为)?多少|\b(?:calculate|compute|subtract|divide|multiply)\b', re.I)
_CONVERSION = re.compile(r'换算|转换(?:成|为)|折合|折算|兑换|\bconvert\b', re.I)
_IMPLICIT_CONVERSION = re.compile(r'(?:等于|合|是|为|有)\s*(?:多少|几)|(?:多少|几)\s*(?:元|钱|米|千米|公里|厘米|公斤|千克|吨|分钟|小时|秒|天|万元|亿元|平方米|公顷|亩|美元|欧元|kWh|Wh|kW|W|MPa|kPa|Pa)', re.I)
_MONEY_QUESTION = re.compile(r'(?:多少|几)\s*(?:钱|元)|多少钱')
_SCORE = re.compile(r'积分|学分|评分|得分|分数|满分|百分制|学业|成绩')
_FORMULA_ONLY = re.compile(r'如何计算|怎么计算|计算方法|计算公式|换算规则|换算关系|换算方法')
_MATH_ASK = re.compile(r'计算|算出|求值|等于多少|等于几|是多少|多少钱|多少元|结果|\b(?:calculate|compute|evaluate)\b', re.I)
_MATH_CONTEXT = re.compile(r'编号|电话|日期|期间|年份|年度|页码|区间|范围')
_DATE_OR_YEAR_RANGE = re.compile(r'(?:19|20)\d{2}[-/](?:\d{1,2}[-/]\d{1,2}|\d{1,2}|(?:19|20)\d{2})')
_DERIVED_REQUEST = re.compile(r'(?:这|那|上述|以上|这几|这三|这两|这四|这五|这些|三|两|多个|各|所有|全部|\d+)(?:个)?(?:年|月|季度|数|项|条|笔|记录|数值|指标)[^。；;\n]{0,40}(?:平均|总和|合计|总计|加起来)|(?:从)?(?:19|20)\d{2}年?\s*(?:到|至|与|和|比)\s*(?:19|20)\d{2}年?[^。；;\n]{0,30}(?:增长|增加|降低|减少|相差)[^。；;\n]{0,10}多少')
_ACCOUNTING_BASIS = re.compile(r'(?:按|以|采用)(?:\d{4}年?)?(?:不变价格|不变价|可比价格|可比价|现价|当年价格|可比口径)(?:进行)?(?:计算|核算)')
_RETIRED_INPUT = re.compile(r'(?:撤销|取消|移除|去掉|不再使用|不再沿用|不要(?:继续)?(?:沿用|使用))[^，,。；;\n]{0,100}(?:假设|情景|试算)[^，,。；;\n]*')
_REAL_OPERATION = re.compile(r'相加|相减|相除|除以|乘以|加起来|合起来|求(?:出)?(?:差值|差额|比值|比例|总和|和|平均值|平均数)|平均|总和|(?:减少|降低|增加|提高|上调|下调|增长)\s*(?:了)?\s*\d+(?:\.\d+)?\s*[%％]|(?:再|然后|随后|接着)(?:减|加|减少|增加|降低|提高|下调|上调)(?:去|上|了)?\s*\d')
_NEGATED_ASSUMPTION = re.compile(r'(?:不要|不用|无需|无须|禁止|不需要|别)[^，,。；;\n]{0,20}(?:假设|假定|试算)[^，,。；;\n]*')


def _masked_quotes(text):
    """Hide quotations without moving original offsets or interpreting their text."""
    chars = list(text)
    for opening, closing in [('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』'), ('"', '"'), ("'", "'")]:
        pos = 0
        while (start := text.find(opening, pos)) >= 0:
            end = text.find(closing, start + 1)
            if end < 0:
                break
            chars[start:end + 1] = ' ' * (end + 1 - start)
            pos = end + 1
    return ''.join(chars)


def _request_text(text):
    # A revoked old percentage is context, not a newly authorized operand.
    # Published statistical bases ("按不变价格计算") describe existing facts;
    # they are not an imperative to recompute an otherwise direct metric.
    for pattern in (_ACCOUNTING_BASIS, _RETIRED_INPUT, _NEGATED_ASSUMPTION):
        text = pattern.sub(lambda m: ' ' * len(m.group()), text)
    # Only an explicit premise followed by a clause boundary is a rule here.
    # Its equality operands are not extra quantities the user asked us to
    # convert. Keep offsets and every later occurrence of the requested amount.
    units = _unit_pattern()
    premise = re.compile(r'(?:按|已知|根据)\s*(?:(?:原文|资料|换算规则|换算关系)(?:中)?)?\s*'
                         + _NUMBER + r'\s*' + units + r'\s*(?:=|＝|等于|相当于)\s*'
                         + _NUMBER + r'\s*' + units
                         + r'\s*(?:的?(?:换算)?(?:关系|规则))?\s*[，,；;。]')
    text = premise.sub(lambda m: ' ' * len(m.group()), text)
    return text


def _unit_data():
    # Lazy import avoids the formula_engine -> user input -> requirements cycle.
    from .formula_engine import supported_units
    data = supported_units()
    canonical = {u: u for u in data['units'] if u != '1'}
    canonical.update({a: u for a, u in data['aliases'].items() if a and u != '1'})
    canonical['公里'] = 'km'
    return canonical


def _unit_pattern():
    return '(?:' + '|'.join(re.escape(u) for u in sorted(_unit_data(), key=len, reverse=True)) + ')'


def _quantities(text, offset=0):
    units = _unit_data()
    pattern = re.compile(r'(?<![A-Za-z0-9_.])(?P<number>' + _NUMBER + r')\s*(?P<unit>' + _unit_pattern() + r')')
    out = []
    for match in pattern.finditer(text):
        spelling = match['unit']
        tail = text[match.end():]
        # Never accept a supported prefix of an unsupported or compound unit.
        if (re.search(r'[A-Za-z]$', spelling) and re.match(r'[A-Za-z0-9_]', tail)
                or tail.lstrip().startswith(('/', '*', '·', '^', '²', '³'))
                or spelling == '分' and (tail.startswith(('钟', '数', '值', '制')) or _SCORE.search(text))):
            continue
        if units[spelling] == '%' and re.search(r'[\d)]\s*[+＋\-－*×÷/]\s*$', text[:match.start('number')]):
            # In (1-10%), 10% repeats a formula operand rather than creating a
            # second independently supplied quantity. The authorized scenario
            # supplies its original rate and handles structural 1 separately.
            continue
        start, end = match.start('number'), match.end()
        out.append({'start': offset + start, 'end': offset + end,
                    'quote': text[start:end], 'number': match['number'], 'unit': units[spelling]})
    return out


def _expression_quantities(text, offset=0):
    out = []
    unit_pattern = _unit_pattern()
    # The operator lies outside each operand span. In 8元-12元 the second
    # operand is 12元, so substituting exact operand quotes preserves subtraction.
    atom = r'\(?\s*[-+]?(?:' + _UNSIGNED + r')\s*(?:' + unit_pattern + r')?\s*\)?'
    expression_pattern = re.compile(r'(?<![A-Za-z0-9_.])' + atom
                                    + r'(?:\s*[+＋\-－*×÷/]\s*' + atom + r')+(?![A-Za-z0-9_.])')
    operand_pattern = re.compile(r'(?P<number>' + _UNSIGNED + r')(?P<space>\s*)(?P<unit>' + unit_pattern + r')?')
    units = _unit_data()
    for expression in expression_pattern.finditer(text):
        whole = not (text[:expression.start()] + text[expression.end():]).strip(' \t\r\n。？！!?；;=＝')
        explicit = bool(re.search(r'计算|算出|求值|\b(?:calculate|compute|evaluate)\b', text, re.I))
        if (not whole and not _MATH_ASK.search(text)
                or _MATH_CONTEXT.search(text) and not explicit
                or _DATE_OR_YEAR_RANGE.fullmatch(expression.group().strip()) and not explicit):
            continue
        if text[expression.end():].lstrip().startswith(('%', '％')):
            continue
        if (re.search(r'假设|假定|情景|试算', text) and _REAL_OPERATION.search(text)
                and re.search(r'[%％]', expression.group())):
            continue
        for match in operand_pattern.finditer(expression.group()):
            start = expression.start() + match.start('number')
            end = expression.start() + (match.end('unit') if match['unit'] else match.end('number'))
            unit = units[match['unit']] if match['unit'] else '1'
            if unit == '分' and _SCORE.search(text):
                continue
            out.append({'start': offset + start, 'end': offset + end,
                        'quote': text[start:end], 'number': match['number'], 'unit': unit})
    return out


def _targets(text, inputs):
    units = _unit_data()
    pattern = _unit_pattern()
    # One Q can request several representations. Keep separate obligations so
    # one K in yuan cannot certify an omitted result in ten-thousand yuan.
    group = re.search(r'(?<![乘除])(?:用|以)\s*(' + pattern
                      + r'(?:\s*(?:和|与|、|及|或)\s*' + pattern
                      + r')*)\s*(?:表示|为单位|计(?:价|量)?)', text)
    if group:
        found = [units[m.group()] for m in re.finditer(pattern, group[1])]
        if found:
            return list(dict.fromkeys(found))
    for expression in [r'(?:多少|几)\s*(?P<unit>' + pattern + r')',
                       r'(?:换算|转换|折合|折算|兑换)(?:成|为|到)?\s*(?P<unit>' + pattern + r')',
                       r'(?:用|以)\s*(?P<unit>' + pattern + r')\s*(?:表示|计|为单位)']:
        matches = list(re.finditer(expression, text, re.I))
        if matches:
            candidate = matches[-1]
            tail = text[candidate.end():]
            spelling = candidate['unit']
            if not (re.search(r'[A-Za-z]$', spelling) and re.match(r'[A-Za-z0-9_]', tail)):
                return [units[spelling]]
    # "多少钱" with monetary 分/元 is an ordinary yuan conversion. Foreign
    # currency, scores and an otherwise unknown unit do not establish CNY.
    if _MONEY_QUESTION.search(text) and inputs and all(v['unit'] in {'分', '元', '万元', '亿元'} for v in inputs):
        return ['元']
    return []


def requirements(question):
    """Return bounded obligations with exact original-question quantity spans."""
    if not isinstance(question, str) or len(question) > 12000:
        return []
    masked = _request_text(_masked_quotes(question))
    found, cursor = [], 0
    for part in completeness.question_parts(question):
        start = question.find(part['text'], cursor)
        if start < 0:
            continue
        cursor = start + len(part['text'])
        text = masked[start:cursor]
        if _NEGATIVE.search(text):
            continue
        inputs = _quantities(text, start)
        math_inputs = _expression_quantities(text, start)
        target_inputs = [v for v in inputs + math_inputs if v['unit'] != '1']
        targets = _targets(text, target_inputs)
        representation = bool(targets and re.search(r'(?:用|以).{0,80}(?:表示|为单位)', text))
        inherited = bool(inputs and re.search(r'(?:呢|又是多少)[？?。\s]*$', text)
                         and found and found[-1]['kind'] == 'conversion')
        explicit_conversion = bool(_CONVERSION.search(text) or representation or inherited)
        implicit_conversion = bool(_IMPLICIT_CONVERSION.search(text) and re.search(r'\d', text)
                                   and (inputs or _SCORE.search(text) or re.search(r'等于|折合|兑换', text)))
        active_adjustment = bool(any(v['unit'] == '%' for v in inputs)
                                 and _REAL_OPERATION.search(text) and re.search(r'假设|假定|若|情景|试算', text))
        arithmetic = bool(_ARITHMETIC.search(text) or math_inputs or _DERIVED_REQUEST.search(text) or active_adjustment)
        if _FORMULA_ONLY.search(text) and not inputs and not math_inputs:
            continue
        if not (explicit_conversion or implicit_conversion or arithmetic):
            continue
        conversion = explicit_conversion or implicit_conversion and bool(inputs or _MONEY_QUESTION.search(text))
        if conversion and _REAL_OPERATION.search(text):
            conversion = False
        if (conversion and not inputs and re.search(r'假设|假定|情景|试算', text)
                and _REAL_OPERATION.search(masked)):
            conversion = False
        if inherited:
            conversion = True
            if not targets and found[-1]['output_unit']:
                targets = [found[-1]['output_unit']]
        # Quantity-times-price questions are arithmetic, rather than separate
        # conversions of a monetary price and a physical/count quantity.
        monetary = any(v['unit'] in {'分', '元', '万元', '亿元', '美元', '欧元'} for v in inputs)
        nonmonetary = any(v['unit'] not in {'分', '元', '万元', '亿元', '美元', '欧元'} for v in inputs)
        if monetary and nonmonetary and _MONEY_QUESTION.search(text) and not explicit_conversion:
            conversion = False
            if not targets:
                targets = [next(v['unit'] for v in inputs if v['unit'] in {'元', '美元', '欧元'})] if any(v['unit'] in {'元', '美元', '欧元'} for v in inputs) else []
        # Pure numeric arithmetic should not become conversion just because it
        # ends in "等于多少". Only explicit conversion/quantity requests do so.
        kind = 'conversion' if conversion and not math_inputs else 'arithmetic'
        # Literal-expression spans take precedence over an adjacent signed
        # quantity match; otherwise a binary minus could enter its value and
        # disappear when the caller substitutes the original operand quote.
        spans = [v for v in inputs if not any(v['start'] < m['end'] and m['start'] < v['end'] for m in math_inputs)]
        for value in math_inputs:
            if not any(value['start'] >= v['start'] and value['end'] <= v['end'] for v in spans):
                spans.append(value)
        spans.sort(key=lambda v: (v['start'], v['end']))
        for target in targets or [None]:
            found.append({'question_id': part['id'], 'text': part['text'], 'kind': kind,
                          'input_spans': spans, 'output_unit': target})
    return found


def has_calculation_request(question):
    return bool(requirements(question))


def direct_quantities(question):
    values, seen = [], set()
    for req in requirements(question):
        for value in req['input_spans']:
            key = (value['start'], value['end'], value['unit'])
            if key not in seen:
                values.append(copy.deepcopy(value)); seen.add(key)
    return values


def _records(calculations):
    if isinstance(calculations, dict):
        return {k: v for k, v in calculations.items() if isinstance(k, str) and isinstance(v, dict)}
    if not isinstance(calculations, (list, tuple)):
        return {}
    return {v['id']: v for v in calculations if isinstance(v, dict) and isinstance(v.get('id'), str)}


def _used_parameters(record):
    try:
        tree = ast.parse(str(record.get('expression') or ''), mode='eval')
    except (SyntaxError, ValueError, RecursionError):
        return []
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    parameters = record.get('parameters')
    if not isinstance(parameters, list):
        return []
    return [p for p in parameters if isinstance(p, dict) and p.get('name') in used]


def _ancestors(identifier, calculations):
    pending, seen, out = [identifier], set(), []
    while pending:
        key = pending.pop()
        if key in seen or key not in calculations:
            continue
        seen.add(key); record = calculations[key]; out.append(record)
        for parameter in _used_parameters(record):
            if isinstance(parameter, dict) and isinstance(parameter.get('binding'), dict):
                parent = parameter['binding'].get('calculation_id')
                if isinstance(parent, str):
                    pending.append(parent)
    return out


def _has_input(records, value, question):
    digest = hashlib.sha256(question.encode()).hexdigest()
    for record in records:
        for parameter in _used_parameters(record):
            if not isinstance(parameter, dict):
                continue
            proof = parameter.get('user_input')
            if (isinstance(proof, dict) and proof.get('source_text') == question
                    and proof.get('source_sha256') == digest
                    and all(proof.get(k) == value[k] for k in ('quote', 'start', 'end', 'unit'))
                    and type(proof.get('start')) is int and type(proof.get('end')) is int
                    and question[proof['start']:proof['end']] == proof['quote']):
                return True
    return False


def _published_result(claim, calculation):
    """The final fact must contain the real result, not just link a hidden K."""
    from .formula_engine import parse_unit
    text = str(claim.get('text') or '')
    values = []
    for name in ('value', 'display_value'):
        try:
            number = Decimal(str(calculation.get(name)))
            if number.is_finite():
                values.append(number)
        except InvalidOperation:
            pass
    if not values:
        return False
    try:
        expected = parse_unit(calculation.get('unit'))[0]
    except ValueError:
        return False
    if expected == '1':
        return any(Decimal(m.group().replace(',', '')) in values for m in _SIGNED_TOKEN.finditer(text))
    return any(value['unit'] == expected and Decimal(value['number'].replace(',', '')) in values
               for value in _quantities(text))


def _pure_conversion(record):
    try:
        body = ast.parse(str(record.get('expression') or ''), mode='eval').body
        params = record.get('parameters', [])
        return (isinstance(body, ast.Name) and isinstance(params, list) and len(params) == 1
                and isinstance(params[0], dict) and params[0].get('name') == body.id)
    except (SyntaxError, ValueError, RecursionError):
        return False


def _unbound_literal(text):
    text = _request_text(_masked_quotes(text))
    text = re.sub(r'(?:第\s*\d+\s*(?:页|章|节|条|行|轮)|\d{4}\s*年|图\s*\d+|表\s*\d+|公式\s*[（(]?\d+|[A-Za-z_][A-Za-z0-9_]*\(\d+\))', '', text)
    return bool(re.search(r'\d', text) or _SCORE.search(text))


def assess_coverage(question, claims, calculations, coverage):
    """Demote unsupported answered rows without inventing results or evidence."""
    needed = requirements(question)
    audit = {'version': VERSION, 'status': 'not_required' if not needed else 'passed',
             'requirements': copy.deepcopy(needed), 'issues': []}
    value = copy.deepcopy(coverage) if isinstance(coverage, dict) else completeness.assess(
        completeness.question_parts(question), None, [], [])
    if not needed:
        return value, audit
    by_claim = {c['id']: c for c in claims or [] if isinstance(c, dict) and isinstance(c.get('id'), str)}
    records = _records(calculations)
    items = value.get('items')
    rows = {v.get('id'): v for v in items if isinstance(v, dict)} if isinstance(items, list) else {}
    original_rows = copy.deepcopy(rows)
    for req in needed:
        row = original_rows.get(req['question_id'])
        codes = []
        if not row or row.get('status') != 'answered':
            codes.append('calculation_not_answered')
        else:
            usable = []
            for cid in row.get('claim_ids', []):
                claim = by_claim.get(cid)
                if not claim:
                    continue
                for kid in claim.get('calculation_ids', []):
                    record = records.get(kid)
                    if not record or not _published_result(claim, record):
                        continue
                    if req['output_unit'] and record.get('unit') != req['output_unit']:
                        continue
                    if req['kind'] == 'conversion' and not _pure_conversion(record):
                        continue
                    usable.append((record, _ancestors(kid, records)))
            if not usable:
                codes.append('executed_result_not_published_for_question')
            if req['kind'] == 'conversion' and not req['input_spans'] and _unbound_literal(req['text']):
                codes.append('conversion_input_not_bound')
            for span in req['input_spans']:
                if not any(_has_input(chain, span, question) for _, chain in usable):
                    codes.append('question_input_not_used')
                    break
            # A unitless or foreign-currency "多少钱" has no determined target.
            if req['kind'] == 'conversion' and req['output_unit'] is None:
                codes.append('conversion_target_not_established')
        if codes:
            audit['issues'].append({'question_id': req['question_id'], 'codes': list(dict.fromkeys(codes))})
            if row and row.get('status') == 'answered':
                updated = rows[req['question_id']]
                updated['status'] = 'evidence_available' if row.get('evidence_ids') else 'missing_evidence'
                previous = updated.get('calculation_gate', {}).get('codes', [])
                updated['calculation_gate'] = {'status': 'incomplete', 'codes': list(dict.fromkeys(previous + codes))}
    if audit['issues']:
        audit['status'] = 'incomplete'
        value['complete'] = False
    return value, audit
