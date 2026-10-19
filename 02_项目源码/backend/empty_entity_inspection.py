"""Bounded text-filter hints from actual catalog samples, never answer evidence.

The original statement is not rewritten or executed here. Positional/named
parameters and physical columns use the existing entity-check analysis. Each
predicate is inspected independently, including predicates under OR; no hint
claims that a sampled value satisfies the complete query or denotes the same
business entity as a substring.
"""
from __future__ import annotations

import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .entity_checks import _bound_tree, _filter_context, _resolve_column, _text_value
from .sql_engine import SQLExecutionError

MAX_FILTERS = 4
MAX_CANDIDATES = 4
MAX_VALUE_CHARS = 160
MAX_FEEDBACK_CHARS = 6000


def _tree(sql, parameters):
    try:
        return _bound_tree(sql, parameters)
    except (SQLExecutionError, ValueError, sqlglot.errors.SqlglotError, RecursionError):
        return None, {}


def _predicates(tree, bindings):
    for scope in traverse_scope(tree):
        for predicate in scope.find_all(exp.EQ, exp.In, exp.Like):
            filtered, reason = _filter_context(predicate, scope)
            if not filtered or reason:
                continue  # NOT, projections and non-filter expressions are not selections.
            if isinstance(predicate, exp.In):
                if predicate.args.get('query'):
                    continue
                column, operands, kind = predicate.this, predicate.expressions, 'in'
            else:
                column, operand = predicate.this, predicate.expression
                if isinstance(predicate, exp.EQ) and isinstance(_text_value(column, bindings), str):
                    column, operand = operand, column
                operands = [operand]
                kind = 'like' if isinstance(predicate, exp.Like) else 'eq'
            for operand in operands:
                value = _text_value(operand, bindings)
                if isinstance(value, str) and value:
                    yield scope, column, value, kind


def needs_text_inspection(sql, parameters):
    tree, bindings = _tree(sql, parameters)
    if tree is None:
        return False
    try:
        return next(_predicates(tree, bindings), None) is not None
    except (sqlglot.errors.SqlglotError, RecursionError):
        return False


def _appears(value, question):
    if len(value) < 2:
        return False
    left = r'(?<![A-Za-z0-9_])' if value[0].isascii() and value[0].isalnum() else ''
    right = r'(?![A-Za-z0-9_])' if value[-1].isascii() and value[-1].isalnum() else ''
    return bool(re.search(left + re.escape(value) + right, question))


def text_filter_feedback(source, sql, parameters, question, catalog):
    """Return finite original values and spelling relations, without choosing one."""
    result = {
        'status': 'sampled', 'scope': 'bounded_catalog_sample_not_answer_evidence',
        'source_id': source.get('id'), 'sha256': source.get('sha256'),
        'parser_version': source.get('parser_version'),
        'semantic_revision': source.get('semantic_revision') or source.get('metadata', {}).get('semantic_revision'),
        'metadata_untrusted': True, 'query_changed': False,
        'filters': [], 'truncated': bool(catalog.get('truncated')),
        'omitted_filters': 0,
        'absence_proven': False, 'entity_identity_proven': False,
        'guidance': '以下仅为真实列的有限样本，不是答案或同一实体证明。核对用户完整对象、日期和范围后另发明确SQL动作；不得自动用子串替换实体或直接拿样本算数。多个可能对象未获用户指定时应澄清；无候选或样本截断也不能证明全源不存在。',
    }
    tree, bindings = _tree(sql, parameters)
    if tree is None:
        result.update(status='unsupported', reason='SQL或参数无法安全映射，未推测筛选值')
        return result
    if catalog.get('inspection_unavailable'):
        result.update(status='unavailable', reason='真实取值样本未读取完成', truncated=True)
        return result
    available, entries = {}, {}
    for table in catalog.get('tables', []):
        name = table.get('name')
        columns = table.get('columns', [])
        if not isinstance(name, str) or len(name) > 256:
            continue
        available[name.casefold()] = {'name': name, 'column_map': {
            c['name'].casefold(): c['name'] for c in columns if isinstance(c.get('name'), str)
        }}
        entries[name] = table
    seen = set()
    try:
        predicates = _predicates(tree, bindings)
        for scope, column, value, kind in predicates:
            table, physical, error = _resolve_column(column, scope, available)
            key = (table, physical, value, kind)
            if key in seen:
                continue
            seen.add(key)
            if len(result['filters']) >= MAX_FILTERS:
                result['omitted_filters'] += 1
                result['truncated'] = True
                continue
            if len(value) > MAX_VALUE_CHARS or physical is not None and len(physical) > 256:
                result['omitted_filters'] += 1
                result['truncated'] = True
                continue  # Never publish a clipped spelling as an actual stored value.
            item = {'table': table, 'column': physical, 'requested_value': value,
                    'predicate': kind, 'predicate_scope': 'independent_not_complete_query',
                    'status': 'sampled', 'candidates': [], 'truncated': False}
            if error:
                item.update(status='unsupported', reason=error)
            else:
                details = entries[table].get('details', {})
                profile = next((p for p in details.get('column_profiles', []) if p.get('name') == physical), None)
                if profile is None:
                    item.update(status='unavailable', reason='当前实际字段没有完整样本投影', truncated=True)
                else:
                    item['truncated'] = bool(details.get('sample_truncated') or profile.get('values_truncated') or catalog.get('truncated'))
                    item['sampled_rows'] = profile.get('sampled_rows')
                    # Only simple edge-wildcard LIKE is analyzed. A wildcard is
                    # not a character to silently remove from an entity name.
                    needle = value.strip('%') if kind == 'like' else value
                    supported_like = kind != 'like' or not any(c in needle for c in ('%', '_', '\\'))
                    if not supported_like:
                        item.update(status='unsupported', reason='复杂LIKE模式未解释为实体名称')
                    else:
                        for observed in profile.get('distinct_values', []):
                            if not isinstance(observed, str) or not observed or len(observed) > MAX_VALUE_CHARS:
                                if isinstance(observed, str): item['truncated'] = True
                                continue
                            reasons = []
                            if kind in ('eq', 'in') and observed == value:
                                reasons.append('exact_filter_value_observed')
                            elif len(needle) >= 2 and needle in observed:
                                reasons.append('filter_text_is_substring_not_entity_equivalence')
                            if _appears(observed, question):
                                reasons.append('full_sample_text_occurs_in_user_question')
                            if not reasons:
                                continue
                            if len(item['candidates']) >= MAX_CANDIDATES:
                                item['truncated'] = True
                                continue
                            item['candidates'].append({'value': observed, 'relations': reasons,
                                                       'entity_equivalence_verified': False})
                        item['multiple_sample_candidates'] = len(item['candidates']) > 1
            if len(json.dumps({**result, 'filters': result['filters'] + [item]}, ensure_ascii=False)) > MAX_FEEDBACK_CHARS:
                result['omitted_filters'] += 1
                result['truncated'] = True
                continue
            result['filters'].append(item)
            result['truncated'] |= item['truncated']
    except (sqlglot.errors.SqlglotError, RecursionError):
        result.update(status='unsupported', reason='查询作用域无法安全分析，未补造筛选归属', truncated=True)
    return result
