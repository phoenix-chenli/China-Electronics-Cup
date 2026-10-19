"""Exact provenance labels are annotations, never free business-number tokens."""
from __future__ import annotations
import hashlib
import re

_CODE = re.compile(r'(?<![A-Za-z0-9])(?:[A-Za-z]{1,16}(?:/[A-Za-z]{1,16})?)[ \t-]*\d+(?:[-./]\d+)+(?![A-Za-z0-9])')
_REFERENCE = re.compile(r'依据|根据|参见|引用|文献|手册|标准|规范|报告|资料|(?:^|[，。；\s])按\s*', re.I)
_HYPOTHETICAL = re.compile(r'假设|假定|情景|试算|给定|given|assum', re.I)
# Only complete, explicitly negated fact-status disclaimers are neutral here.
# The boundary excludes double negation/reporting such as "不能说不是实际值";
# any other actual/observed assertion in the same context still blocks masking.
_NEGATED_ACTUAL = re.compile(
    r'(^|[（(，,。；;：:\n])\s*(?:(?:这|此|该|本)(?:个)?(?:结果|情景|数值|金额)?)?'
    r'(?:不是|并非|不属于|不代表)\s*'
    r'(?:实际(?:登记)?(?:结果|金额|记录|数值|值)|实测(?:结果|数值|值)|观测(?:结果|数值|值)|真实值)'
    r'(?=$|[）)，,。；;：:\n])')


def _has_actual_assertion(text):
    # This is only a polarity check, not another numeric annotation. No number
    # or disclaimer text is removed from the actual candidate or published text.
    remainder = _NEGATED_ACTUAL.sub(lambda m: ' ' * len(m[0]), text)
    return bool(re.search(r'实测|实际|观测|真实值', remainder))


def annotate_context(text, ids, evidence, calculations, sources):
    """Mask only occurrence spans confirmed by cited source/user provenance.

    No matching token is added to an allowed-number set. A value repeated as a
    measurement or amount elsewhere in the same sentence remains unmasked.
    """
    annotations, spans = [], []
    pages = []
    for eid in ids:
        record = evidence[eid]
        source = sources.get(record.get('source_id'))
        if not source or any(record.get(k) != source.get(k) for k in ('sha256','parser_version')):
            continue
        page = record.get('page')
        current = next((p for i,p in enumerate(source.get('pages') or [])
                        if p.get('number',i+1) == page), None)
        if current is not None:
            pages.append((eid,source,page,current.get('text','')))
    for match in re.finditer(r'第\s*(\d+)\s*页',text):
        number = int(match[1])
        targets = {(s['id'],p) for _,s,p,_ in pages if p == number and
                   ((s.get('metadata') or {}).get('page_unit') == 'physical_page' or s.get('format') == 'pdf')}
        if len(targets) == 1:
            sid,page = next(iter(targets))
            spans.append(match.span())
            annotations.append({'kind':'page_label','origin':'source_metadata','source_id':sid,
                                'page':page,'label':match[0],'start':match.start(),'end':match.end()})
    for match in _CODE.finditer(text):
        clause_start = max(text.rfind(c,0,match.start()) for c in '。；;\n') + 1
        if not _REFERENCE.search(text[max(clause_start,match.start()-70):match.start()]):
            continue
        code = re.sub(r'\s+','',match[0])
        targets = {(s['id'],p) for _,s,p,raw in pages
                   if any(re.sub(r'\s+','',found[0]) == code for found in _CODE.finditer(raw))}
        if len({sid for sid,_ in targets}) == 1:
            spans.append(match.span())
            annotations.append({'kind':'source_identifier','origin':'cited_source_page',
                'source_id':next(iter(targets))[0],'pages':sorted({p for _,p in targets}),
                'label':match[0],'start':match.start(),'end':match.end()})
    seen_inputs = set()
    for calculation in calculations:
        for parameter in calculation.get('parameters',[]):
            item = parameter.get('user_input') or {}
            binding = parameter.get('binding') or {}
            original,quote = item.get('source_text'),item.get('quote')
            start,end = item.get('start'),item.get('end')
            if (item.get('origin') != 'user_question' or item.get('is_hypothetical') is not True
                    or item.get('is_document_evidence') is not False or item.get('id') in seen_inputs
                    or binding.get('user_input_id') != item.get('id')
                    or not isinstance(original,str) or not isinstance(quote,str)
                    or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(original)
                    or original[start:end] != quote
                    or hashlib.sha256(original.encode()).hexdigest() != item.get('source_sha256')):
                continue
            seen_inputs.add(item['id'])
            escaped_quote = r'\s*'.join(re.escape(part) for part in quote.split())
            if item.get('parameter_kind') == 'quantity' and isinstance(item.get('symbol'),str):
                pattern = r'(?<![A-Za-z0-9_])'+re.escape(item['symbol'])+r'\s*[=＝]\s*'+escaped_quote+r'(?![A-Za-z0-9_./*^²³])'
            elif item.get('parameter_kind') == 'percentage':
                verbs = r'减少|降低|下调' if item.get('direction') == 'decrease' else r'增加|提高|上调|增长'
                pattern = r'(?:'+verbs+r')\s*(?:了)?\s*(?:用户(?:明确)?(?:假设|假定)的\s*)?'+escaped_quote+r'(?![A-Za-z0-9_./*^²³])'
            else:
                continue
            for match in re.finditer(pattern,text):
                clause_start = max(text.rfind(c,0,match.start()) for c in '。；;\n') + 1
                prefix = text[max(clause_start,match.start()-90):match.start()]
                ends = [v for mark in '，,。；;\n' if (v := text.find(mark,match.end())) >= 0]
                suffix = text[match.end():min(ends,default=len(text))]
                polarity_prefix = _NEGATED_ACTUAL.sub(lambda m: ' ' * len(m[0]), prefix)
                denied = re.search(r'(?:不(?:是|属)|并非|未|没有|撤销|取消|否认|不再|不要|不能|不应|不得|禁止|不采用|不使用)[^，,。；;\n]{0,12}(?:假设|假定|给定|情景|试算)',polarity_prefix + match[0])
                explicit_user_label = re.search(r'用户(?:明确)?(?:假设|假定)的', match[0])
                if (not (_HYPOTHETICAL.search(prefix) or explicit_user_label) or denied
                        or re.search(r'撤销|取消|不再沿用|停止沿用|不要沿用', prefix+suffix)
                        or _has_actual_assertion(prefix+suffix)):
                    continue
                if text[match.end():].lstrip().startswith(('/', '*', '·', '^', '²', '³', '每')):
                    continue
                spans.append(match.span())
                annotations.append({'kind':'user_assumption','origin':'user_question','user_input_id':item['id'],
                    'source_answer_id':item.get('source_answer_id'),'label':match[0],
                    'start':match.start(),'end':match.end(),'is_document_evidence':False})
    masked = list(text)
    for start,end in spans:
        masked[start:end] = ' '*(end-start)
    return ''.join(masked),annotations
