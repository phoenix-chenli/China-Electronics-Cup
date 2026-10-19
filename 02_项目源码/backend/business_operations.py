"""Finite user-authorized cost operations, never fabricated source formulas.

Mechanical authorization only. Each used operation requires independent
intent/roles/scope/pricing review before any associated claim is published.
"""
from __future__ import annotations
import ast
import re

KIND = 'quantity_times_unit_price'
CHECKS = ('intent_supported', 'parameter_roles_supported', 'scope_supported', 'pricing_supported')
INSTRUCTIONS = '''若用户明确要求计算费用/电费/价款，但资料未写公式，可使用有限operation:{kind:"quantity_times_unit_price",pairs:[{quantity:"q",unit_price:"p"}]}，表达式只能q*p或多个这样的乘积相加。它表达用户计算意图，不是来源规则；不能用来推导阶梯价、固定附加费、税率或优惠。参数仍须真实格/原句绑定；SQL单位可用同一结果行的真实unit_column（不可SELECT常量伪造）。先用WHERE真实对象/时期筛选；未筛选多行禁止任意选第0行。没有授权或业务计价不明确应澄清，不得借普通说明段落伪装公式。'''
REVIEW_INSTRUCTIONS = '''对user_operation_calculation_ids中的每个K，必须在operation_relations中恰好返回{id,intent_supported,parameter_roles_supported,scope_supported,pricing_supported}四项布尔。逐项判断：真实用户确实要求这种计算；quantity/price确实是用量与对应单价而非功率/总价/比例；对象、时期、修订和逐对对应一致且范围完整；量纲、税口径、计价规则与原文一致，不缺阶梯价、固定费、附加费或折扣等必要条件。它是用户操作授权，不是原文公式；机械乘法正确不代表业务适用。任一未知或不支持填false。该数组不能由claims总体支持替代。'''

def question_authorizes_cost(question):
    if not isinstance(question, str): return False
    clauses = re.split(r'[。！？!?；;\n]', question)
    return any(re.search(r'(?:计算|算出|求出|求|核算|算一下|算一算)', c)
               and re.search(r'(?:电费|费用|成本|价款|货款|金额|总价)', c)
               and not re.search(r'(?:不要|不用|禁止|无需|不能|无法).{0,5}(?:计算|算|求|核算)', c)
               for c in clauses)

def authorize(operation, question, tree, bindings, resolved, output_dimensions):
    if not isinstance(operation, dict) or set(operation) != {'kind', 'pairs'} or operation.get('kind') != KIND:
        raise ValueError('业务操作只支持显式 quantity_times_unit_price 与 pairs')
    if not question_authorizes_cost(question):
        raise ValueError('真实用户问题没有明确授权费用计算；需要用户明确规则或原文规则')
    pairs = operation['pairs']
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 8:
        raise ValueError('业务操作必须包含1到8个真实用量/单价对')
    wanted=[]; names=[]
    for pair in pairs:
        if not isinstance(pair,dict) or set(pair) != {'quantity','unit_price'}:
            raise ValueError('每对业务操作须明确quantity与unit_price参数名')
        q,p=pair['quantity'],pair['unit_price']
        if not isinstance(q,str) or not isinstance(p,str) or q == p or q not in resolved or p not in resolved:
            raise ValueError('业务操作参数不存在或角色重叠')
        # Each named parameter is used exactly once, including repeated prices:
        # repeated rows have separate explicit bindings and reviewable scopes.
        names.extend([q,p]); wanted.append(tuple(sorted((q,p))))
        qdims=dict(resolved[q][1]); pdims=dict(resolved[p][1]); out=dict(output_dimensions)
        if len(out)!=1 or next(iter(out)) not in ('CNY','USD','EUR') or next(iter(out.values()))!=1:
            raise ValueError('费用操作输出必须是明确的货币单位')
        currency=next(iter(out))
        if not qdims or any(k in qdims for k in ('CNY','USD','EUR')) or pdims.get(currency)!=1:
            raise ValueError('quantity须有物理/计数单位，unit_price须为该数量的货币单价')
        combined=dict(qdims)
        for k,v in pdims.items(): combined[k]=combined.get(k,0)+v
        if {k:v for k,v in combined.items() if v} != out:
            raise ValueError('用量和单价的单位不相消为输出货币')
    if len(names)!=len(set(names)) or set(names)!=set(bindings):
        raise ValueError('所有参数须唯一分配到一个用量/单价对，不得夹带未说明参数')
    terms=[]
    def visit(node):
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add): visit(node.left);visit(node.right)
        elif isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult) and isinstance(node.left,ast.Name) and isinstance(node.right,ast.Name):
            terms.append(tuple(sorted((node.left.id,node.right.id))))
        else: raise ValueError('费用操作只允许逐对用量乘单价后求和，不允许常数、折扣、除法或其他公式')
    visit(tree.body)
    if sorted(terms)!=sorted(wanted): raise ValueError('表达式与类型化用量/单价对不一致')
    return {'kind':KIND,'pairs':pairs,'authorization_origin':'actual_user_question',
            'source_formula_claimed':False,'independent_review_required':list(CHECKS),
            'mechanical_status':'operation_shape_and_dimensions_checked'}

def required_calculations(claims, calculations):
    required=set(); pending=[cid for c in claims for cid in c.get('calculation_ids',[])]
    seen=set()
    while pending:
        cid=pending.pop()
        if cid in seen or cid not in calculations:continue
        seen.add(cid); item=calculations[cid]
        if item.get('rule_origin')=='user_operation': required.add(cid)
        pending.extend(p.get('binding',{}).get('calculation_id') for p in item.get('parameters',[]))
    return sorted(required)

def review_passed(review, ids):
    if not ids:return True
    rows=review.get('operation_relations')
    return (isinstance(rows,list) and len(rows)==len(ids) and all(isinstance(r,dict) for r in rows)
            and all(isinstance(r.get('id'),str) for r in rows) and {r.get('id') for r in rows}==set(ids)
            and all(all(r.get(k) is True for k in CHECKS) for r in rows))
