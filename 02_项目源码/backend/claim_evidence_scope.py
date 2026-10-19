"""Narrow citation-sufficiency checks; these never establish semantic truth.

Only declared statistical qualifiers are checked. Absence is not a claim that
an original lacks the information. Evidence and source texts remain immutable.
"""
from __future__ import annotations

import copy
import re

# Separate families: comparable prices must not silently license constant
# prices, and an estimated value must not license a preliminary actual value.
_QUALIFIERS = {
    'constant_price': ('不变价格', '不变价'),
    'comparable_price': ('可比价格', '可比价'),
    'current_price': ('现行价格', '当年价格', '现价'),
    'preliminary': ('初步核算', '初步统计数', '初步统计数据', '初步统计'),
    'estimated': ('预计', '预测'),
}
_DENIAL = re.compile(r'(?:不能|无法|未能|未明确|未说明|不确定|尚未确认|不能确认|没有说明|并非|不是|不按|未按)[^，,。；;\n]{0,12}$')


def _compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def declared_qualifiers(text):
    result = []
    for family, aliases in _QUALIFIERS.items():
        pattern = '|'.join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
        for match in re.finditer(pattern, text):
            prefix = re.split(r'[，,。；;\n]', text[:match.start()])[-1]
            suffix = text[match.end():]
            if _DENIAL.search(prefix) or re.match(r'(?:口径)?(?:尚)?(?:不明|未知|未确认)', suffix):
                continue
            result.append({'family': family, 'text': match.group(), 'start': match.start(), 'end': match.end()})
            break
    return result


def _texts(value):
    """Read only authoritative text fields, never arbitrary keys/model labels."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for member in value:
            yield from _texts(member)
    elif isinstance(value, dict):
        if isinstance(value.get('text'), str):
            yield value['text']


def qualifier_scope(record, bindings=()):
    context = record.get('table_context') or {}
    has_table = bool(context.get('has_table') or context.get('tables'))
    if not has_table:
        # SQL aliases/SQL text are not an original statistical qualifier.
        return [record.get('quote', '')] if record.get('kind') != 'sql' else []
    if context.get('truncated') or context.get('unresolved_structure') or context.get('location_unknown'):
        return []
    values = []
    own = [b for b in bindings if b.get('evidence_id') == record.get('id')]
    for binding in own:
        for key in ('header_path', 'row_headers', 'unit_evidence', 'footnotes', 'cell'):
            values.extend(_texts(binding.get(key)))
        for table in context.get('tables', []):
            if table.get('table_name') == binding.get('table_name'):
                for key in ('caption', 'context', 'footnotes'):
                    values.extend(_texts(table.get(key)))
    return values


def check_declared_qualifiers(text, evidence_records, bindings=()):
    declarations = declared_qualifiers(text)
    sources = [(record.get('id'), '\n'.join(qualifier_scope(record, bindings))) for record in evidence_records]
    supported, unsupported = [], []
    for declaration in declarations:
        ids = [eid for eid, value in sources if declaration['family'] in
               {item['family'] for item in declared_qualifiers(_compact(value))}]
        item = {**declaration, 'evidence_ids': ids}
        (supported if ids else unsupported).append(item)
    return {'supported': supported, 'unsupported': unsupported,
            'scope': 'declared_qualifier_presence_only_not_semantic_verification'}


def missing_requested_qualifiers(question, claims):
    """Notice dropped explicit statistical scope, not infer facts from history.

    The host supplies its reviewed standalone question for a followup. Questions
    asking whether a basis holds are not positive instructions to assert it.
    A retained qualifier still needs its own real source and semantic review.
    """
    present = {q['family'] for claim in claims for q in declared_qualifiers(claim.get('text', ''))}
    missing = []
    for item in declared_qualifiers(question):
        # A request about a prediction model is not an instruction to label
        # every result as an estimated value. Keep this guard narrowly scoped.
        if item['family'] == 'estimated':
            continue
        prefix = re.split(r'[，,。；;\n]', question[:item['start']])[-1]
        suffix = re.split(r'[，,。；;\n]', question[item['end']:])[0]
        if re.search(r'是否|是不是|能否|能不能|有无|还是', prefix) or re.search(r'吗|么|还是', suffix):
            continue
        if re.search(r'(?:不要|不需要|无需|不采用|不使用|不考虑|不含)[^，,。；;\n]{0,12}$', prefix):
            continue
        if item['family'] not in present:
            missing.append({'family':item['family'], 'text':item['text'],
                            'scope':'explicit_question_condition_not_source_evidence'})
    return missing


def claim_review_payload(context, claims, evidence, calculations, steps):
    """One real support package per claim, plus explicit executed dependencies.

    IDs reference the single evidence registry below to avoid budget-inflating
    duplicate text. Another claim's citations cannot fill this claim's gap.
    """
    packages, all_eids, all_cids, all_steps = [], set(), set(), set()
    step_by_id = {step['id']: step for step in steps}
    for claim in claims:
        direct = set(claim.get('evidence_ids', []))
        cids, pending = set(), list(claim.get('calculation_ids', []))
        dependency = set()
        while pending:
            cid = pending.pop()
            if cid in cids or cid not in calculations:
                continue
            cids.add(cid); calculation = calculations[cid]
            dependency.update(calculation.get('evidence_ids', []))
            pending.extend(p.get('binding', {}).get('calculation_id')
                           for p in calculation.get('parameters', []) if isinstance(p, dict))
        needed = direct | dependency
        selected_steps, pending = set(), [s['id'] for s in steps
            if needed.intersection(s.get('evidence_ids', [])) or s.get('calculation', {}).get('id') in cids]
        while pending:
            sid = pending.pop()
            if sid in selected_steps or sid not in step_by_id:
                continue
            selected_steps.add(sid)
            for ancestor in step_by_id[sid].get('depends_on', []):
                if ancestor in step_by_id:
                    dependency.update(step_by_id[ancestor].get('evidence_ids', []))
                    pending.append(ancestor)
        all_eids.update(direct | dependency); all_cids.update(cids); all_steps.update(selected_steps)
        packages.append({'claim_id': claim['id'], 'evidence_ids': sorted(direct),
            'calculation_ids': sorted(cids), 'dependency_evidence_ids': sorted(dependency - direct),
            'step_ids': [s['id'] for s in steps if s['id'] in selected_steps],
            'support_rule': 'Only this claim evidence_ids and its executed calculation support the assertion; dependencies audit input provenance, not unreferenced extra assertions.'})
    used_sources = {evidence[eid].get('source_id') for eid in all_eids if eid in evidence}
    payload = {key: copy.deepcopy(context[key]) for key in ('question', 'question_parts', 'history', 'supported_units') if key in context}
    payload['history_role'] = 'question_scope_only_not_source_evidence'
    payload['sources'] = [{key: source.get(key) for key in ('id', 'name', 'format', 'sha256', 'parser_version', 'page_unit')}
                          for source in context.get('sources', []) if source.get('id') in used_sources]
    payload['source_groups'] = copy.deepcopy(context.get('source_groups', []))
    payload['claim_support'] = packages
    payload['evidence'] = [copy.deepcopy(item) for eid, item in evidence.items() if eid in all_eids]
    payload['calculations'] = [copy.deepcopy(item) for cid, item in calculations.items() if cid in all_cids]
    payload['steps'] = []
    for step in steps:
        if step['id'] not in all_steps:
            continue
        # No planner candidate, global tool errors or unrelated read result can
        # serve as support during this independent review.
        projected = {key: copy.deepcopy(step[key]) for key in
            ('id', 'kind', 'depends_on', 'query', 'calculation', 'search') if key in step}
        projected['evidence_ids'] = [eid for eid in step.get('evidence_ids', []) if eid in all_eids]
        payload['steps'].append(projected)
    return payload


def review_image_keys(payload):
    keys = set()
    for record in payload['evidence']:
        if record.get('page') is not None:
            keys.add((record.get('source_id'), record['page']))
        for quality in record.get('table_quality', []):
            if quality.get('page') is not None:
                keys.add((record.get('source_id'), quality['page']))
    return keys
