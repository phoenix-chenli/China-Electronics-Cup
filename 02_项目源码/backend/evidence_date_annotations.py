"""Occurrence-local calendar translation supported by this claim's plain text.

This annotates an exact complete year/month interval. It never authorizes a
pool of month numbers or proves that a period applies to a business fact.
Evidence records are controller-created source spans; no model-created dates,
uncited records, table transcriptions or file metadata are accepted here.
"""
from __future__ import annotations

import re

_MONTHS = {name.lower(): number for number, name in enumerate((
    'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December'), 1)}
_MONTH = '|'.join(_MONTHS)
_CHINESE = re.compile(
    r'(?<!\d)(?P<y1>\d{4})\s*年\s*(?P<m1>\d{1,2})\s*月\s*'
    r'(?:至|到|[—–-])\s*(?P<y2>\d{4})\s*年\s*(?P<m2>\d{1,2})\s*月'
    r'(?!\s*(?:\d|日|号))')
_ENGLISH = re.compile(
    r'(?<![A-Za-z0-9])(?P<m1>' + _MONTH + r')\s+(?P<y1>\d{4})\s*'
    r'(?:to|through|until|[—–-])\s*(?P<m2>' + _MONTH + r')\s+(?P<y2>\d{4})'
    r'(?!\d)', re.IGNORECASE)
_NEGATION = re.compile(
    r'不是|并非|不属于|不在|未覆盖|不覆盖|排除|尚未确认|无法确认|未经确认|否认|'
    r'\b(?:not|never|neither|outside|exclude|excludes|excluding|excluded|except|incorrect|unconfirmed|unknown|'
    r"isn't|wasn't|weren't|doesn't|didn't)\b",
    re.IGNORECASE)
_BUSINESS_SUFFIX = re.compile(
    r'\s*(?:mg/L|g/L|MGD|MPa|kPa|亿元|万元|元|人|吨|千克|公斤|公里|平方米|%|％)', re.IGNORECASE)
_CLAUSE_END = re.compile(r'[。；;!?！？]|(?<!\d)\.(?=\s|$)')


def source_identity(record):
    """Same identity fields used by the existing immutable evidence registry."""
    values = tuple(record.get(key) for key in ('source_id', 'sha256', 'parser_version'))
    return values if all(isinstance(value, str) and value for value in values) else None


def _ranges(text):
    for pattern, language in ((_CHINESE, 'zh'), (_ENGLISH, 'en')):
        for match in pattern.finditer(text):
            month = lambda name: int(match[name]) if language == 'zh' else _MONTHS[match[name].lower()]
            value = (int(match['y1']), month('m1'), int(match['y2']), month('m2'))
            yield match, value, language


def has_complete_calendar_range(text):
    return isinstance(text, str) and next(_ranges(text), None) is not None


def _positive(text, start, end):
    # Keep both sides of the date, but stop at sentence boundaries. Newlines
    # may be PDF wrapping inside an English date and are not sentence ends.
    boundaries = list(_CLAUSE_END.finditer(text))
    left = max((m.end() for m in boundaries if m.end() <= start), default=0)
    right = min((m.start() for m in boundaries if m.start() >= end), default=len(text))
    prefix = text[max(left, start - 140):start]
    prefix_parts = re.split(r'[,，]', prefix)
    # A comma immediately before an English range can introduce an appositive
    # ("evaluation period, October ..."); retain that period's qualifier.
    prefix = prefix_parts[-1] if prefix_parts[-1].strip() else (prefix_parts[-2] if len(prefix_parts) > 1 else '')
    suffix = re.split(r'[,，]', text[end:min(right, end + 100)], maxsplit=1)[0]
    context = prefix + text[start:end] + suffix
    return not _NEGATION.search(context)


def _ordered(value):
    y1, m1, y2, m2 = value
    return 1 <= y1 <= 9999 and 1 <= y2 <= 9999 and 1 <= m1 <= 12 and 1 <= m2 <= 12 and (y1, m1) <= (y2, m2)


def _plain_anchor(record):
    context = record.get('table_context') or {}
    quote, start, end, page = (record.get(key) for key in ('quote', 'start', 'end', 'page'))
    if (record.get('kind') != 'text' or record.get('stale') is True or not source_identity(record)
            or not isinstance(record.get('id'), str) or not record['id']
            or context.get('has_table') or context.get('tables') or context.get('unresolved_structure')
            or context.get('location_unknown') or record.get('query_result') is not None
            or not isinstance(quote, str) or type(page) is not int or page < 1
            or type(start) is not int or type(end) is not int or start < 0 or end <= start
            or end - start != len(quote)):
        return None
    return {'page': page, 'start': start, 'end': end}


def annotate_evidence_date_ranges(text, records, *, allowed_identities=None, excluded_spans=(), source_scopes=()):
    """Return annotations/errors without changing text or any source record.

    ``records`` must be only this claim's actual citations. In a table claim,
    ``allowed_identities`` comes from validated original-cell bindings. Plain
    claims omit it and use their own cited source identities. No new signature
    or metadata field is required of evidence that does not use this feature.
    """
    result = {'annotations': [], 'unsupported': []}
    candidates = [(match, value, language) for match, value, language in _ranges(text)
                  if not any(start <= match.start() and match.end() <= end for start, end in excluded_spans)]
    if not candidates:
        return result
    allowed = (set(allowed_identities) if allowed_identities is not None else
               {source_identity(record) for record in records if source_identity(record)})
    source_ranges = []
    for record in records:
        anchor = _plain_anchor(record)
        if not anchor or source_identity(record) not in allowed:
            continue
        quote = record['quote']
        for match, value, language in _ranges(quote):
            if (_ordered(value) and _positive(quote, match.start(), match.end())
                    and not _BUSINESS_SUFFIX.match(quote[match.end():])):
                source_ranges.append((record, anchor, match, value, language))
    for match, value, language in candidates:
        reason = None
        local_identities = {identity for start, end, identity in source_scopes
                            if start <= match.start() and match.end() <= end}
        if source_scopes and not local_identities:
            local_identities = allowed if len(allowed) == 1 else set()
        if source_scopes and len(local_identities) != 1:
            reason = '该日期没有唯一的表格来源语境，请按真实来源拆分有引用的日期句'
        if not _ordered(value):
            reason = '年月区间无效或起止顺序相反'
        elif not _positive(text, match.start(), match.end()):
            reason = '该日期处于否定、排除或未确认语境，不能按肯定区间注记'
        elif _BUSINESS_SUFFIX.match(text[match.end():]):
            reason = '日期后直接出现业务单位，不能作为日历注记'
        matches = [entry for entry in source_ranges if entry[3] == value
                   and (not source_scopes or source_identity(entry[0]) in local_identities)]
        if not reason and not matches:
            reason = '本条实际引用的同源普通正文没有对应的完整年月区间'
        if reason:
            result['unsupported'].append({'kind': 'unsupported_evidence_date_range', 'text': match[0],
                'start': match.start(), 'end': match.end(), 'reason': reason,
                'repair_guidance': '核对实际引用原文的完整起止年月；可保留原文月份写法，或另写有真实引用支持的日期句，不能借用别处数字。'})
            continue
        record, anchor, original, _, original_language = matches[0]
        source_anchor = {'page': anchor['page'], 'start': anchor['start'] + original.start(),
                         'end': anchor['start'] + original.end()}
        result['annotations'].append({'kind': 'evidence_date_range', 'text': match[0],
            'start': match.start(), 'end': match.end(), 'evidence_id': record['id'],
            'source_id': record['source_id'], 'sha256': record['sha256'], 'parser_version': record['parser_version'],
            'source_anchor': source_anchor, 'source_quote': original[0],
            'quote_relative_start': original.start(), 'quote_relative_end': original.end(),
            'calendar_range': {'start': [value[0], value[1]], 'end': [value[2], value[3]]},
            'candidate_language': language, 'source_language': original_language,
            'scope': 'calendar_expression_equivalence_only', 'semantic_review_required': True})
    return result
