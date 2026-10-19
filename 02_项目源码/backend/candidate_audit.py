"""Bounded, redacted snapshots of rejected proposals, never supporting evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

MAX_ATTEMPTS = 3
MAX_AUDIT_CHARACTERS = 12000
MAX_ATTEMPT_CHARACTERS = 3980
MAX_FEEDBACK_CHARACTERS = 4000


def evidence_manifest(evidence):
    """Keep exact source locations across a later failed model request.

    This is a diagnostic index, not published or verified supporting evidence.
    It stores hashes of content, never a second copy of text, SQL or images.
    """
    entries = []
    for key, item in list(evidence.items())[:512]:
        if not isinstance(item, dict):
            continue
        entry = {name: item.get(name) for name in
                 ('id', 'kind', 'source_id', 'sha256', 'parser_version', 'page', 'start', 'end')}
        entry['registry_id'] = key
        quote = item.get('quote')
        if isinstance(quote, str):
            entry['quote_characters'] = len(quote)
            entry['quote_sha256'] = hashlib.sha256(quote.encode('utf-8')).hexdigest()
        query = item.get('query')
        if isinstance(query, dict):
            encoded = json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            entry['query_sha256'] = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        entries.append(entry)
    return {'audit_only': True, 'publication_status': 'not_published_or_verified',
            'registry_count': len(evidence), 'entries': entries,
            'omitted_entries': len(evidence) - len(entries)}
_LOCAL_PATH = re.compile(r"(?<![\w:])(?:file://[^\s\"'<>]+|(?:/Users/|/home/|/private/|/tmp/|/var/|/Volumes/|/etc/|/opt/|/usr/|/root/|/Applications/|/Library/)[^\s\"'<>]+|[A-Za-z]:[\\/][^\s\"'<>]+)")
_CREDENTIAL = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie|authorization)[\"']?\s*[:=]\s*)(?:\"(?:\\.|[^\"\\])*(?:\"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;\"'}]+)")
_BEARER = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_URL_AUTH = re.compile(r"(https?://)[^\s/@]+@", re.I)
_DATA_URL = re.compile(r"data:[^\s\"'<>]+", re.I)
_BINDING_FIELDS = ('table_name', 'cell_id', 'value', 'statement', 'column_label', 'row_label',
                   'unit', 'entity_cell_id', 'entity_value', 'kind')
_CHART_BINDING_FIELDS = ('kind', 'statement', 'observation_type', 'association', 'category', 'value',
                         'unit', 'series', 'title', 'period', 'category_observation_id',
                         'value_observation_id', 'unit_observation_id', 'series_observation_id',
                         'title_observation_id', 'period_observation_id')
_VISUAL_BINDING_FIELDS = ('item_id', 'statement')
_QUERY_BINDING_FIELDS = ('context_id', 'cell_id', 'row_index', 'column_index', 'column_label',
                         'value', 'statement', 'unit', 'unit_column')
_BINDING_PROJECTIONS = (
    ('table_bindings', _BINDING_FIELDS, 'omitted_bindings', ()),
    ('chart_bindings', _CHART_BINDING_FIELDS, 'omitted_chart_bindings', ('observation_ids',)),
    ('visual_bindings', _VISUAL_BINDING_FIELDS, 'omitted_visual_bindings', ()),
    ('query_bindings', _QUERY_BINDING_FIELDS, 'omitted_query_bindings', ('entity_cells',)),
)
_OPERATION_CHECKS = ('intent_supported', 'parameter_roles_supported', 'scope_supported', 'pricing_supported')
_SCENARIO_CHECKS = ('intent_supported','parameter_provenance_supported','scope_supported','hypothetical_label_supported')
_RELATIONS = (('table_relations', ('table_bindings',)),
              ('chart_relations', ('chart_bindings', 'visual_bindings')),
              ('query_relations', ('query_bindings', 'query_fact')))
_LONG_FIELDS = {'text', 'statement', 'column_label', 'row_label', 'entity_value', 'value', 'reason', 'message',
                'category', 'series', 'title', 'period'}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False))


def _clean(value, limit, state, path):
    if not isinstance(value, str):
        return None
    # Scan beyond the retained prefix so a token crossing the cut is redacted
    # before truncation. Never traverse arbitrary model-returned nested values.
    raw = value[:max(4096, limit * 2)]
    clean = _LOCAL_PATH.sub('[本机路径已隐去]', raw)
    clean = _BEARER.sub('Bearer [凭据已隐去]', clean)
    clean = _CREDENTIAL.sub(r'\1[凭据已隐去]', clean)
    clean = _URL_AUTH.sub(r'\1[凭据已隐去]@', clean)
    clean = _DATA_URL.sub('[嵌入数据已隐去]', clean)
    if clean != raw:
        state['redacted'] = True
    if len(value) > len(raw) or len(clean) > limit:
        state['truncated'] = True
        if len(state['original_lengths']) < 24:
            state['original_lengths'][path] = len(value)
    return clean[:limit]


def _references(value, registry, state, path):
    known, unknown = [], []
    if not isinstance(value, list):
        return known, unknown
    if len(value) > 16:
        state['truncated'] = True
        state['original_lengths'][path] = len(value)
    for index, identifier in enumerate(value[:16]):
        if not isinstance(identifier, str):
            continue
        if identifier in registry:
            if identifier not in known:
                known.append(identifier)
        else:
            item = _clean(identifier, 80, state, f'{path}[{index}]')
            if item not in unknown:
                unknown.append(item)
    return known, unknown


def _scalar(value, limit, state, path, item, key):
    """Never stringify nested input or coerce an invalid value into a real one."""
    if isinstance(value, str):
        return _clean(value, limit, state, path)
    if value is None or type(value) is bool:
        return value
    if type(value) is int and value.bit_length() <= 1024:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    item.setdefault('omitted_fields', []).append(key)
    state['truncated'] = True
    return None


def _id_array(value, key, item, state, path):
    item[key] = [_clean(identifier, 80, state, f'{path}[{position}]')
                 for position, identifier in enumerate(value[:16])
                 if isinstance(identifier, str)] if isinstance(value, list) else []
    if not isinstance(value, list) or any(not isinstance(identifier, str) for identifier in value[:16]):
        item['invalid_' + key] = True
    if isinstance(value, list) and len(value) > 16:
        state['truncated'] = True
        item['omitted_' + key] = len(value) - 16


def _operation_ids(claim, calculations):
    """Use only the supplied calculation registry, with a bounded dependency walk."""
    ids = claim.get('calculation_ids', []) if isinstance(claim, dict) else []
    pending = [v for v in ids[:16] if isinstance(v, str)] if isinstance(ids, list) else []
    seen = set()
    while pending and len(seen) < 64:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        calculation = calculations.get(identifier)
        if not isinstance(calculation, dict):
            continue
        parameters = calculation.get('parameters')
        for parameter in parameters[:16] if isinstance(parameters, list) else []:
            binding = parameter.get('binding') if isinstance(parameter, dict) else None
            child = binding.get('calculation_id') if isinstance(binding, dict) else None
            if isinstance(child, str):
                pending.append(child)
    return seen


def review_feedback(review):
    """Keep verdicts, not arbitrary review prose or untrusted extra fields."""
    if not isinstance(review, dict):
        return {}
    result = {key: review.get(key) if isinstance(review.get(key), bool) else None
              for key in ('supported', 'answer_complete', 'conflict', 'visual_supported')}
    for key in ('claims', 'table_relations', 'chart_relations', 'query_relations', 'operation_relations', 'scenario_relations'):
        rows = review.get(key)
        fields = _OPERATION_CHECKS if key == 'operation_relations' else _SCENARIO_CHECKS if key == 'scenario_relations' else ('supported',)
        pattern = r'K[1-9][0-9]{0,3}' if key in ('operation_relations','scenario_relations') else r'C(?:[1-9]|1[0-6])'
        result[key] = [{'id': row['id'], **{field: row.get(field) if type(row.get(field)) is bool else None for field in fields}}
                       for row in rows[:16] if isinstance(row, dict) and isinstance(row.get('id'), str)
                       and re.fullmatch(pattern, row['id'])] if isinstance(rows, list) else []
        result[f'{key}_present'] = isinstance(rows, list)
        if isinstance(rows, list) and len(rows) > 16:
            result['truncated'] = True
            result['omitted_' + key] = len(rows) - 16
    coverage = review.get('coverage')
    result['coverage_present'] = isinstance(coverage, list)
    result['coverage_statuses'] = [row['status'] for row in coverage[:16] if isinstance(row, dict)
                                   and row.get('status') in ('answered', 'evidence_available', 'missing_evidence', 'conflict')] if isinstance(coverage, list) else []
    return result


def _fit(record, limit):
    """Trim structured fields with explicit omission markers; never cut JSON."""
    def trim_binding(minimum):
        # Keep the failing claim and its first binding before unrelated repeats.
        # Only the projected audit is mutated, never model data or real evidence.
        for claim in reversed(record.get('claims', [])):
            for key, _, count, _ in reversed(_BINDING_PROJECTIONS):
                bindings = claim.get(key)
                if isinstance(bindings, list) and len(bindings) > minimum:
                    bindings.pop()
                    claim[count] = claim.get(count, 0) + 1
                    record['truncated'] = True
                    return True
        return False
    while _size(record) > limit and trim_binding(1):
        pass
    while _size(record) > limit and len(record.get('claims', [])) > 1:
        record['claims'].pop()
        record['omitted_claims'] += 1
        record['truncated'] = True
    for maximum in (900, 450, 220, 100, 40):
        if _size(record) <= limit:
            return record
        record['truncated'] = True
        def shorten(value, path=''):
            if isinstance(value, dict):
                for key, item in value.items():
                    child = f'{path}.{key}'
                    if key in _LONG_FIELDS and isinstance(item, str) and len(item) > maximum:
                        if len(record['original_lengths']) < 24:
                            record['original_lengths'].setdefault(child, len(item))
                        value[key] = item[:maximum]
                    elif key != 'original_lengths':
                        shorten(item, child)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    shorten(item, f'{path}[{index}]')
        shorten(record)
    while _size(record) > limit and trim_binding(0):
        pass
    if _size(record) > limit:
        # A tiny remaining request budget cannot carry useful candidate text.
        # Its audit remains intact outside the prompt; report only its identity.
        return {key: record[key] for key in ('attempt_id', 'round', 'stage', 'publication_status')} | {
            'truncated': True, 'omitted_claims': record['original_claim_count'],
            'original_claim_count': record['original_claim_count'], 'claims': [], 'original_lengths': {},
            'notice': '候选反馈因预算省略；原审计仍保留，不能作为支持依据。'}
    return record


def capture_candidate(response, evidence, calculations, *, round_number, stage, reason, details=None, review=None):
    """Project only the rejected completed candidate; prefer its failing claim."""
    details = details if isinstance(details, dict) else {}
    candidates = response.get('claims') if isinstance(response, dict) else None
    candidates = candidates if isinstance(candidates, list) else []
    failed = details.get('claim_index')
    if not isinstance(failed, int) or isinstance(failed, bool) or not 1 <= failed <= len(candidates):
        failed = 1
        verdicts = review if isinstance(review, dict) else {}
        rejected = []
        for key, _ in _RELATIONS:
            rows = verdicts.get(key)
            for row in rows[:16] if isinstance(rows, list) else []:
                if (isinstance(row, dict) and row.get('supported') is not True
                        and re.fullmatch(r'C(?:[1-9]|1[0-6])', str(row.get('id', '')))
                        and int(row['id'][1:]) <= len(candidates)):
                    rejected.append(int(row['id'][1:]))
        operation_rows = verdicts.get('operation_relations')
        rejected_operations = {row['id'] for row in operation_rows[:16]
                               if isinstance(row, dict) and isinstance(row.get('id'), str)
                               and re.fullmatch(r'K[1-9][0-9]{0,3}', row['id'])
                               and not all(row.get(field) is True for field in _OPERATION_CHECKS)} if isinstance(operation_rows, list) else set()
        rejected.extend(index + 1 for index, claim in enumerate(candidates[:16])
                        if _operation_ids(claim, calculations) & rejected_operations)
        if rejected:
            failed = rejected[0]
        elif review is not None:
            missing = []
            for key, bindings in _RELATIONS:
                rows = verdicts.get(key)
                supported = {row['id'] for row in rows[:16] if isinstance(row, dict)
                             and isinstance(row.get('id'), str) and row.get('supported') is True} if isinstance(rows, list) else set()
                missing.extend(index + 1 for index, claim in enumerate(candidates[:16])
                               if isinstance(claim, dict) and any(claim.get(field) for field in bindings)
                               and f'C{index + 1}' not in supported)
            reviewed_operations = {row['id'] for row in operation_rows[:16]
                                   if isinstance(row, dict) and isinstance(row.get('id'), str)
                                   and all(row.get(field) is True for field in _OPERATION_CHECKS)} if isinstance(operation_rows, list) else set()
            required_operations = {identifier for identifier, value in calculations.items()
                                   if isinstance(value, dict) and value.get('rule_origin') == 'user_operation'}
            missing.extend(index + 1 for index, claim in enumerate(candidates[:16])
                           if (_operation_ids(claim, calculations) & required_operations) - reviewed_operations)
            failed = missing[0] if missing else 1
    order = ([failed - 1] if 1 <= failed <= len(candidates) else [])
    order += [index for index in range(min(len(candidates), 16)) if index not in order]
    record = {'projection_version': 2, 'attempt_id': f'A{round_number}', 'round': round_number, 'stage': stage,
              'scope': 'candidate_attempt', 'validation_status': 'rejected', 'publication_status': 'not_adopted',
              'notice': '未采纳的完成态候选，仅供诊断；不是原文、支持依据或已发布结论。',
              'claim_index': failed, 'original_claim_count': len(candidates), 'claims': [],
              'omitted_claims': max(0, len(candidates) - min(3, len(order))),
              'truncated': len(candidates) > 3, 'redacted': False, 'original_lengths': {}}
    record['reason'] = _clean(reason, 500, record, 'reason')
    if isinstance(details.get('kind'), str):
        record['error_kind'] = _clean(details['kind'], 80, record, 'error_kind')
    if review is not None:
        record['review'] = review_feedback(review)
    for index in order[:3]:
        raw = candidates[index]
        claim = {'candidate_index': index + 1}
        if not isinstance(raw, dict):
            claim['invalid_shape'] = True
            record['claims'].append(claim)
            continue
        claim['text'] = _clean(raw.get('text'), 1800, record, f'claims[{index}].text')
        if 'query_fact' in raw:
            selection = raw['query_fact']
            if not isinstance(selection, dict):
                claim['invalid_query_fact'] = True
            else:
                projected = {}
                fields = ('evidence_id', 'context_id', 'cell_id', 'unit_cell_id')
                for key in fields:
                    if key in selection:
                        projected[key] = _clean(selection[key], 160, record, f'claims[{index}].query_fact.{key}')
                        if not isinstance(selection[key], str):
                            projected['invalid_' + key] = True
                if 'entity_cells' in selection:
                    _id_array(selection['entity_cells'], 'entity_cells', projected, record,
                              f'claims[{index}].query_fact.entity_cells')
                if 'unit_from_column' in selection:
                    projected['unit_from_column'] = selection['unit_from_column'] if type(selection['unit_from_column']) is bool else None
                    if type(selection['unit_from_column']) is not bool:
                        projected['invalid_unit_from_column'] = True
                omitted = len(set(selection) - {*fields, 'entity_cells', 'unit_from_column'})
                if omitted:
                    projected['omitted_fields_count'] = omitted
                    record['truncated'] = True
                claim['query_fact'] = projected
        for key, registry in (('evidence_ids', evidence), ('calculation_ids', calculations)):
            claim[key], unknown = _references(raw.get(key), registry, record, f'claims[{index}].{key}')
            if unknown:
                claim['unresolved_' + key] = unknown
        for binding_key, fields, omitted_key, array_fields in _BINDING_PROJECTIONS:
            bindings = raw.get(binding_key)
            if not isinstance(bindings, list):
                if binding_key in raw:
                    claim['invalid_' + binding_key] = True
                continue
            claim[binding_key] = []
            claim[omitted_key] = max(0, len(bindings) - 4)
            record['truncated'] |= len(bindings) > 4
            for number, binding in enumerate(bindings[:4]):
                if not isinstance(binding, dict):
                    claim[binding_key].append({'binding_index': number + 1, 'invalid_shape': True})
                    continue
                item = {'binding_index': number + 1}
                path = f'claims[{index}].{binding_key}[{number}]'
                for key in fields:
                    if key in binding:
                        limit = 1800 if key == 'statement' else 80 if key.endswith('_observation_id') else 600
                        if key in ('value', 'entity_value', 'row_index', 'column_index'):
                            item[key] = _scalar(binding[key], limit, record, f'{path}.{key}', item, key)
                        else:
                            item[key] = _clean(binding[key], limit, record, f'{path}.{key}')
                for key in array_fields:
                    if key in binding:
                        _id_array(binding[key], key, item, record, f'{path}.{key}')
                eid = binding.get('evidence_id')
                if isinstance(eid, str) and eid in evidence:
                    item['evidence_id'] = eid
                    if eid not in claim['evidence_ids']:
                        item['outside_claim_evidence'] = True
                elif eid is not None:
                    item['unresolved_evidence_id'] = _clean(eid, 80, record, f'{path}.evidence_id')
                claim[binding_key].append(item)
        record['claims'].append(claim)
    return _fit(record, MAX_ATTEMPT_CHARACTERS)


def append_attempt(attempts, record):
    result = [*attempts[-(MAX_ATTEMPTS - 1):], record]
    while _size(result) > MAX_AUDIT_CHARACTERS:
        result.pop(0)
    return result


def with_latest_feedback(payload, attempts, max_context):
    """Use only spare payload space; never evict evidence or retained facts."""
    if not attempts:
        return payload
    key = 'last_candidate_feedback'
    available = max_context - _size(payload) - len(json.dumps(key)) - 4
    if available < 260:
        return payload
    feedback = _fit(copy.deepcopy(attempts[-1]), min(MAX_FEEDBACK_CHARACTERS, available))
    result = {**payload, key: feedback}
    return result if _size(feedback) <= MAX_FEEDBACK_CHARACTERS and _size(result) <= max_context else payload
