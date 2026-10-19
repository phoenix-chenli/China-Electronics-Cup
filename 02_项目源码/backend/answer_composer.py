"""Lossless presentation of terminal, already-verified engine results.

This is an exact projection, not another author or verifier. The caller must
pass the engine's published result, never a planner candidate. Audit snapshots,
rejected SQL rows and model traces are never promoted into display facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

MAX_COMPOSITION_CHARS = 120000
MAX_CLAIMS = 64
PUBLISHABLE = {"completed", "insufficient_evidence"}


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def published_claims(result, status):
    """Validate the shape of engine-published facts, with no candidate fallback.

    A partial terminal answer may have limited overall coverage while its
    retained claims have passed individual review. This helper preserves that
    distinction instead of discarding all useful partial facts.
    """
    if not isinstance(result, dict) or status not in PUBLISHABLE:
        return [], ["not_a_publishable_terminal_result"]
    verification = result.get('verification')
    if not isinstance(verification, dict) or verification.get('status') not in {'passed', 'limited'}:
        return [], ["published_verification_not_recorded"]
    evidence = result.get('evidence', [])
    if not isinstance(evidence, list):
        return [], ["invalid_evidence_collection"]
    ids = [e.get('id') for e in evidence if isinstance(e, dict)]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(evidence) or len(set(ids)) != len(ids):
        return [], ["ambiguous_evidence_identifiers"]
    allowed = set(ids)
    calculations = {c.get('id'): c for c in result.get('calculations') or [] if isinstance(c, dict)}
    claims = result.get('claims', [])
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        return [], ["claim_collection_budget_or_shape"]
    seen = set(); clean = []; issues = []
    for claim in claims:
        if not isinstance(claim, dict):
            issues.append('invalid_claim'); continue
        ident, text, refs = claim.get('id'), claim.get('text'), claim.get('evidence_ids')
        calc_ids = claim.get('calculation_ids') or []
        user_arithmetic = (isinstance(calc_ids, list) and bool(calc_ids)
            and all(isinstance(k, str) and k in calculations
                    and calculations[k].get('rule_origin') == 'user_question'
                    and bool(calculations[k].get('parameters'))
                    and all(p.get('provenance') == 'user_question' for p in calculations[k]['parameters'])
                    for k in calc_ids))
        if (not isinstance(ident, str) or not ident or ident in seen
                or not isinstance(text, str) or not text.strip()
                or not isinstance(refs, list) or (not refs and not user_arithmetic)
                or any(not isinstance(e, str) or e not in allowed for e in refs)
                or claim.get('supported') is False or claim.get('verified') is False):
            issues.append('invalid_published_claim'); continue
        seen.add(ident)
        clean.append(copy.deepcopy(claim))
    return clean, list(dict.fromkeys(issues))


def _kind(question, status, result, claims):
    if status == 'needs_clarification':return 'clarification'
    if status not in PUBLISHABLE:return 'recovery'
    if status == 'insufficient_evidence':return 'partial_answer' if claims else 'missing_evidence'
    if any(c.get('calculation_ids') for c in claims):return 'calculation'
    if re.search(r'比较|对比|差异|相比|分别|同比|环比', question or ''):return 'comparison'
    if len(claims) <= 1:return 'direct_fact'
    return 'evidence_summary'


def compose_answer(result, status, question='', mode=None):
    """Return additive ``answer_plan/display_blocks/composition_audit`` fields.

    Claim bodies, values and qualifiers are copied byte-for-byte. A result
    table or calculation is only shown when linked to the published claims.
    Original text is kept for audit, never replaced by a synthesized paragraph.
    """
    result = result if isinstance(result, dict) else {}
    original = result.get('text') if isinstance(result.get('text'), str) else ''
    claims, issues = published_claims(result, status)
    kind = _kind(question, status, result, claims)
    audit = {'version':'exact-answer-composition-1', 'method':'lossless_published_fact_projection',
             'original_text':original, 'original_text_sha256':_digest(original),
             'new_model_calls':0, 'semantic_rewrite_performed':False,
             'publication_basis':'terminal_engine_published_claims',
             'issues':issues, 'status':'ready', 'checks':{}}
    output = {'answer_plan':{'version':'answer-plan-1', 'kind':kind, 'mode':mode,
                            'claim_order':[], 'assumptions_added':False},
              'display_blocks':[], 'composition_audit':audit}
    if status == 'needs_clarification':
        detail = result.get('clarification')
        if isinstance(detail, dict) and isinstance(detail.get('question'), str) and detail['question'].strip():
            output['display_blocks'] = [{'id':'clarification', 'kind':'clarification',
                'text':detail['question'], 'options':copy.deepcopy(detail.get('options', [])),
                'field':detail.get('field', ''), 'requires_user_input':True}]
        else:audit['status']='unavailable'
        audit['issues']=[] if output['display_blocks'] else ['clarification_not_recorded']
        return output
    if status not in PUBLISHABLE:
        output['display_blocks'] = [{'id':'recovery', 'kind':'recovery',
            'text':'本次回答已取消，可修改问题后重新发起。' if status=='cancelled' else '本次回答未完成，可检查资料与问题后重新发起。',
            'automatic_resubmission':False}]
        audit['issues']=[]; return output
    if issues:
        # Do not make malformed historical claims look newly verified. The
        # caller can retain its existing history view and show this limitation.
        audit['status']='unavailable'; return output
    if status=='completed' and not claims:
        audit['status']='unavailable';audit['issues'].append('published_claims_not_recorded');return output
    coverage = result.get('completeness') or result.get('coverage') or {}
    parts = coverage.get('items', []) if isinstance(coverage, dict) else []
    by_id = {c['id']:c for c in claims}
    order = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):continue
        for cid in part.get('claim_ids', []) if isinstance(part.get('claim_ids', []), list) else []:
            if isinstance(cid, str) and cid in by_id and cid not in order:order.append(cid)
    order.extend(c['id'] for c in claims if c['id'] not in order)
    output['answer_plan']['claim_order']=order
    blocks=[]
    if kind=='partial_answer':
        blocks.append({'id':'partial-scope','kind':'scope_notice','text':'当前可确认的部分如下。','complete':False})
    for cid in order:
        claim=by_id[cid]
        blocks.append({'id':'fact-'+cid,'kind':'fact','text':claim['text'],
                       'claim_ids':[cid],'evidence_ids':list(dict.fromkeys(claim['evidence_ids'])),
                       **({'calculation_ids':list(claim['calculation_ids'])} if claim.get('calculation_ids') else {}),
                       'citation_placement':'immediately_after_fact',
                       'text_sha256':_digest(claim['text']),
                       'provenance':'published_claim'})
    # SQL/table output is already the final executed-and-reviewed result. Never
    # fall back to query.executed_result (which stores rejected numeric output).
    columns, rows = result.get('columns'), result.get('rows')
    if (status=='completed' and claims and isinstance(columns,list) and columns
            and isinstance(rows,list) and all(isinstance(r,list) and len(r)==len(columns) for r in rows)):
        table={'id':'result-main','kind':'result_table','columns':copy.deepcopy(columns),
               'rows':copy.deepcopy(rows),'claim_ids':order,
               'evidence_ids':list(dict.fromkeys(e for c in claims for e in c['evidence_ids'])),
               'provenance':'published_completed_result',
               'truncated':bool((result.get('query') or {}).get('truncated'))}
        display=result.get('display_rows')
        if isinstance(display,list) and len(display)==len(rows) and all(isinstance(r,list) and len(r)==len(columns) for r in display):
            table['display_rows']=copy.deepcopy(display)
        blocks.append(table)
    used_calculations={i for c in claims for i in (c.get('calculation_ids') if isinstance(c.get('calculation_ids'),list) else []) if isinstance(i,str)}
    calculations=result.get('calculations') or []
    evidence_ids={e['id'] for e in result.get('evidence',[])}
    for calculation in calculations if isinstance(calculations,list) else []:
        if not isinstance(calculation,dict) or not isinstance(calculation.get('id'),str) or calculation.get('id') not in used_calculations:continue
        refs=calculation.get('evidence_ids')
        if not isinstance(refs,list) or any(not isinstance(e,str) or e not in evidence_ids for e in refs):
            audit['issues'].append('calculation_reference_unavailable'); continue
        fields=('id','expression','parameters','value','unit','display_value','precision','evidence_ids',
                'rule_evidence_ids','rule_origin','numeric_policy','is_hypothetical','operation_authorization','unit_handling')
        blocks.append({'id':'calculation-'+calculation['id'],'kind':'calculation',
            'calculation':{k:copy.deepcopy(calculation[k]) for k in fields if k in calculation},
            'claim_ids':[c['id'] for c in claims if calculation['id'] in c.get('calculation_ids',[])],
            'evidence_ids':list(refs),'provenance':'published_calculation','recomputed_by_composer':False})
    if status=='insufficient_evidence':
        gaps=[]
        for part in parts if isinstance(parts,list) else []:
            if isinstance(part,dict) and part.get('status')!='answered' and isinstance(part.get('text'),str):
                gaps.append({'question_id':part.get('id'), 'question_text':part['text'],
                             'coverage_status':part.get('status','unknown')})
        blocks.append({'id':'coverage-gap','kind':'coverage_gap','items':gaps,
            'text':'以上事实尚不足以完整回答问题；可补充这些分项所需的资料后继续核对。' if claims else '当前已读取的资料尚不足以形成可核验的完整答案。',
            'original_source_absence_claimed':False})
    # Preserve all facts rather than silently cropping a professional answer.
    try:encoded=json.dumps(blocks,ensure_ascii=False,allow_nan=False)
    except (TypeError,ValueError):encoded='x'*(MAX_COMPOSITION_CHARS+1)
    if len(encoded)>MAX_COMPOSITION_CHARS:
        audit['status']='unavailable'; audit['issues'].append('display_budget_exceeded_without_truncating_facts')
        return output
    output['display_blocks']=blocks
    rendered={b['claim_ids'][0]:b['text'] for b in blocks if b['kind']=='fact'}
    audit['checks']={'all_published_claims_preserved':set(rendered)==set(by_id),
                     'claim_texts_exactly_preserved':all(rendered[cid]==by_id[cid]['text'] for cid in rendered),
                     'fact_citations_adjacent':all(b.get('evidence_ids') or b.get('calculation_ids') for b in blocks if b['kind']=='fact'),
                     'rejected_audit_content_excluded':True,
                     'numeric_entities_years_qualifiers_unchanged':all(rendered[cid]==by_id[cid]['text'] for cid in rendered)}
    return output
