"""Finite user-authorized scenarios with original-utterance provenance.

These inputs are hypotheses, never document evidence. No model-provided value
or rewritten question can create one. Business scope still needs separate review.
"""
from __future__ import annotations
import ast
import copy
import hashlib
import re

KINDS = {'percentage_adjust', 'difference'}
CHECKS = ('intent_supported', 'parameter_provenance_supported', 'scope_supported', 'hypothetical_label_supported')
_RATE = re.compile(r'(?P<direction>减少|降低|下调|增加|提高|上调|增长|decrease(?:\s+by)?|increase(?:\s+by)?)\s*(?:了)?\s*(?P<number>[0-9]+(?:\.[0-9]+)?)\s*[%％]', re.I)
_REVOKE = re.compile(r'(?:撤销|取消|去掉|移除|不再使用)[^。；;\n]{0,60}(?:假设|情景|试算)|恢复[^。；;\n]{0,20}实际', re.I)
_DECLINE = re.compile(r'(?:不要|不再|不应|不许|停止|别|do not|stop)\s*(?:再)?(?:沿用|使用|按|继续(?:使用|沿用|按)?|采用|执行|保留|use|using|continue)[^。；;\n]{0,50}(?:假设|情景|试算|assumption|scenario)', re.I)
_ASSUME = re.compile(r'假设|假定|情景|试算|assum(?:e|ing|ption)', re.I)
_GIVEN = re.compile(r'假设|假定|若|设定|给定|取值为|assuming|given|if\b', re.I)
_ASSIGNMENT = re.compile(r'(?<![A-Za-z0-9_])(?P<symbol>[A-Za-z][A-Za-z0-9_]{0,30}(?:\([0-9]{1,4}\))?)\s*[=＝]\s*(?P<number>[-+]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)\s*')
_REFER = re.compile(r'当前|刚才|上轮|上一轮|此前|沿用|保持|仍|current|previous', re.I)
_NEGATE = re.compile(r'(?:不要|禁止|不允许|不能|无需|不用|不做|不采用|不(?=减少|降低|增加|提高)|do not|never)[^。；;\n]{0,20}(?:假设|假定|试算|减少|降低|增加|提高|计算|assum|decrease|increase)', re.I)

INSTRUCTIONS = '''user_assumptions.inputs仅由控制器从真实用户原话识别，不是资料事实。存在输入时，可绑定{user_input_id:"控制器U编号",quote:"原样百分数片段",start:原文字符起点,end:原文字符终点,unit:"%"}，每个字段须复制input；禁止value、补造数字或把问题改写当用户原话。没有输入就不能算情景。原话明确给定变量及单位（如若Is(50)=2 MPa）会提供parameter_kind:quantity和symbol；绑定必须额外复制symbol。quantity只能代入已检索原文rule_evidence_ids支持的公式；公式系数仍绑定原文或已独立核验公式结构，不能把给定值当来源事实。此路径不填percentage_adjust operation。
parameter_kind:request_quantity表示本轮计算问题里直接给出的数量，如“1000分是多少钱”中的1000分。复制该input的user_input_id、quote、start、end、unit，不填symbol或value；它是题目输入，不是资料事实，也不会自动被标为假设。纯换算先检索真实换算依据，再以单变量expression:"a"和目标output_unit执行，不能重复乘除单位系数。数值计算必须先得到真实K，不能只引用换算规则作答。纯显式算式的无单位数字也按原问span绑定，expression用绑定参数代替数字且保持原式AST，不能另造公式；自然语言公式及业务系数仍须原文依据。
对明确“假设减少/增加百分比”，使用operation:{kind:"percentage_adjust",base:"a",rate:"r",direction:"decrease|increase"}；expression只能a*(1-r)或a*(1+r)，r的%已归一化，不再除100，a必须来自已核验资料/真实SQL或本次此前计算。输出单位按白名单自动换算；这个规则来自用户操作，不是原文公式。
用户明确要求差额时可用operation:{kind:"difference",left:"a",right:"b"}和expression:a-b，两参数必须同量纲且有来源。若一方为假设，结果仍为假设比较，不得写成实际同比。
历史假设只有当前用户明确继续或比较该假设且未撤销时才出现在inputs。撤销后须重新查询实际数据，不能沿用旧假设数值。每条假设计算结论必须写明“假设/情景/试算”；参数出处在calculations中展示，不在结论里反复抄输入数值。'''
REVIEW_INSTRUCTIONS = '''对user_scenario_calculation_ids中的每个K，scenario_relations必须恰好返回{id,intent_supported,parameter_provenance_supported,scope_supported,hypothetical_label_supported}四项布尔。核对actual_user_question与原始history，不依赖模型改写：用户确实授权本次百分比调整/差额、直接给定数量换算或给定参数代入原公式；输入数值、单位及符号逐字来自user_question及精确span，源公式和系数另有原页支持（纯显式算式可按原问AST），须逐式核对实际expression的每个运算、分母和结构常量；text规则不等于已完成AST对照，只有visual_rule_structure_checked表示已核验图像公式树匹配；基数对象、时期、实际/假设性质及单位正确；撤销条件没有继续执行；is_hypothetical为true的结果须明确标成假设，request_quantity且is_hypothetical=false的普通题目输入应直接说明是用户给定量，不强加假设标签。不得把题目输入冒充资料实测。分需分清人民币分、积分、评分和时间；固定单位换算不能代替业务兑换率或汇率，未知应false。纯实际差额也要正确标明两方性质。计算机械通过不证明语义，任一未知填false。此数组不可由总体supported或coverage替代。'''


def _quoted(text, start, end):
    # Quoting source text is not authorizing its parameters as a new scenario.
    for opening, closing in [('“','”'),('‘','’'),('「','」'),('『','』'),('"','"'),("'","'")]:
        before=text[:start]
        inside=(before.count(opening)%2==1) if opening==closing else (before.rfind(opening)>before.rfind(closing))
        if inside and text.find(closing,end)>=end: return True
    return False


def _clause(text, match):
    start = max(text.rfind(c, 0, match.start()) for c in '。；;\n？！?!') + 1
    ends = [i for c in '。；;\n？！?!' if (i := text.find(c, match.end())) >= 0]
    return text[start:min(ends, default=len(text))]


def _quantity_matches(original):
    # Import lazily: formula_engine calls this module only after its whitelist
    # has been initialized. A unit is copied from raw user text, not guessed.
    from .formula_engine import supported_units, parse_unit
    capabilities = supported_units()
    spellings = sorted({u for u in [*capabilities['units'], *capabilities['aliases']]
                        if u and u != '1'}, key=len, reverse=True)
    for match in _ASSIGNMENT.finditer(original):
        if _quoted(original,match.start(),match.end()): continue
        clause = _clause(original, match)
        clause_start = max(original.rfind(c, 0, match.start()) for c in '。；;\n？！?!') + 1
        if not _GIVEN.search(original[clause_start:match.start()]) or _NEGATE.search(clause) or _REVOKE.search(clause) or _DECLINE.search(clause): continue
        rest = original[match.end():]
        spelling = next((u for u in spellings if rest.startswith(u)
            and (not re.search(r'[A-Za-z]$', u) or not re.match(r'[A-Za-z0-9_]', rest[len(u):]))), None)
        if spelling is None: continue
        end = match.end() + len(spelling)
        # Do not silently accept the prefix of a compound/unsupported unit.
        if original[end:].lstrip().startswith(('/', '*', '·', '^', '²', '³', '每')): continue
        unit, _ = parse_unit(spelling)
        yield match.start('number'), end, unit, match['symbol'], match['number']


def build_context(question, history=()):
    """Only immutable original questions may contribute a still-active rate."""
    if not isinstance(question, str) or len(question) > 12000:
        raise ValueError('真实用户问题格式无效')
    active = []
    turns = []
    for turn in history or []:
        if (isinstance(turn, dict) and turn.get('kind') != 'structured_context'
                and isinstance(turn.get('original_question'), str)
                and isinstance(turn.get('answer_id'), str)):
            turns.append((turn['answer_id'], turn['original_question']))
    turns.append(('current', question))
    for answer_id, original in turns:
        if len(original) > 12000: continue
        if _REVOKE.search(original) or _DECLINE.search(original): active = []
        found = []
        for match in _RATE.finditer(original):
            if _quoted(original,match.start(),match.end()): continue
            clause = _clause(original, match)
            if not _ASSUME.search(clause) or _NEGATE.search(clause) or _REVOKE.search(clause) or _DECLINE.search(clause): continue
            nstart, nend = match.start('number'), match.end()
            quote = original[nstart:nend]
            direction = 'decrease' if match['direction'].lower().startswith(('减少','降低','下调','decrease')) else 'increase'
            identifier = 'U' + hashlib.sha256((answer_id+'\n'+original+'\n'+str(nstart)).encode()).hexdigest()[:12]
            found.append({'id':identifier, 'origin':'user_question', 'source_answer_id':answer_id,
                'source_text':original, 'source_sha256':hashlib.sha256(original.encode()).hexdigest(),
                'quote':quote, 'start':nstart, 'end':nend, 'unit':'%', 'direction':direction, 'parameter_kind':'percentage',
                'is_hypothetical':True, 'is_document_evidence':False})
        for nstart, nend, unit, symbol, number in _quantity_matches(original):
            identifier = 'U' + hashlib.sha256((answer_id+'\n'+original+'\n'+str(nstart)).encode()).hexdigest()[:12]
            found.append({'id':identifier, 'origin':'user_question', 'source_answer_id':answer_id,
                'source_text':original, 'source_sha256':hashlib.sha256(original.encode()).hexdigest(),
                'quote':original[nstart:nend], 'start':nstart, 'end':nend, 'unit':unit,
                'symbol':symbol, 'raw_number':number, 'parameter_kind':'quantity',
                'is_hypothetical':True, 'is_document_evidence':False})
        # Only this turn's literal calculation operands are eligible. Historical
        # request quantities are never silently carried into a new question.
        if answer_id == 'current':
            from .calculation_requirements import direct_quantities
            for quantity in direct_quantities(original):
                nstart, nend = quantity['start'], quantity['end']
                if any(p['start'] == nstart and p['end'] == nend for p in found):
                    continue
                identifier = 'U' + hashlib.sha256((answer_id+'\n'+original+'\n'+str(nstart)).encode()).hexdigest()[:12]
                found.append({'id':identifier, 'origin':'user_question', 'source_answer_id':answer_id,
                    'source_text':original, 'source_sha256':hashlib.sha256(original.encode()).hexdigest(),
                    'quote':quantity['quote'], 'start':nstart, 'end':nend, 'unit':quantity['unit'],
                    'raw_number':quantity['number'], 'parameter_kind':'request_quantity',
                    'is_hypothetical':bool(_ASSUME.search(_clause(original, re.compile(re.escape(quantity['quote'])).search(original, nstart)))),
                    'is_document_evidence':False})
        if found: active = found[:8]
    if active and active[0]['source_answer_id'] != 'current' and not (_ASSUME.search(question) and _REFER.search(question)):
        active = []
    return {'schema_version':'user-scenario-1', 'actual_user_question':question,
            'inputs':active, 'boundary':'用户问题输入，不是原件观测；假设按is_hypothetical区分，撤销后不可再用'}


def resolve_input(binding, context):
    if not isinstance(binding, dict) or set(binding) - {'user_input_id','quote','start','end','unit','symbol'} or not {'user_input_id','quote','start','end','unit'} <= set(binding):
        raise ValueError('用户参数只接受user_input_id、quote、start、end、unit及原symbol，不接受value或资料引用混用')
    inputs = (context or {}).get('inputs', [])
    item = next((v for v in inputs if v.get('id') == binding['user_input_id']), None)
    if item is None: raise ValueError('用户假设不存在、未明确授权或已经撤销')
    if any(binding.get(k) != item.get(k) for k in ('quote','start','end','unit')):
        raise ValueError('用户假设原文、位置或单位不一致')
    if type(binding['start']) is not int or type(binding['end']) is not int:
        raise ValueError('用户假设位置必须为整数')
    original = item['source_text']
    if (original[item['start']:item['end']] != item['quote']
            or hashlib.sha256(original.encode()).hexdigest() != item['source_sha256']):
        raise ValueError('用户原话版本或数值片段已变化')
    if item.get('parameter_kind') == 'quantity':
        if binding.get('symbol') != item['symbol']: raise ValueError('用户给定参数的原始符号不匹配')
        actual = list(_quantity_matches(original))
        if (item['start'], item['end'], item['unit'], item['symbol'], item['raw_number']) not in actual:
            raise ValueError('用户给定参数不是原话中的完整变量、数值和单位')
        return item['raw_number'], copy.deepcopy(item)
    if item.get('parameter_kind') == 'request_quantity':
        from .calculation_requirements import direct_quantities
        if 'symbol' in binding:
            raise ValueError('直接题目数量没有公式符号')
        actual = direct_quantities(original)
        if not any(all(q.get(key) == item.get(key) for key in ('start','end','quote','unit'))
                   and q['number'] == item.get('raw_number') for q in actual):
            raise ValueError('计算输入不是原始问题中的完整数量')
        return item['raw_number'], copy.deepcopy(item)
    if 'symbol' in binding: raise ValueError('百分比调整输入没有公式符号')
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*[%％]', item['quote'])
    if not match or binding['unit'] != '%': raise ValueError('用户假设目前仅支持原话明确的百分比调整')
    return match[1], copy.deepcopy(item)


def literal_arithmetic_authorized(expression, bindings, context):
    """Compare the actual numeric expression after exact operand substitution.

    This authorizes no natural-language business formula, guessed coefficient,
    unit conversion or historical input. The bounded calculator still validates
    the AST, dimensions, precision and every parameter independently.
    """
    original = (context or {}).get('actual_user_question')
    if not isinstance(original, str) or not isinstance(bindings, dict) or not bindings:
        return False
    spans = []
    try:
        for name, binding in bindings.items():
            _, proof = resolve_input(binding, context)
            if proof.get('parameter_kind') != 'request_quantity':
                return False
            spans.append((proof['start'], proof['end'], name))
        start, end = min(s[0] for s in spans), max(s[1] for s in spans)
        allowed = '0123456789eE.,+-−*/×÷^() \t'
        while start and original[start-1] in allowed:
            start -= 1
        while end < len(original) and original[end] in allowed:
            end += 1
        raw = original[start:end].strip()
        if not any(c in raw for c in '+-−*/×÷^'):
            return False
        literal = original[start:end]
        for first, last, name in sorted(spans, reverse=True):
            literal = literal[:first-start] + name + literal[last-start:]
        literal = literal.strip().replace('×', '*').replace('÷', '/').replace('−', '-').replace('^', '**')
        return ast.dump(ast.parse(literal, mode='eval'), include_attributes=False) == ast.dump(ast.parse(expression, mode='eval'), include_attributes=False)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False


def authorize(operation, question, tree, bindings, resolved, output_dimensions, context):
    if not isinstance(operation, dict): raise ValueError('用户情景操作须为对象')
    kind = operation.get('kind')
    if kind == 'percentage_adjust':
        if set(operation) != {'kind','base','rate','direction'}: raise ValueError('百分比调整操作字段无效')
        base, rate = operation['base'], operation['rate']
        if base == rate or {base,rate} != set(bindings): raise ValueError('百分比调整须恰好包含基数和比例两个参数')
        if base not in resolved or rate not in resolved: raise ValueError('情景参数未绑定')
        proof = resolved[rate][2].get('user_input')
        if not proof or proof.get('parameter_kind') != 'percentage' or proof.get('direction') != operation['direction']: raise ValueError('比例方向必须匹配用户原话中的增加或减少')
        if resolved[base][2].get('user_input') or not resolved[base][2].get('evidence_ids'):
            raise ValueError('情景基数必须来自实际资料或已有可追溯计算')
        if resolved[rate][1] or tuple(resolved[base][1]) != tuple(output_dimensions): raise ValueError('百分比或基数输出量纲不匹配')
        value = resolved[rate][0]
        if value < 0 or operation['direction'] == 'decrease' and value > 1: raise ValueError('减少比例必须在0%至100%之间')
        operator = '-' if operation['direction'] == 'decrease' else '+'
        expected = [f'{base}*(1{operator}{rate})', f'(1{operator}{rate})*{base}']
        hypothetical = True
    elif kind == 'difference':
        if set(operation) != {'kind','left','right'}: raise ValueError('差额操作字段无效')
        if not re.search(r'相差|差额|差值|差多少|多多少|少多少|difference',question,re.I) or _NEGATE.search(question):
            raise ValueError('用户没有明确授权本次差额计算')
        left,right = operation['left'],operation['right']
        if left == right or {left,right} != set(bindings) or any(k not in resolved for k in (left,right)):
            raise ValueError('差额须恰好包含两个有来源的参数')
        if any(not resolved[k][2].get('evidence_ids') or resolved[k][2].get('user_input') for k in (left,right)):
            raise ValueError('差额两方必须是资料或可追溯计算，不能直接塞入用户数值')
        if any(tuple(resolved[k][1]) != tuple(output_dimensions) for k in (left,right)):
            raise ValueError('差额两方量纲必须相同')
        expected = [f'{left}-{right}']
        hypothetical = any(resolved[k][2].get('is_hypothetical') for k in (left,right))
    else: raise ValueError('不支持的用户情景操作')
    if ast.dump(tree, include_attributes=False) not in [ast.dump(ast.parse(e,mode='eval'),include_attributes=False) for e in expected]:
        raise ValueError('情景表达式必须与受限操作完全一致，不能附加常数、比例或其他运算')
    return {'kind':kind, 'authorization_origin':'actual_user_question', 'is_hypothetical':bool(hypothetical),
            'source_formula_claimed':False, 'independent_review_required':list(CHECKS),
            'mechanical_status':'original_input_spans_operation_shape_and_dimensions_checked'}


def required_calculations(claims, calculations):
    required, seen = set(), set()
    pending = [k for c in claims for k in c.get('calculation_ids', [])]
    while pending:
        cid = pending.pop()
        if cid in seen or cid not in calculations: continue
        seen.add(cid); item=calculations[cid]
        if item.get('rule_origin') == 'user_scenario' or any(p.get('user_input') for p in item.get('parameters',[])): required.add(cid)
        pending.extend(p.get('binding',{}).get('calculation_id') for p in item.get('parameters',[]))
    return sorted(required)


def review_passed(review, identifiers):
    if not identifiers: return True
    rows=review.get('scenario_relations')
    return (isinstance(rows,list) and len(rows)==len(identifiers)
        and all(isinstance(r,dict) and isinstance(r.get('id'),str) for r in rows)
        and {r['id'] for r in rows}==set(identifiers)
        and all(all(r.get(k) is True for k in CHECKS) for r in rows))


def check_label(text, calculations):
    if any(c.get('is_hypothetical') for c in calculations) and not re.search(r'假设|情景|试算|给定条件|给定参数|假定|hypothetical|scenario|given conditions|given parameters',text,re.I):
        raise ValueError('用户假设计算必须在该条结论中明确标为假设、情景或试算，不能作为实际观测发布')
