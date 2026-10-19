"""Bounded runtime witnesses for SQL reviews, never a business-answer oracle."""
from __future__ import annotations

import copy
import contextlib
import json
import math
import re
import sqlite3

import sqlglot
from sqlglot import exp

from .sql_answer_contract import REVIEW_FIELDS
from .sql_contract_checks import fingerprint

MAX_CHARS = 12000
MAX_PROBES = 3
MAX_VALUES = 12
MAX_ISSUES = 12

REVIEW_INSTRUCTIONS = '''review_evidence 只给出原用户逐字片段和受限SQLite运行事实，不替用户决定业务口径。
原值NULL、SUM对已知值求和、全NULL聚合、COALESCE补零是不同行为；不能从SUM忽略NULL推断每个未知原值为0，也不能无用户依据要求任一NULL令整组NULL。
LIKE及日期函数按runtime事实核对；真实YYYY-MM文本不一定是SQLite日期函数接受的完整日期。样本是具体反例，不证明全库范围。
单位与金额转换必须有原用户/已确认语义依据；“均分”等可能歧义片段不能直接作为除100的授权。列名增加正确单位后缀不等于增加输出列，除非用户明确指定精确别名。显示小数位不要求特定SQL函数。
业务指标按实际聚合表达式、字段、筛选、单位与用户需求判断；自然语言标签与字段运算的等义表述不要求字面一致。解释文字需改进不等于SQL错误：SQL正确时保留supported=true，通过interpretation_supported=false和corrected_interpretation单独修正。
NULL不补零是计算规则；仅未把这句话写入列别名/输出契约不能作为output_contract否决，除非用户明确要求该说明为输出内容。不得因此隐去真实缺失值或改聚合口径。
否决时另给issues（最多12项，每项独立唯一id I1/I2...）：
{id,dimension:"source_basis|population_grain|quantifier_scope|missing_values|output_contract",kind:"runtime_fact|user_requirement|business_interpretation",reason:string}。
runtime_fact 必须给 fact_id 及 asserted_value（你认为本次实际求值是什么，复制其JSON类型；不是你希望的值）。fact_id只用review_evidence.facts已有ID。
user_requirement 必须给 requirement:"currency_conversion|null_propagation|exact_column_alias",user_quote:原用户逐字依据；currency_conversion另给target_unit。
业务定义不确定或其他语义问题用business_interpretation，不能编造runtime值。issues是诊断，不能代替supported、完整scope_review及table_relations。'''

ADJUDICATION_INSTRUCTIONS = '''这是一次受限的独立裁决，不是要求把失败改为成功。
同一SQL、参数、来源和原用户问题保持不变；initial_review是可能出错的已收到意见，不是权威。
逐项对照review_evidence与challenges，维持真实语义缺陷；不得为通过核对增加单位转换或NULL业务规则。
返回JSON {decision:"uphold|retract",resolved_issue_ids:[I编号],reason:string,review:{完整独立复核对象}}。
仅在原否决的每项issue均得到解释且同一候选完整正确时retract；必须重新给review.supported/interpretation_supported/五项scope_review及所有适用table_relations。机械门禁不会被裁决覆盖。无新依据维持原否决。
不得返回或执行新的SQL、探针、工具请求；原SQL如确错由正常规划链修复。'''


def _size(value):
    return len(json.dumps(value, ensure_ascii=False))


def _scalar(value):
    return (value is None or type(value) is bool
            or type(value) is int and -(2**63) <= value <= 2**63 - 1
            or type(value) is float and math.isfinite(value)
            or isinstance(value, str) and len(value) <= 256)


def _originals(question, history):
    return [question] + [h['original_question'] for h in history if isinstance(h, dict) and isinstance(h.get('original_question'), str)]


def build_review_evidence(source, question, history, sql, parameters, catalog, execute_readonly, cancelled=lambda: False):
    out = {'version': 'sql-review-runtime-1', 'sql_fingerprint': fingerprint(sql, parameters),
           'source_sha256': source.get('sha256'), 'user_contract': {'literal_fragments': [], 'interpretation': 'not_inferred'},
           'facts': [], 'probes': [], 'probe_attempts': 0, 'unresolved': [], 'truncated': False,
           'boundary': 'Runtime rules and bounded source witnesses only; no independent business definition or full-table proof.'}

    def add(key, value):
        if _size(out) + _size(value) + 4 > MAX_CHARS:
            out['truncated'] = True
            return False
        out[key].append(value)
        return True

    originals = _originals(question, history)
    for index, text in [(0, question), *list(enumerate(originals))[1:][-8:]]:
        for match in list(re.finditer(r'(?:单位\s*[：:]?\s*(?:为|是)?\s*[^，。；;\n]{1,16}|[（(]均[^）)]{1,16}[）)]|保留[^，。；;\n]{1,12}小数)', text))[:8]:
            fragment = {'original_question_index': index, 'quote': match.group(0), 'span': list(match.span())}
            if _size(out) + _size(fragment) + 4 <= MAX_CHARS:
                out['user_contract']['literal_fragments'].append(fragment)
            else: out['truncated'] = True
    # Actual SQLite scalar rules are source-independent, not source values.
    with contextlib.closing(sqlite3.connect(':memory:')) as connection:
        mixed = connection.execute('SELECT SUM(x) FROM (SELECT NULL x UNION ALL SELECT 2)').fetchone()[0]
        empty = connection.execute('SELECT SUM(x) FROM (SELECT NULL x)').fetchone()[0]
    add('facts', {'id': 'SQL_SUM_MIXED_NULL', 'kind': 'sqlite_scalar_rule', 'expression': 'SUM(NULL, 2)', 'value': mixed})
    add('facts', {'id': 'SQL_SUM_ALL_NULL', 'kind': 'sqlite_scalar_rule', 'expression': 'SUM(NULL)', 'value': empty})
    tree = sqlglot.parse_one(sql, read='sqlite')
    add('facts', {'id': 'SQL_HAS_COALESCE', 'kind': 'ast_presence', 'value': any(True for _ in tree.find_all(exp.Coalesce))})
    tables = {t['name']: {c['name'] for c in t.get('columns', [])} for t in catalog.get('tables', [])}
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    scopes = list(tree.find_all(exp.Select))
    if len(scopes) > 16: out['truncated'] = True
    seen = set()
    for scope in scopes[:16]:
        where = scope.args.get('where')
        if not where: continue
        direct = ([scope.args['from_'].this] if scope.args.get('from_') else []) + [j.this for j in scope.args.get('joins', [])]
        physical = {t.alias_or_name: t for t in direct if isinstance(t, exp.Table) and t.name in tables and t.name not in cte_names}
        predicates = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
        if len(predicates) > 16: out['truncated'] = True
        for predicate in predicates[:16]:
            if out['probe_attempts'] >= MAX_PROBES:
                out['truncated'] = True
                break
            if cancelled(): raise InterruptedError('已取消SQL复核运行事实探查')
            if not isinstance(predicate, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.Between)): continue
            if list(predicate.find_all(exp.Select)) or list(predicate.find_all(exp.Placeholder)):
                add('unresolved', 'parameterized_or_nested_predicate_not_rewritten')
                continue
            columns = list(predicate.find_all(exp.Column))
            names = {(c.table, c.name) for c in columns}
            if len(names) != 1: continue
            alias, column = next(iter(names))
            candidates = [t for key, t in physical.items() if (not alias or alias == key) and column in tables[t.name]]
            if len(candidates) != 1: continue
            table = candidates[0]
            # Only actual source expressions are copied, never reviewer SQL.
            # Limit to common scalar comparisons/date functions and no UDFs.
            allowed = {'TIME_TO_STR', 'STR_TO_TIME', 'TS_OR_DS_TO_DATE', 'TS_OR_DS_TO_TIMESTAMP', 'SUBSTRING', 'CAST', 'DATE', 'DATETIME', 'STRFTIME'}
            if any(f.sql_name() not in allowed for f in predicate.find_all(exp.Func)):
                add('unresolved', 'unsupported_scalar_function')
                continue
            expression = predicate.copy()
            for c in expression.find_all(exp.Column): c.set('table', None)
            expression_sql = expression.sql(dialect='sqlite')
            key = (table.name, expression_sql)
            if key in seen: continue
            seen.add(key)
            column_sql = exp.column(column, quoted=True).sql(dialect='sqlite')
            table_sql = exp.Table(this=exp.to_identifier(table.name, quoted=True)).sql(dialect='sqlite')
            probe_sql = f'SELECT DISTINCT {column_sql} AS source_value, ({expression_sql}) AS evaluated FROM {table_sql} ORDER BY {column_sql} LIMIT {MAX_VALUES}'
            if len(probe_sql) > 4000:
                add('unresolved', 'probe_expression_too_long'); continue
            out['probe_attempts'] += 1
            try: result = execute_readonly(source, probe_sql, [], cancelled)
            except InterruptedError: raise
            except ValueError as exc:
                add('unresolved', {'kind': 'probe_rejected', 'error_type': type(exc).__name__})
                continue
            pid = f'P{len(out["probes"]) + 1}'
            rows = result.get('rows', [])
            probe = {'id': pid, 'sql': probe_sql, 'parameters': [], 'table': table.name, 'column': column,
                     'predicate': expression_sql, 'sample_only': True, 'rows': [],
                     'truncated': bool(result.get('truncated')) or len(rows) >= MAX_VALUES}
            for row in rows[:MAX_VALUES]:
                if not isinstance(row, list) or len(row) != 2 or not all(_scalar(v) for v in row):
                    probe['truncated'] = True; continue
                probe['rows'].append(row)
            if not add('probes', probe): break
            for index, row in enumerate(probe['rows'], 1):
                add('facts', {'id': f'{pid}V{index}', 'kind': 'source_predicate_witness', 'probe_id': pid,
                              'source_value': row[0], 'value': bool(row[1]) if row[1] in (0, 1) else row[1]})
    return out


def review_challenges(check, evidence, question, history):
    """Challenge explicit technical contradictions, never infer correctness."""
    issues = check.get('issues')
    if not isinstance(issues, list) or not 1 <= len(issues) <= MAX_ISSUES or _size(issues) > 6000: return []
    known = {v['id']: v for v in evidence['facts']}
    originals = _originals(question, history)
    seen, challenges = set(), []
    for issue in issues:
        if not isinstance(issue, dict): return []
        ident, dimension = issue.get('id'), issue.get('dimension')
        if not isinstance(ident, str) or not re.fullmatch(r'I[1-9][0-9]?', ident) or ident in seen or dimension not in REVIEW_FIELDS: return []
        seen.add(ident)
        scope = check.get('scope_review') if isinstance(check.get('scope_review'), dict) else {}
        if check.get('supported') is True and scope.get(dimension) is not False: continue
        reason = None
        if issue.get('kind') == 'runtime_fact':
            fact = known.get(issue.get('fact_id')) if isinstance(issue.get('fact_id'), str) else None
            if fact and 'asserted_value' in issue and _scalar(issue['asserted_value']):
                if type(issue['asserted_value']) is not type(fact['value']) or issue['asserted_value'] != fact['value']:
                    reason = {'kind': 'runtime_fact_contradiction', 'fact_id': fact['id'], 'observed_value': fact['value']}
        elif issue.get('kind') == 'user_requirement':
            quote = issue.get('user_quote')
            requirement = issue.get('requirement')
            if requirement not in ('currency_conversion', 'null_propagation', 'exact_column_alias'): continue
            if not isinstance(quote, str) or not 1 <= len(quote) <= 300 or not any(quote in q for q in originals):
                reason = {'kind': 'missing_literal_user_basis', 'requirement': requirement}
            elif requirement == 'currency_conversion':
                target = issue.get('target_unit')
                if isinstance(target, str) and target in ('元', '分', '万元', '亿元', 'USD', 'CNY', '美元') and target not in quote and not re.search(r'(?:除以|乘以|[/÷×*])\s*\d|换算|转换', quote):
                    reason = {'kind': 'conversion_not_literal_in_cited_quote', 'requirement': requirement, 'user_quote': quote}
            elif requirement == 'null_propagation' and not re.search(r'(?:未知|缺失|NULL).{0,30}(?:累计|合计|整组|整体|全部).{0,20}(?:未知|NULL)|(?:累计|合计).{0,20}(?:只要|遇到|存在).{0,20}(?:未知|NULL)', quote, re.I):
                reason = {'kind': 'propagation_not_literal_in_cited_quote', 'requirement': requirement, 'user_quote': quote}
            elif requirement == 'exact_column_alias' and not re.search(r'别名|精确列名|列名必须|列名严格|命名为|字段名必须', quote):
                reason = {'kind': 'exact_alias_not_literal_in_cited_quote', 'requirement': requirement, 'user_quote': quote}
        if reason: challenges.append({'issue_id': ident, 'dimension': dimension, **reason})
    # A second opinion cannot silently remove an unrelated, unchallenged issue.
    return challenges if {v['issue_id'] for v in challenges} == seen else []


def interpretation_recheck_allowed(plan, previous_result):
    """Permit one independently reviewed explanation repair, never approve SQL."""
    query = (previous_result or {}).get('query') or {}
    check = query.get('effective_review_response') or query.get('review_response') or {}
    correction = check.get('corrected_interpretation')
    scope = check.get('scope_review') or {}
    issues = check.get('issues')
    mechanical = query.get('mechanical_contract') or {}
    if (plan.get('status') != 'ready' or check.get('failure_kind') != 'invalid_plan'
            or check.get('interpretation_supported') is not False
            or not isinstance(correction, dict)
            or not isinstance(correction.get('normalized_question'), str)
            or not correction['normalized_question'].strip()
            or not isinstance(correction.get('intent'), dict)
            or not isinstance(issues, list) or not 1 <= len(issues) <= MAX_ISSUES
            or _size(correction) > 20000
            or any(scope.get(k) is not True for k in REVIEW_FIELDS if k != 'output_contract')
            or mechanical.get('unresolved_issues', mechanical.get('issues', []))):
        return False
    # Only a received, explanation-specific issue may take this path. Source,
    # arithmetic, NULL, grain and predicate defects remain ordinary SQL repairs.
    if any(not isinstance(i, dict) or i.get('kind') != 'business_interpretation'
           or i.get('dimension') != 'output_contract' for i in issues):
        return False
    if plan.get('normalized_question') != correction['normalized_question'] or plan.get('intent') != correction['intent']:
        return False
    previous = query.get('plan') or {}
    if all(plan.get(k) == previous.get(k) for k in ('normalized_question', 'intent')):
        return False
    return (fingerprint(plan.get('sql'), plan.get('parameters', []))
            == fingerprint(query.get('sql'), query.get('parameters', [])))


def adopted_review(response, initial, challenges):
    if not isinstance(response, dict) or response.get('decision') != 'retract': return None
    ids = response.get('resolved_issue_ids')
    expected = {c['issue_id'] for c in challenges}
    if not isinstance(ids, list) or len(ids) != len(expected) or not all(isinstance(v, str) for v in ids) or set(ids) != expected: return None
    reviewed = response.get('review')
    if not isinstance(reviewed, dict) or reviewed.get('supported') is not True or reviewed.get('interpretation_supported') is not True: return None
    # The caller still reapplies every mechanical/scope/table publication gate.
    return reviewed


def audit_record(query, actual, index, status):
    rows = actual.get('rows') or []
    record = {'candidate_index': index, 'ast_parameters_sha256': fingerprint(query.get('sql'), query.get('parameters', [])),
              'sql': query.get('sql'), 'parameters': query.get('parameters', []),
              'normalized_question': query.get('plan', {}).get('normalized_question'), 'status': status,
              'review_response': query.get('review_response'), 'mechanical_contract': query.get('mechanical_contract') or {},
              'execution_preview': {'columns': actual.get('columns', []), 'rows': rows[:40],
                                    'display_rows': actual.get('display_rows', rows)[:40],
                                    'returned_rows': len(rows), 'total_rows': query.get('total_rows'),
                                    'truncated': len(rows) > 40 or query.get('truncated', False)},
              'recovery_reason': query.get('semantic_recovery_reason')}
    for key in ('review_evidence', 'review_adjudication', 'effective_review_source', 'interpretation_only_recheck'):
        if key in query: record[key] = query[key]
    from .audit_redaction import redact_diagnostics
    return redact_diagnostics(record)
