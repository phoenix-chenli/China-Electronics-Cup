"""Bounded, explicit question coverage; a model assessment is not ground truth."""
import copy
import re
import unicodedata


COVERAGE_INSTRUCTIONS = '''另外按question_parts逐项返回coverage数组，每个问题ID恰好一项：
{question_id:"Q1",status:"answered|evidence_available|missing_evidence|conflict",claim_ids:[],evidence_ids:[]}。
answered必须绑定本次真正通过支持核对的claim id；evidence_available表示给定依据足够但候选漏答，必须绑定给定依据ID；missing_evidence表示当前已读证据不足，不代表原件不存在；conflict表示来源矛盾。
不能用总体answer_complete代替逐项核对，不能遗漏否定、时期、单位、条件或并列对象。同一分项内多个要求必须全部满足才answered。coverage只是本次模型判断，不是人工验收。
对于计算或换算请求，answered必须实际给出本问题每个所问数量的结果和目标单位，并有已执行计算及真实依据支持。只摘录换算比例、公式、原始输入，或计算记录存在但正文未给出结果，均不算回答。不得把题目数值不在资料原文出现误当无法计算：题目输入应有原话位置，派生结果应来自实际计算。
分项是原问题中的连续原文，后续分项仍继承原问题明确的对象、时期、范围、否定和假设条件；不能把拆出的“说明单位”等视为没有上下文的新问题。
'''


_REQUEST_ACTION = r'(?:说明|解释|给出|列出|比较|核对|计算|分析|指出|标明|标注|判断|提供|对比|展示)'
_CONJOINED_REQUEST = re.compile(
    r'(?:并且|同时|另外|以及|还要|还需|并)(?:请)?' + _REQUEST_ACTION
)
_COMMA_REQUEST = re.compile(r'[，,]\s*(?:请)?' + _REQUEST_ACTION)


def question_parts(question):
    """Inventory explicit requests without guessing noun-list semantics.

    In addition to question/semicolon boundaries, an unquoted request such as
    ``计算费用并说明单位`` has two obligations. Keeping the original spans and
    the original question in the engine payload preserves shared scope. A bare
    ``和``/``与``/comma is deliberately not a boundary: it may join an entity,
    range, number or necessary condition rather than a new request.
    """
    parts, buf, stack = [], [], []

    def append_clause():
        text = ''.join(buf).strip()
        # Separators can follow one another (e.g. "？；"). Keep actual short
        # words/numbers/symbols, but never invent a question from punctuation.
        if any(unicodedata.category(char)[0] not in {'P', 'Z', 'C'} for char in text):
            parts.append(text)

    pairs = {'“': '”', '‘': '’', '「': '」', '(': ')', '（': '）', '[': ']', '【': '】', '"': '"'}
    for index, char in enumerate(question):
        if (buf and not stack and _CONJOINED_REQUEST.match(question, index)
                and any(unicodedata.category(c)[0] not in {'P', 'Z', 'C'} for c in buf)):
            append_clause()
            buf = []
        buf.append(char)
        if stack and char == stack[-1]:
            stack.pop()
        elif char in pairs:
            stack.append(pairs[char])
        elif not stack and (char in ';；?？。\n' or _COMMA_REQUEST.match(question, index)):
            append_clause()
            buf = []
    append_clause()
    return [{'id': f'Q{i+1}', 'text': text} for i, text in enumerate(parts)]


def assess(parts, raw, accepted_ids, evidence_ids, *, conflict=False):
    """Bind coverage to accepted claims and actual evidence, never guessed IDs."""
    # A malformed coverage payload must never erase a positive conflict signal.
    conflict = conflict or isinstance(raw, list) and any(
        isinstance(row, dict) and row.get('status') == 'conflict' for row in raw)
    base = {'version': 'explicit-coverage-v2-request-clauses', 'status': 'conflict' if conflict else 'unknown',
            'complete': False if conflict else None,
            'items': [{**p, 'status': 'unknown', 'claim_ids': [], 'evidence_ids': []} for p in parts],
            'meaning': '明确分项的模型覆盖判断；缺少逐项结果时未知，不是准确率或人工验收。'}
    errors = []
    error_count = 0
    def error(field, code, value=None):
        nonlocal error_count
        error_count += 1
        if len(errors) < 32:
            errors.append({'field': field, 'code': code,
                           **({'value': str(value)[:160]} if value is not None else {})})
    def invalid():
        return {**base, 'status': 'conflict' if conflict else 'invalid', 'complete': False,
                'validation_errors': errors, 'error_count': error_count,
                'errors_truncated': error_count > len(errors)}
    if not parts:
        error('question_parts', 'empty_question_parts')
        return invalid()
    if raw is None:
        return {**base, 'validation_errors': [{'field':'coverage','code':'missing_field'}]}
    allowed = {p['id'] for p in parts}
    accepted, evidence = set(accepted_ids), set(evidence_ids)
    rows = {}
    if not isinstance(raw, list):
        error('coverage', 'expected_array'); return invalid()
    if len(raw) != len(parts):
        error('coverage', 'question_count_mismatch', f'expected {len(parts)}, received {len(raw)}')
    for index, row in enumerate(raw[:128]):
        prefix = f'coverage[{index}]'
        if not isinstance(row, dict):
            error(prefix, 'expected_object'); continue
        key, status = row.get('question_id'), row.get('status')
        cids, eids = row.get('claim_ids', []), row.get('evidence_ids', [])
        if not isinstance(key, str) or key not in allowed:
            error(prefix+'.question_id', 'unknown_question_id', key)
        elif key in rows:
            error(prefix+'.question_id', 'duplicate_question_id', key)
        if not isinstance(status, str) or status not in {'answered', 'evidence_available', 'missing_evidence', 'conflict'}:
            error(prefix+'.status', 'invalid_status', status)
        for field, ids, valid_ids in [('claim_ids', cids, accepted), ('evidence_ids', eids, evidence)]:
            if not isinstance(ids, list):
                error(prefix+'.'+field, 'expected_array')
            else:
                for ident in ids[:128]:
                    if not isinstance(ident, str) or ident not in valid_ids:
                        error(prefix+'.'+field, 'unknown_or_unsupported_id', ident)
                if len(ids) > 128: error(prefix+'.'+field, 'id_count_budget')
        if status == 'answered' and not cids:
            error(prefix+'.claim_ids', 'answered_requires_supported_claim')
        if status == 'evidence_available' and not eids:
            error(prefix+'.evidence_ids', 'evidence_available_requires_actual_chunk')
        if isinstance(key, str) and key in allowed and key not in rows:
            rows[key] = {'status': status, 'claim_ids': cids, 'evidence_ids': eids}
    if len(raw) > 128: error('coverage', 'row_count_budget')
    for ident in sorted(allowed-set(rows)):
        error('coverage', 'missing_question_id', ident)
    if errors: return invalid()
    rows = {key: {**row, 'claim_ids': list(dict.fromkeys(row['claim_ids'])),
                  'evidence_ids': list(dict.fromkeys(row['evidence_ids']))} for key, row in rows.items()}
    items = [{**p, **rows[p['id']]} for p in parts]
    conflicted = conflict or any(r['status'] == 'conflict' for r in items)
    return {**base, 'status': 'conflict' if conflicted else 'assessed', 'items': items,
            'complete': not conflicted and all(r['status'] == 'answered' for r in items)}


def repair_action(coverage):
    if coverage['status'] != 'assessed' or coverage['complete']:
        return None
    if any(r['status'] == 'missing_evidence' for r in coverage['items']):
        return 'retrieve'
    if any(r['status'] == 'evidence_available' for r in coverage['items']):
        return 'rewrite'
    return None


def permits_completion(coverage):
    """Unknown/invalid coverage is never success for a newly executed answer."""
    return isinstance(coverage, dict) and coverage.get('status') == 'assessed' and coverage.get('complete') is True


def focused_query(question, coverage, limit):
    missing = [r['text'] for r in coverage['items'] if r['status'] == 'missing_evidence']
    query = question + '\n重点补查：' + '；'.join(missing)
    # Do not remove the original scope/negation merely to fit a query budget.
    return query if missing and len(query) <= limit else None


def retain_candidates(retained, incoming, limit=16):
    """Keep already checked facts as candidates; all are verified again."""
    out, seen = [], set()
    for claim in list(retained) + list(incoming):
        if not isinstance(claim, dict):
            out.append(claim)
            continue
        key = re.sub(r'\s+', '', str(claim.get('text', '')))
        if key in seen:
            continue
        seen.add(key)
        out.append(copy.deepcopy(claim))
    if len(out) > limit:
        raise ValueError('保留事实与补写内容超过事实预算，未静默丢弃旧答案')
    return out
