"""Explicit SQL output-cell selections rendered as unverified review candidates.

No source row, numerical value, unit, business scope or verdict is inferred.
Original query/claim and independent semantic/coverage gates remain mandatory.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .query_result_context import build_query_result_context, check_query_bindings

VERSION = "explicit-query-fact-narrative-v3"
PERIOD_BINDING_GUIDANCE = '输出别名（如sum_YYYY）本身不能证明统计期。多期查询应返回从实际日期字段或其日期表达式取得的年份/时期实体列，与金额或计数在同一结果行，并在entity_cells显式选择该真实实体格；或按每个完整日期范围分别查询，再绑定各次实际查询的结果。不得只修改别名、编造时期格或删除用户要求的年份来通过检查。已有结果足够时复用，不重复发送相同SQL；确实缺少必需的时期实体时，允许更改查询补取。'
INSTRUCTIONS = '''实际SQL输出事实优先每格一条query_fact候选。以下两种单位选择互斥，所有ID均须从当前query_result_context复制。context_id优先填该上下文明示的短context_alias，完整context_id也兼容；未知标识不自动纠错。
单位格示例：{"query_fact":{"evidence_id":"E1","context_id":"当前上下文ID","cell_id":"目标格ID","entity_cells":["同一行对象格ID","同一行完整时期格ID"],"unit_cell_id":"同一行真实单位格ID"}}
列映射示例：{"query_fact":{"evidence_id":"E1","context_id":"当前上下文ID","cell_id":"目标格ID","entity_cells":[],"unit_from_column":true}}
unit_from_column仅在该目标输出列已有column_units.unit和非空source_columns时可用，必须布尔true；无单位依据时两种字段均省略。query_fact内不能自填unit/text/statement/value/行列号。控制器复制真实列名、完整值和显式选择的实体/单位生成待核验候选，不替你选择对象或推测范围。多行结果的entity_cells必须唯一定位该行，保留完整对象/时期/必要口径。
外层严格协议：query_fact可附text、statement、evidence_ids和空calculation_ids。非空text是待核验正文，绝不忽略；单格事实未填text时才机械生成正文。statement若省略使用整条text；复杂叙述必须显式选择正文内唯一完整事实子句，不能把两格数值都塞进一个单格statement。显式evidence_ids须包含本条所选结果格的evidence_id；可另引真实支持关系说明的普通段落。calculation_ids仅兼容空数组，实际计算另列claim。
若查询事实正文还写了文档名称、版本、单位换算规则或文档定义的业务口径，本条evidence_ids必须同时显式列出查询证据和支持这些说明的文档证据；query_fact只补查询证据，不继承其他claim的引用。不要依赖另一条“口径说明”来补足本条引用，也不要为省引用删掉问题必需的口径。
多格比较/范围/同异判断用：{"text":"完整自然语言答案，包含明确关系结论和各格的完整对象、时期、原值、单位","query_facts":[{"selection":{"evidence_id":"E1","context_id":"当前上下文ID","cell_id":"目标格ID","entity_cells":[],"unit_from_column":true},"statement":"从本条text逐字复制该格的完整事实子句"}],"evidence_ids":["E1"]}。每个selection与query_fact同schema；每个statement仍须通过原格值、同一行实体、完整日期和真实单位绑定。多格最多8项，不能用实体/数字词袋代替子句；不要用另一条claim的引用证明本条对象映射。关系结论必须在text中明确写出且有本条真实引用支持，不能只列数值让读者自己推断是否同对象、能否替代、口径差异等必答项。
同一数字在正文不同位置出现须分别绑定；已有某格绑定不授权摘要或关系结论再次无绑定地写该数字。优先让开头给出清楚的关系结论，在逐格子句中写一次完整数值、对象、时期和单位，避免重复数字造成无依据的附加解释。
query_fact与query_facts互斥，不混旧query_bindings/table_bindings或计算；复杂原表仍用table_bindings。数字差额、比例和费用须先执行calculate，另列已执行K的结论。控制器不根据关系词推断源、值、单位或事实是否成立。比较、对象映射、适用范围与完整性仍须独立query_relations/claims/coverage复核。query_fact不能证明不存在、全量排名或总体范围；阴性缺口仍用原阴性协议。
''' + PERIOD_BINDING_GUIDANCE


class QueryFactError(ValueError):
    def __init__(self, message, claim_index=1):
        super().__init__(message)
        self.claim_index = claim_index
ALLOWED = {"evidence_id", "context_id", "cell_id", "entity_cells", "unit_cell_id", "unit_from_column"}


def render_query_fact(selection, evidence_records, *, max_chars=1800):
    """Generate ONE unverified candidate from ONE explicit actual output cell.

    No body, value, row, column, inferred entity, or invented unit is accepted.
    All selections must already identify current projected controller evidence.
    Semantic scope/SQL correctness/coverage are deliberately NOT decided here.
    """
    if not isinstance(selection, dict) or set(selection) - ALLOWED:
        raise ValueError("query_fact仅接受显式结果格选择字段，不接受text/value/statement或其他字段")
    if not all(isinstance(selection.get(k), str) and selection[k]
               for k in ("evidence_id", "context_id", "cell_id")):
        raise ValueError("缺少evidence_id/context_id/cell_id")
    entities = selection.get("entity_cells")
    if (not isinstance(entities, list) or len(entities) > 16
            or not all(isinstance(x, str) and x for x in entities)
            or len(set(entities)) != len(entities)):
        raise ValueError("entity_cells必须显式给出唯一且有限的格ID列表")
    matches = [e for e in evidence_records if isinstance(e, dict)
               and e.get("id") == selection["evidence_id"]]
    if len(matches) != 1:
        raise ValueError("evidence_id未唯一指向已读取依据")
    evidence = matches[0]
    table_context = evidence.get("table_context") or {}
    if (evidence.get("query_result") is None
            or any(table_context.get(k) for k in ('has_table', 'tables', 'unresolved_structure', 'location_unknown'))):
        raise ValueError("仅普通SQL查询输出可使用query_fact，不能替代原文复杂表关系")
    context = build_query_result_context(evidence)
    if not context.get("usable"):
        raise ValueError("上下文不可用或来源/查询/结果版本不匹配")
    stored = evidence.get("query_result_context")
    if stored is not None:
        if (not isinstance(stored, dict) or stored.get("usable") is not True
                or stored.get("context_id") != context["context_id"]):
            raise ValueError("本次投影不可用或已变化")
        allowed = {c.get("cell_id") for r in stored.get("rows", [])
                   for c in r.get("cells", [])}
    else:
        allowed = {c["cell_id"] for r in context["rows"] for c in r["cells"]}
    supplied_context = selection['context_id']
    alias = context['context_alias']
    if supplied_context != context['context_id']:
        # A short reference is valid only if actually advertised for this exact
        # evidence/version. Never truncate or edit a received long identifier.
        if (supplied_context != alias or not isinstance(stored, dict)
                or stored.get('context_alias') != alias):
            raise ValueError('上下文标识未精确匹配；请复制当前证据的context_alias或完整context_id，不自动纠错')
        alias_targets = {e['query_result_context'].get('context_id') for e in evidence_records
            if isinstance(e, dict) and isinstance(e.get('query_result_context'), dict)
            and e['query_result_context'].get('context_alias') == alias}
        if alias_targets != {context['context_id']}:
            raise ValueError('短上下文标识存在不同完整目标，须使用完整context_id')
    lookup = {c["cell_id"]: (r["row_index"], c) for r in context["rows"]
              for c in r["cells"] if c["cell_id"] in allowed}
    entry = lookup.get(selection["cell_id"])
    if entry is None:
        raise ValueError("目标格不在已提供的投影中")
    row, cell = entry
    if cell["value"] is None:
        raise ValueError("NULL不能转录为已知事实")
    entity_rows = []
    for eid in entities:
        hit = lookup.get(eid)
        if hit is None or hit[0] != row or eid == cell["cell_id"] or hit[1]["value"] in (None, ""):
            raise ValueError("实体必须是同一行的其他真实非空格")
        entity_rows.append(hit[1])
    binding = {"evidence_id": evidence["id"], "context_id": context["context_id"],
               "cell_id": cell["cell_id"], "row_index": row,
               "column_index": cell["column_index"], "column_label": cell["column_label"],
               "value": copy.deepcopy(cell["value"]), "entity_cells": list(entities)}
    unit = ""
    if "unit_cell_id" in selection and "unit_from_column" in selection:
        raise ValueError("unit_cell_id与unit_from_column必须互斥")
    if "unit_from_column" in selection:
        if selection['unit_from_column'] is not True:
            raise ValueError("unit_from_column必须是布尔true，不能由字符串或数字代替")
        mapped = (evidence.get('column_units') or {}).get(cell['column_label'])
        columns = mapped.get('source_columns') if isinstance(mapped, dict) else None
        unit = mapped.get('unit') if isinstance(mapped, dict) else None
        if (not isinstance(unit, str) or not unit or len(unit) > 40
                or not isinstance(columns, list) or not columns or len(columns) > 32
                or not all(isinstance(v, str) and v for v in columns)):
            raise ValueError("目标输出列没有已溯源column_units.unit/source_columns，不能补单位")
        binding['unit'] = unit
    if "unit_cell_id" in selection:
        unit_id = selection["unit_cell_id"]
        if not isinstance(unit_id, str) or not unit_id:
            raise ValueError("unit_cell_id必须是真实格ID")
        hit = lookup.get(unit_id)
        if hit is None or hit[0] != row or unit_id == cell["cell_id"]:
            raise ValueError("单位必须来自同一结果行的独立真实格")
        unit = hit[1]["value"]
        if not isinstance(unit, str) or not unit or len(unit) > 40:
            raise ValueError("单位格值无效")
        binding.update(unit=unit, unit_column=hit[1]["column_label"])

    def display(value):
        if value is True: return "true"
        if value is False: return "false"
        return str(value)

    def literal(value):
        # Preserve the literal substring required by the existing validator,
        # while visibly delimiting source data (including quotes/newlines and
        # instruction-like strings). Never escape/rewrite its business value.
        value = display(value)
        for left, right in [('「', '」'), ('『', '』'), ('«', '»'), ('‹', '›'), ('【', '】')]:
            if left not in value and right not in value:
                return left + value + right
        raise ValueError('原字段含所有可用数据界定符；不能无界定拼接，需其他精确原文候选')

    # Original physical output labels and full source values. No lexical alias,
    # selected object inference, period normalization, or automatic source join.
    prefix = "、".join(f'{literal(e["column_label"])}为{literal(e["value"])}' for e in entity_rows)
    text = (prefix + "时，" if prefix else "") + f'{literal(cell["column_label"])}为{literal(cell["value"])}'
    if unit:
        text += '，单位为' + literal(unit)
    text += '。'
    if type(max_chars) is not int or max_chars < 1 or len(text) > min(max_chars, 1800):
        raise ValueError("完整规范候选超预算，不能截掉对象/时期/数值后继续")
    binding["statement"] = text
    check = check_query_bindings(text, [binding], [evidence])
    if check["valid"] is not True:
        raise ValueError("原查询门禁拒绝规范候选：" + "；".join(check["errors"]))
    claim = {"text": text, "evidence_ids": [evidence["id"]], "query_bindings": [binding]}
    audit = {"version": VERSION, "selection": copy.deepcopy(selection),
             "resolved_context_id": context['context_id'],
             "context_reference_kind": 'alias' if supplied_context == alias else 'full_id',
             "candidate_text": text, "publication_status": "not_adopted",
             "source_text_is_verbatim": False, "localization_scope": "query_output_not_original_cell",
             "source": copy.deepcopy(context["source"]), "query_fingerprint": context["query_fingerprint"],
             "query_result_truncated": context["query_result_truncated"],
             "projection_truncated": context["projection_truncated"],
             "required_next_checks": ["original_claims", "query_relations", "claim_semantics", "coverage"]}
    return {"claim": claim, "audit": audit}


def _explicit_citations(claim, required, evidence_records):
    ids = claim.get('evidence_ids', list(dict.fromkeys(required)))
    known = {e.get('id') for e in evidence_records if isinstance(e, dict)}
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 16
            or any(not isinstance(eid, str) or eid not in known for eid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('evidence_ids须为1至16个已读取且不重复的显式引用')
    if any(eid not in ids for eid in required):
        raise ValueError('evidence_ids缺少本条显式选择格的依据；核对后补本条引用，不借另一条claim')
    return list(ids)


def has_query_facts(claims):
    return isinstance(claims, list) and any(isinstance(c, dict) and
        ('query_fact' in c or 'query_facts' in c) for c in claims)


def received_audit(claims, *, round_number):
    """Whitelist the received proposal before validation/cancellation; never evidence."""
    from .document_receipts import received_snapshot
    from .audit_redaction import redact_diagnostics
    rows = []
    for item in claims[:16] if isinstance(claims, list) else []:
        if not isinstance(item, dict): continue
        row = {k: copy.deepcopy(item[k]) for k in ('text', 'statement', 'evidence_ids', 'calculation_ids')
               if k in item and (isinstance(item[k], str) if k in ('text', 'statement') else
                   isinstance(item[k], list) and all(isinstance(x, str) for x in item[k]))}
        def selection(value):
            if not isinstance(value, dict): return {'invalid_type': True}
            safe = {k: copy.deepcopy(v) for k, v in value.items() if k in ALLOWED
                    and (isinstance(v, str) or type(v) is bool or
                         k == 'entity_cells' and isinstance(v, list) and all(isinstance(x, str) for x in v))}
            safe['omitted_fields_count'] = len(value) - len(safe)
            return safe
        if 'query_fact' in item: row['query_fact'] = selection(item['query_fact'])
        if isinstance(item.get('query_facts'), list):
            row['query_facts'] = [{'selection': selection(f.get('selection')),
                'statement': f.get('statement') if isinstance(f.get('statement'), str) else None}
                for f in item['query_facts'][:8] if isinstance(f, dict)]
            row['omitted_query_facts'] = len(item['query_facts']) - len(row['query_facts'])
        row['omitted_fields_count'] = len(set(item) - {'query_fact','query_facts','text','statement','evidence_ids','calculation_ids'})
        rows.append(row)
    safe = redact_diagnostics({'claims': rows})
    return {'scope': 'query_fact_received', 'publication_status': 'not_adopted',
            'round': round_number, 'render_policy': VERSION,
            'original_claim_count': len(claims) if isinstance(claims, list) else None,
            'received_sha256': hashlib.sha256(json.dumps(claims,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest(),
            'response': received_snapshot(safe), 'not_supporting_evidence': True}


def expand_candidate_claims(claims, evidence_records, *, max_chars=24000):
    """Explicit choices become metadata; submitted prose is kept or rejected.

    Every natural-language fact span passes the UNCHANGED output-cell validator.
    No numerical/semantic verdict is inferred and no nonempty prose is dropped.
    """
    if not isinstance(claims, list) or not 1 <= len(claims) <= 16:
        raise ValueError('候选须为1至16条')
    result, audit = [], []
    for index, claim in enumerate(claims, 1):
        if not isinstance(claim, dict): raise ValueError('候选必须是对象')
        if 'query_fact' not in claim and 'query_facts' not in claim:
            result.append(copy.deepcopy(claim)); continue
        try:
            if set(claim) - {'query_fact','query_facts','text','statement','evidence_ids','calculation_ids'}:
                raise ValueError('query_fact叙述只接受text/statement/evidence_ids与空calculation_ids，不混旧绑定或其他字段')
            if 'query_fact' in claim and 'query_facts' in claim:
                raise ValueError('query_fact和query_facts互斥')
            if 'calculation_ids' in claim and claim['calculation_ids'] != []:
                raise ValueError('query_fact不能混入计算；calculation_ids仅兼容空数组')
            prose = claim.get('text')
            if 'text' in claim and (not isinstance(prose,str) or not prose.strip() or len(prose)>1800):
                raise ValueError('text须为1至1800字符字符串；不静默丢掉叙述后发布格值')
            if 'query_facts' in claim:
                specs = claim['query_facts']
                if 'statement' in claim or not isinstance(prose,str):
                    raise ValueError('多格query_facts须提供text，每格各有statement')
                if not isinstance(specs,list) or not 1<=len(specs)<=8:
                    raise ValueError('query_facts须为1至8项显式selection/statement')
                if any(not isinstance(f,dict) or set(f)!={'selection','statement'} for f in specs):
                    raise ValueError('query_facts每项仅含selection与statement，不填value或嵌套额外对象')
            else:
                if 'statement' in claim and prose is None: raise ValueError('statement不能脱离text单独提交')
                specs=[{'selection':claim['query_fact'],'statement':claim.get('statement',prose)}]
            rendered = [render_query_fact(f['selection'],evidence_records) for f in specs]
            if prose is None: prose=rendered[0]['claim']['text']
            bindings=[]
            for spec,value in zip(specs,rendered):
                binding=copy.deepcopy(value['claim']['query_bindings'][0])
                statement=spec['statement'] if spec['statement'] is not None else prose
                if not isinstance(statement,str) or not statement or len(statement)>1800:
                    raise ValueError('每格statement须为正文内完整、唯一的字符串子句')
                if prose.count(statement)!=1:
                    raise ValueError('statement须是实际正文中唯一精确连续子句')
                start=prose.index(statement);end=start+len(statement)
                # Do not accept a substring which cuts an identifier, sign,
                # numeric token or attached unit outside the bound statement.
                if ((start and re.match(r'[A-Za-z0-9_+./-]',prose[start-1]) and re.match(r'[A-Za-z0-9_]',statement[0]))
                    or (end<len(prose) and re.match(r'[A-Za-z0-9_.%‰]',prose[end]) and re.match(r'[A-Za-z0-9_]',statement[-1]))
                    or (end<len(prose) and statement[-1].isdigit() and prose[end].isalnum())):
                    raise ValueError('statement边界切开数值、标识符或紧邻单位，须复制完整子句')
                binding['statement']=statement;bindings.append(binding)
            ids=_explicit_citations(claim,[b['evidence_id'] for b in bindings],evidence_records)
            check=check_query_bindings(prose,bindings,[e for e in evidence_records if e.get('id') in ids])
            if check['valid'] is not True:
                raise ValueError('叙述未通过原查询格绑定：'+'；'.join(check['errors'])+
                    '。保留必答关系，用query_facts逐格选择并从text复制完整statement；不能删叙述只留格值')
            result.append({'text':prose,'evidence_ids':ids,'query_bindings':check['bindings']})
            for fact_index,value in enumerate(rendered,1):
                value['audit'].update(candidate_text=prose, narrative_preserved='text' in claim,
                    statement=bindings[fact_index-1]['statement'],fact_index=fact_index,
                    candidate_index=index, explicit_evidence_ids=ids)
                audit.append(value['audit'])
        except ValueError as exc:
            raise QueryFactError(str(exc),index) from exc
    if (type(max_chars) is not int or max_chars<1 or
            len(json.dumps({'claims':result,'render_audit':audit},ensure_ascii=False))>max_chars):
        raise ValueError('完整候选与审计超过有界预算；不静默丢弃')
    return {'claims':result,'render_audit':audit}


def failure_feedback(claims, claim_index, evidence_records):
    """Show existing projected row cells for repair, never synthesize a binding."""
    if not isinstance(claims,list) or not 1 <= claim_index <= len(claims):
        return {}
    claim=claims[claim_index-1]
    if not isinstance(claim,dict): return {}
    selections = ([claim['query_fact']] if isinstance(claim.get('query_fact'),dict) else
                  [item.get('selection') for item in claim.get('query_facts',[]) if isinstance(item,dict)])
    rows=[];seen=set()
    for selection in selections[:8]:
        if not isinstance(selection,dict): continue
        record=next((e for e in evidence_records if e.get('id')==selection.get('evidence_id')),None)
        if not record: continue
        context=record.get('query_result_context') or {}
        if (context.get('usable') is not True or selection.get('context_id') not in
                (context.get('context_id'),context.get('context_alias'))): continue
        for row in context.get('rows',[]):
            if not any(c.get('cell_id')==selection.get('cell_id') for c in row.get('cells',[])): continue
            key=(record['id'],row.get('row_index'))
            if key in seen: continue
            seen.add(key)
            cells=[{k:copy.deepcopy(cell[k]) for k in ('cell_id','column_label','value') if k in cell}
                   for cell in row.get('cells',[])[:8]]
            rows.append({'evidence_id':record['id'],'context_id':context.get('context_alias') or context.get('context_id'),
                         'selected_cell_id':selection.get('cell_id'),'row_index':row.get('row_index'),
                         'cells':cells,'omitted_cells':max(0,len(row.get('cells',[]))-len(cells))})
    if not rows: return {}
    from .audit_redaction import redact_diagnostics
    safe=redact_diagnostics({'existing_query_rows':rows[:3]})
    if len(json.dumps(safe,ensure_ascii=False))>3200:
        return {'repair_action':'优先从现有query_result_context逐格绑定，不重复相同SQL；若必需时期实体未返回，按范围指引更改查询补取。',
                'period_binding_guidance':PERIOD_BINDING_GUIDANCE,'diagnostic_only':True,'row_feedback_truncated':True}
    return {**safe,'repair_action':'当前单格选择只支持自身数值。若正文还含同一结果行的其他计数/金额，分别选其真实 cell_id，并把各完整子句写入 query_facts.statement 或拆成不同 claims。已有结果足够时无需重发相同SQL；缺少必需时期实体时按范围指引更改查询补取。不要自动补选，也不要删掉必答事实。',
            'period_binding_guidance':PERIOD_BINDING_GUIDANCE,'diagnostic_only':True,'action_rewritten':False}


def audit_record(rendered, evidence_records, *, round_number):
    """Whitelist/redact bounded diagnostic projection; never an evidence record."""
    from . import candidate_audit
    selection = rendered['selection']
    record = candidate_audit.capture_candidate(
        {'claims': [{'query_fact': selection, 'text': rendered['candidate_text'],
                     'evidence_ids': rendered.get('explicit_evidence_ids', [selection['evidence_id']])}]},
        {e['id']: e for e in evidence_records}, {}, round_number=round_number,
        stage='query_fact_rendered', reason='控制器按显式结果格选择生成候选；仍需完整事实、关系与覆盖复核。')
    record.update(scope='query_fact_rendering', validation_status='mechanical_candidate',
                  publication_status='not_adopted', render_policy=VERSION,
                  candidate_index=rendered['candidate_index'], source_text_is_verbatim=False,
                  narrative_preserved=rendered.get('narrative_preserved',False),fact_index=rendered.get('fact_index',1),
                  localization_scope='query_output_not_original_cell',
                  query_result_truncated=rendered['query_result_truncated'],
                  projection_truncated=rendered['projection_truncated'])
    record['resolved_context_id'] = rendered['resolved_context_id']
    record['context_reference_kind'] = rendered['context_reference_kind']
    if 'ignored_model_text' in rendered:
        record['ignored_model_text'] = copy.deepcopy(rendered['ignored_model_text'])
        record['redacted'] |= rendered['ignored_model_text']['redacted']
        record['truncated'] |= rendered['ignored_model_text']['truncated']
    record['source_binding'] = {k: candidate_audit._clean(v, 160, record, 'source_binding.' + k)
                               for k, v in {**rendered['source'], 'query_fingerprint': rendered['query_fingerprint']}.items()
                               if isinstance(v, str)}
    return candidate_audit._fit(record, candidate_audit.MAX_ATTEMPT_CHARACTERS)
