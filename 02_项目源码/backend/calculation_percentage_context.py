"""Occurrence-local percentage factors from executed, user-authorized calculations."""
from __future__ import annotations
import ast
from decimal import Decimal, InvalidOperation
import re
from . import user_scenarios
from .claim_context_annotations import _HYPOTHETICAL, _NEGATED_ACTUAL, _has_actual_assertion

_FACTOR = re.compile(r'[（(]\s*1\s*(?P<operator>[-+])\s*(?P<percentage>[0-9]+(?:\.[0-9]+)?\s*[%％])\s*[）)]')


def _percentage_proof(identifier, calculation):
    if (not isinstance(calculation, dict) or calculation.get('id') != identifier
            or calculation.get('rule_origin') != 'user_scenario'
            or calculation.get('is_hypothetical') is not True):
        return None
    parameters = calculation.get('parameters')
    if not isinstance(parameters, list) or len(parameters) != 2 or not all(isinstance(p, dict) for p in parameters):
        return None
    rates = [p for p in parameters if isinstance(p.get('user_input'), dict) and p['user_input'].get('parameter_kind') == 'percentage']
    if len(rates) != 1:
        return None
    rate = rates[0]; base = next(p for p in parameters if p is not rate)
    item = rate['user_input']; binding = rate.get('binding')
    if not isinstance(binding, dict):
        return None
    if (rate.get('unit') != '%' or rate.get('provenance') != 'user_question'
            or rate.get('is_hypothetical') is not True or rate.get('is_document_evidence') is not False
            or item.get('origin') != 'user_question' or item.get('is_hypothetical') is not True
            or item.get('is_document_evidence') is not False
            or not isinstance(item.get('source_answer_id'), str) or not item['source_answer_id']
            or base.get('user_input') or not base.get('evidence_ids')
            or not isinstance(base.get('name'), str) or not isinstance(rate.get('name'), str)
            or base['name'] == rate['name'] or item.get('direction') not in ('increase', 'decrease')):
        return None
    try:
        raw, verified = user_scenarios.resolve_input(binding, {'inputs': [item]})
        # Recheck the source utterance's actual authorization, not just its hash.
        original_context = user_scenarios.build_context('沿用当前假设', [
            {'answer_id': item['source_answer_id'], 'original_question': item['source_text']}])
        if not any(all(candidate.get(key) == item.get(key) for key in
            ('id','source_sha256','quote','start','end','direction','parameter_kind'))
            for candidate in original_context['inputs']):
            return None
        value = Decimal(raw)
        if not value.is_finite() or Decimal(str(rate.get('value'))) != value:
            return None
        normalized = value / Decimal(100)
        if normalized < 0 or item['direction'] == 'decrease' and normalized > 1:
            return None
        handling = calculation.get('unit_handling')
        if not isinstance(handling, dict) or not isinstance(handling.get('parameter_base_values'), list):
            return None
        bases = handling['parameter_base_values']
        resolved = [p for p in bases if isinstance(p, dict) and p.get('parameter') == rate['name']]
        if len(resolved) != 1 or Decimal(str(resolved[0].get('base_value'))) != normalized:
            return None
        operator = '-' if item['direction'] == 'decrease' else '+'
        expression = ast.dump(ast.parse(calculation['expression'], mode='eval'), include_attributes=False)
        expected = [f"{base['name']}*(1{operator}{rate['name']})", f"(1{operator}{rate['name']})*{base['name']}"]
        if expression not in [ast.dump(ast.parse(e,mode='eval'), include_attributes=False) for e in expected]:
            return None
    except (ValueError, TypeError, KeyError, SyntaxError, InvalidOperation):
        return None
    return {'calculation_id': identifier, 'rate_parameter': rate['name'], 'operator': operator,
            'ratio': normalized, 'user_input': verified, 'expression': calculation['expression']}


def annotate_calculation_percentages(text, calculation_ids, registry):
    """Bind only each explicit (1 +/- rate%) occurrence, never a free number.

    The registry is controller-owned and the caller has checked claim IDs.
    This only permits final independent scenario/claim review, never approval.
    """
    proofs=[];seen=set();pending=list(calculation_ids)
    while pending and len(seen) < 32:
        identifier=pending.pop()
        if identifier in seen:continue
        seen.add(identifier);record=registry.get(identifier)
        if not isinstance(record,dict) or record.get('id') != identifier:continue
        proof=_percentage_proof(identifier,record)
        if proof:proofs.append(proof)
        parameters=record.get('parameters')
        if not isinstance(parameters,list):continue
        pending.extend(p.get('binding',{}).get('calculation_id') for p in parameters
                       if isinstance(p,dict) and isinstance(p.get('binding'),dict)
                       and isinstance(p['binding'].get('calculation_id'),str))
    annotations=[];masked=list(text)
    for match in _FACTOR.finditer(text):
        start,end=match.span('percentage');literal=match['percentage']
        try:ratio=Decimal(re.sub(r'\s|[%％]','',literal))/Decimal(100)
        except InvalidOperation:continue
        matching=[proof for proof in proofs if proof['operator']==match['operator'] and proof['ratio']==ratio]
        identities={proof['user_input']['id'] for proof in matching}
        if len(identities)!=1:continue
        clause_start=max(text.rfind(c,0,match.start()) for c in '。；;\n')+1
        prefix=text[clause_start:match.start()]
        ends=[i for mark in '，,。；;\n' if (i:=text.find(mark,match.end()))>=0]
        suffix=text[match.end():min(ends,default=len(text))]
        polarity_prefix=_NEGATED_ACTUAL.sub(lambda m:' '*len(m[0]),prefix)
        if (not _HYPOTHETICAL.search(prefix)
                or re.search(r'(?:不(?:是|属)|并非|未|没有|撤销|取消|否认|不再|不要|不能|不应|不得|禁止)[^，,。；;\n]{0,12}(?:假设|假定|给定|情景|试算)',polarity_prefix)
                or re.search(r'撤销|取消|不再沿用|停止沿用|不要沿用',prefix+suffix)
                or _has_actual_assertion(prefix+suffix)
                or not (re.search(r'乘以|计算|公式|表达式|代入|[×*=]',prefix[-30:])
                        or re.match(r'\s*[×*]',suffix))
                or text[match.end():].lstrip().startswith(('/', '每'))):
            continue
        item=matching[0]['user_input']
        masked[start:end]=' '*(end-start)
        annotations.append({'kind':'calculation_user_percentage','origin':'executed_user_scenario_parameter',
            'calculation_ids':[proof['calculation_id'] for proof in matching],
            'user_input_id':item['id'],'source_answer_id':item['source_answer_id'],
            'source_sha256':item['source_sha256'],'source_quote':item['quote'],
            'source_start':item['start'],'source_end':item['end'],
            'label':literal,'start':start,'end':end,'normalized_ratio':str(ratio),
            'direction':item['direction'],'is_document_evidence':False,
            'semantic_status':'pending_independent_scenario_and_claim_review'})
    return ''.join(masked),annotations
