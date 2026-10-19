"""Detached privacy projection for diagnostics, never model inputs or business rows."""
from __future__ import annotations
import copy
import math
import re
from .candidate_audit import _clean

MAX_NODES = 100000
MAX_DEPTH = 40
MAX_TEXT_CHARACTERS = 4 * 1024 * 1024
_PRIVATE = re.compile(r'^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|password|passwd|secret|client[_-]?secret|cookie|authorization|private[_-]?key|endpoint|base[_-]?url|api[_-]?(?:base|url|endpoint)|model[_-]?endpoint)$', re.I)
_INLINE_ENDPOINT = re.compile(r'''(?i)(\b(?:endpoint|base[_-]?url|api[_-]?(?:base|url|endpoint)|model[_-]?endpoint)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;"'}]+)''')
_BARE_TOKEN = re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b')
_PEM = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)', re.S)
_MARK = '[私密诊断字段已隐去]'


def redact_diagnostics(value):
    """Keep ordinary JSON shape/verdicts/numbers/hashes; sanitize a fresh copy.

    Bounds apply to this diagnostics-only copy. An explicit top-level marker
    reports any omissions. No caller-owned value is modified and no secret is
    interpolated into errors/logs. Reapplication is idempotent.
    """
    if not isinstance(value, dict):
        raise ValueError('诊断投影必须是对象')
    state = {'redacted': False, 'truncated': False, 'original_lengths': {}}
    nodes = 0
    remaining = MAX_TEXT_CHARACTERS

    def text(v, path):
        nonlocal remaining
        # Once over budget, do not retain a partial secret prefix.
        if len(v) > remaining:
            state['truncated'] = True
            return '[审计文本预算不足，内容已省略]'
        remaining -= len(v)
        clean = _clean(v, max(4096, len(v) * 4), state, path)
        final = _INLINE_ENDPOINT.sub(lambda m: m[1] + _MARK, clean)
        final = _BARE_TOKEN.sub('[凭据已隐去]', final)
        final = _PEM.sub('[私钥材料已隐去]', final)
        if final != clean: state['redacted'] = True
        return final

    def walk(v, depth=0, path='$'):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            state['truncated'] = True
            return '[审计结构预算不足，内容已省略]'
        if v is None or type(v) in (bool, int): return v
        if type(v) is float:
            if math.isfinite(v): return v
            state['truncated'] = True
            return '[非有限诊断数值已省略]'
        if isinstance(v, str): return text(v, path)
        if isinstance(v, list):
            out = []
            for i, child in enumerate(v):
                if nodes >= MAX_NODES:
                    state['truncated'] = True; out.append('[其余审计项已省略]'); break
                out.append(walk(child, depth+1, f'{path}[{i}]'))
            return out
        if isinstance(v, dict):
            out = {}
            for key, child in v.items():
                if nodes >= MAX_NODES:
                    state['truncated'] = True; break
                if not isinstance(key, str): state['truncated'] = True; continue
                key_safe = text(key, path)
                if _PRIVATE.fullmatch(key):
                    out[key_safe] = None if child is None else _MARK
                    if child not in (None, _MARK): state['redacted'] = True
                else: out[key_safe] = walk(child, depth+1, path+'.'+key_safe)
            return out
        state['truncated'] = True
        return '[非JSON诊断值已省略]'

    result = walk(value)
    if state['redacted'] or state['truncated']:
        previous = result.get('_audit_redaction', {})
        result['_audit_redaction'] = {
            'version': 1, 'diagnostic_only': True,
            'redacted': state['redacted'] or isinstance(previous, dict) and previous.get('redacted') is True,
            'truncated': state['truncated'] or isinstance(previous, dict) and previous.get('truncated') is True,
            'limits': {'nodes': MAX_NODES, 'depth': MAX_DEPTH, 'text_characters': MAX_TEXT_CHARACTERS},
        }
    return result


def redact_sql_result_diagnostics(result):
    """Project only published SQL diagnostic fields after all model decisions.

    Business text, claims, rows, evidence, SQL and bound parameters are unchanged.
    No active query/check/adjudication object is mutated during model review.
    """
    out = copy.deepcopy(result)
    if isinstance(out.get('query_diagnostics'), dict):
        out['query_diagnostics'] = redact_diagnostics(out['query_diagnostics'])
    query = out.get('query')
    if isinstance(query, dict):
        for name in ('review_response', 'review_adjudication', 'effective_review_response', 'review_evidence'):
            if isinstance(query.get(name), dict): query[name] = redact_diagnostics(query[name])
        if isinstance(query.get('semantic_reviews'), list):
            query['semantic_reviews'] = redact_diagnostics({'items': query['semantic_reviews']})['items']
        if isinstance(query.get('plan'), dict) and isinstance(query['plan'].get('verification_reason'), str):
            query['plan']['verification_reason'] = redact_diagnostics({'reason': query['plan']['verification_reason']})['reason']
    return out
