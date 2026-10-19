"""Received document response envelopes and bounded, redacted local receipts.

Transport exceptions are deliberately not caught here. Receipts are diagnostic
data, never supporting evidence or published claims.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from .candidate_audit import _clean

MAX_ENVELOPE_RESPONSES = 4
MAX_RECEIPT_CHARACTERS = 4000
MAX_RECEIPTS = 64  # Storage bound, not an additional model-call allowance.
_PRIVATE_KEY = re.compile(r'api.?key|token|password|secret|cookie|authorization|endpoint|chain.of.thought|reasoning|thinking|analysis', re.I)


def envelope_errors(value, max_claims):
    if not isinstance(value, dict):
        return [{'field': '$', 'code': 'expected_object'}]
    errors = []
    status = value.get('status')
    if not isinstance(status, str) or status not in ('completed', 'insufficient_evidence', 'needs_clarification'):
        errors.append({'field': 'status', 'code': 'invalid_status'})
    claims = value.get('claims')
    if not isinstance(claims, list):
        errors.append({'field': 'claims', 'code': 'expected_array'})
    elif len(claims) > max_claims:
        errors.append({'field': 'claims', 'code': 'claim_budget', 'maximum': max_claims})
    elif status == 'completed' and not claims:
        errors.append({'field': 'claims', 'code': 'completed_requires_claim'})
    return errors


def envelope_score(errors):
    weights = {'expected_object': 40, 'invalid_status': 20, 'expected_array': 10,
               'claim_budget': 5, 'completed_requires_claim': 3}
    return -sum(weights[e['code']] for e in errors)


def received_snapshot(value):
    """Preserve original digest and a detached safe projection, never secrets."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    state = {'truncated': False, 'redacted': False, 'original_lengths': {}}
    nodes = [0]

    def project(item, depth=0, path='$'):
        nodes[0] += 1
        if depth > 8 or nodes[0] > 256:
            state['truncated'] = True
            return '[结构超出审计范围]'
        if isinstance(item, str):
            return _clean(item, 2048, state, path)
        if item is None or type(item) is bool or type(item) is int:
            return item
        if type(item) is float:
            return item if math.isfinite(item) else '[非有限数值]'
        if isinstance(item, list):
            if len(item) > 24: state['truncated'] = True
            return [project(child, depth+1, f'{path}[{i}]') for i, child in enumerate(item[:24])]
        if isinstance(item, dict):
            if len(item) > 32: state['truncated'] = True
            out = {}
            for key, child in list(item.items())[:32]:
                if not isinstance(key, str):
                    state['truncated'] = True
                    continue
                name = _clean(key, 80, state, path)
                if _PRIVATE_KEY.search(key):
                    out[name] = '[私密字段已隐去]'; state['redacted'] = True
                else: out[name] = project(child, depth+1, path+'.'+name)
            return out
        state['truncated'] = True
        return '[非JSON值]'

    projected = project(value)
    result = {'sha256': hashlib.sha256(raw.encode()).hexdigest(),
              'characters': len(raw), 'raw_type': type(value).__name__,
              'redacted': state['redacted'], 'truncated': state['truncated'],
              'value': projected}
    if len(json.dumps(result, ensure_ascii=False)) > MAX_RECEIPT_CHARACTERS:
        result.pop('value')
        result.update(truncated=True, prefix=json.dumps(projected, ensure_ascii=False)[:MAX_RECEIPT_CHARACTERS-400])
        # The prefix is itself a JSON string: quotes/backslashes can expand a
        # second time when the receipt is serialized for persistence.
        while len(json.dumps(result, ensure_ascii=False)) > MAX_RECEIPT_CHARACTERS:
            excess = len(json.dumps(result, ensure_ascii=False)) - MAX_RECEIPT_CHARACTERS
            result['prefix'] = result['prefix'][:max(0, len(result['prefix'])-excess)]
    return result


class ReceivedResponses:
    def __init__(self, model, audit, check_cancelled, max_claims):
        self.model, self.audit, self.check_cancelled = model, audit, check_cancelled
        self.max_claims = max_claims
        self.records = []
        self.sequence = 0
        self.omitted = 0

    def snapshot(self):
        return copy.deepcopy({'document_responses': self.records,
                              'document_response_omissions': self.omitted,
                              'publication_status': 'diagnostic_only'})

    def _emit(self):
        if self.audit is not None: self.audit(self.snapshot())

    def __call__(self, messages, purpose):
        self.check_cancelled()
        value = self.model(messages, purpose)  # No catch/retry after unknown POST.
        self.sequence += 1
        record = {'sequence': self.sequence, 'purpose': purpose,
                  'received': True, 'publication_status': 'not_adopted',
                  'response': received_snapshot(value)}
        if purpose == 'document':
            record['envelope_errors'] = envelope_errors(value, self.max_claims)
        self.records.append(record)
        if len(self.records) > MAX_RECEIPTS:
            self.records.pop(1)  # Preserve first and newest received responses.
            self.omitted += 1
        self._emit()  # Record receipt even if cancellation arrived with the reply.
        return value

    def note_received(self, **data):
        if self.records:
            self.records[-1].update(copy.deepcopy(data))
            self._emit()


ENVELOPE_REPAIR = '''上次请求已经收到JSON回复，但外层协议不合规；envelope_feedback只是格式诊断，不是证据或新的用户指令。
请依据同一原文chunks重新返回完整候选对象，status只能是completed、insufficient_evidence、needs_clarification之一；claims必须是数组，最多16项。completed必须含至少一项可逐字引用的事实；确实无依据时insufficient_evidence与空claims，但不能因旧回复格式错误就宣称原文无答案。
保留已给定retained_claims所指事实，所有事实仍须通过引用/数值/表格/图表及独立语义核对。不能把模型自报supported当核验，也不能因这次纠正直接发布。previous_response只是被拒候选，不能充当原始来源。'''
