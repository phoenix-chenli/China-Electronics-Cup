"""Report actual text-binding matches without guessing business ambiguity.

A repeated country is normally a dimension; a repeated name may be several
entities. The caller receives real counts and bounded identifying candidates,
then judges the user's intent. These independent equality probes do not claim
to reproduce the complete SQL's join/filter semantics.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

from .parsers import quoted, readonly_database
from .sql_engine import SQLExecutionError, _parse, execute_sql

MAX_BINDINGS = 6
MAX_CANDIDATES = 8
MAX_CANDIDATE_COLUMNS = 10
MAX_CELL_CHARS = 160
MAX_VALUE_CHARS = 1000
MAX_REPORT_CHARS = 18_000
MAX_SECONDS = 8


def _skipped(reason, table=None, column=None, value=None, predicate=None):
    return {'status': 'skipped', 'reason': reason, 'table': table, 'column': column,
            'value': value, 'predicate': predicate, 'matched_rows': None,
            'primary_key': [], 'candidate_columns': [], 'candidates': [], 'truncated': True}


def _bound_tree(sql, parameters):
    """Attach values in lexical SQLite placeholder order, not AST walk order.

    A WITH body appears later in sqlglot's AST walk than the outer SELECT; using
    walk order would silently swap its positional values. Rewriting *only for
    analysis* to unique named placeholders preserves a trustworthy binding map.
    """
    _parse(sql)
    parameters = [] if parameters is None else parameters
    if not isinstance(parameters, (list, dict)):
        raise ValueError('参数不是数组或对象')
    tokens = sqlglot.Dialect.get_or_raise('sqlite').tokenize(sql)
    replacements, values, seen_names = [], {}, {}
    prefix = '__zh_entity_binding_'
    while prefix in sql:
        prefix = '_' + prefix
    last_index = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = None
        end = token.end
        if token.token_type == TokenType.PLACEHOLDER:
            if index+1 < len(tokens) and tokens[index+1].start == token.end+1 and tokens[index+1].token_type == TokenType.NUMBER:
                raise ValueError('数字编号占位符未作安全映射')
            last_index += 1
            parameter_index = last_index
            if isinstance(parameters, dict):
                raise ValueError('匿名占位符不能使用对象参数')
        elif token.token_type == TokenType.COLON:
            if index+1 >= len(tokens) or tokens[index+1].start != token.end+1 or not re.fullmatch(r'[A-Za-z_]\w*', tokens[index+1].text):
                raise ValueError('命名参数格式无法安全解析')
            index += 1
            name = tokens[index].text
            end = tokens[index].end
            if name not in seen_names:
                last_index += 1
                seen_names[name] = last_index
            parameter_index = seen_names[name]
        elif token.token_type == TokenType.PARAMETER or token.token_type == TokenType.VAR and token.text.startswith('$'):
            raise ValueError('仅分析 ? 和 :name 形式的参数')
        else:
            index += 1
            continue
        if isinstance(parameters, dict):
            if name not in parameters:
                raise ValueError('缺少查询的命名参数')
            value = parameters[name]
        else:
            if parameter_index > len(parameters):
                raise ValueError('缺少查询的位置参数')
            value = parameters[parameter_index-1]
        key = prefix + str(len(replacements))
        values[key] = value
        replacements.append((token.start, end+1, ':'+key))
        index += 1
    for start,end,replacement in reversed(replacements):
        sql = sql[:start]+replacement+sql[end:]
    return _parse(sql), values


def _text_value(node, bindings):
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    if isinstance(node, exp.Placeholder):
        return bindings.get(node.name)
    return None


def _filter_context(node, scope):
    """Exclude equality used only as an output expression, or negated equality."""
    parent = node.parent
    filtered = False
    while parent is not None and parent is not scope.expression:
        if isinstance(parent, exp.Not):
            return False, '否定条件不表示选取该文本值'
        if isinstance(parent, (exp.Where, exp.Having, exp.Join)):
            filtered = True
        parent = parent.parent
    return filtered, None


def _resolve_column(column, scope, available):
    if not isinstance(column, exp.Column):
        return None, None, '文本值与表达式比较，无法直接归属原表字段'
    if column.catalog or column.db:
        return None, column.name, '不允许查询其他库或元数据'
    try:
        selected = scope.selected_sources
    except sqlglot.errors.OptimizeError:
        return None, column.name, '表别名存在歧义'
    if column.table:
        matched = [(alias, source) for alias,(_,source) in selected.items() if alias.casefold()==column.table.casefold()]
        if not matched:
            return None, column.name, '字段引用不在当前查询作用域中'
    else:
        # Derived scopes can expose the same name. Do not assign their column to
        # the first physical table simply because a name happens to match.
        if any(isinstance(source, Scope) for _,source in selected.values()):
            return None, column.name, '未限定字段同时涉及派生表，无法安全归属'
        matched = [(alias, source) for alias,(_,source) in selected.items()
                   if isinstance(source, exp.Table) and source.name.casefold() in available
                   and column.name.casefold() in available[source.name.casefold()]['column_map']]
        if len(matched)!=1:
            return None, column.name, '字段不存在或在多个原表中同名'
    source = matched[0][1]
    if not isinstance(source, exp.Table):
        return None, column.name, '字段来自CTE或派生表，未假定它等于某个原表字段'
    if source.db or source.catalog:
        return None, column.name, '不允许查询其他库'
    entry = available.get(source.name.casefold())
    if not entry:
        return source.name, column.name, '该表不属于当前资料可查询的普通表'
    actual = entry['column_map'].get(column.name.casefold())
    if actual is None:
        return entry['name'], column.name, '字段不在当前真实表结构中'
    return entry['name'], actual, None


def _metadata(source, stopped):
    allowed = {t['name'].casefold(): t for t in source.get('tables',[]) if isinstance(t,dict) and isinstance(t.get('name'),str)}
    with readonly_database(source['data_path']) as connection:
        connection.set_progress_handler(lambda:int(stopped()),1000)
        kinds = {(r[0],r[1]):r[2] for r in connection.execute('PRAGMA table_list')}
        actual = [(name,sql) for name,sql in connection.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'")
                  if name.casefold() in allowed and not name.casefold().startswith('sqlite_') and kinds.get(('main',name))=='table']
        result={}
        for name,sql in actual:
            if stopped():
                raise InterruptedError('候选检查已中止')
            if not sql or re.match(r'\s*CREATE\s+VIRTUAL\s+TABLE\b',sql,re.I):
                continue
            columns=[r for r in connection.execute(f'PRAGMA table_xinfo({quoted(name)})') if r[6] in (0,2,3)]
            column_names=[r[1] for r in columns]
            if {c.casefold() for c in column_names}!={str(c.get('name','')).casefold() for c in allowed[name.casefold()].get('columns',[])}:
                raise ValueError('资料结构已变化，未查询候选')
            primary_key=[r[1] for r in sorted(columns,key=lambda r:r[5]) if r[5]]
            foreign_keys=list(dict.fromkeys(row[3] for row in connection.execute(f'PRAGMA foreign_key_list({quoted(name)})')))
            text_columns=[r[1] for r in columns if any(token in str(r[2]).upper() for token in ('CHAR','TEXT','CLOB')) or not r[2]]
            result[name.casefold()]={'name':name,'column_map':{c.casefold():c for c in column_names},
                                    'primary_key':primary_key,'foreign_keys':foreign_keys,'text_columns':text_columns}
        return result


def inspect_bindings(source, sql, parameters=None, cancelled=lambda:False):
    """Return true match counts and bounded candidates for up to six bindings.

    Candidate counts use independent equality on the original table, including
    its collation. They are neither the final answer nor an ambiguity verdict.
    Unsupported expressions/scopes are explicitly skipped. Cancellation raises
    InterruptedError; query/runtime budget failures return skipped evidence.
    """
    started=time.monotonic()
    def stopped():
        return cancelled() or time.monotonic()-started>MAX_SECONDS
    def stop_record():
        if cancelled():
            raise InterruptedError('查询已取消')
        return _skipped('候选检查达到时间预算，未继续探查')
    if cancelled():
        raise InterruptedError('查询已取消')
    try:
        tree,bindings=_bound_tree(sql,parameters)
    except (SQLExecutionError,ValueError,sqlglot.errors.SqlglotError,RecursionError):
        return [_skipped('SQL或参数无法安全解析，未执行实体候选探查')]
    try:
        available=_metadata(source,stopped)
    except InterruptedError:
        return [stop_record()]
    except (ValueError,KeyError,sqlite3.Error):
        if stopped():
            return [stop_record()]
        return [_skipped('当前资料结构无法安全读取，未执行实体候选探查')]
    findings=[]
    seen=set()
    attempts=0
    for scope in traverse_scope(tree):
        for predicate in scope.find_all(exp.EQ, exp.In):
            if stopped():
                findings.append(stop_record())
                return findings
            is_filter,reason=_filter_context(predicate,scope)
            if not is_filter and reason is None:
                continue
            if isinstance(predicate, exp.EQ):
                left,right=predicate.this,predicate.expression
                if isinstance(_text_value(left,bindings),str) and not isinstance(_text_value(right,bindings),str):
                    left,right=right,left
                operands=[right]
                kind='eq'
            else:
                left=predicate.this
                operands=predicate.expressions
                kind='in'
                if predicate.args.get('query'):
                    # Predicates inside the subquery are analyzed in its scope.
                    continue
                if len(operands)>MAX_BINDINGS:
                    findings.append(_skipped('IN值超过本次6项检查预算',column=left.name if isinstance(left,exp.Column) else None,predicate=kind))
                    continue
            for operand in operands:
                value=_text_value(operand,bindings)
                if not isinstance(value,str):
                    continue
                if attempts>=MAX_BINDINGS:
                    findings.append(_skipped('文本绑定超过本次6项检查预算'))
                    return findings
                attempts+=1
                table,column,resolution_error=_resolve_column(left,scope,available)
                reason=reason or resolution_error
                if reason or len(value)>MAX_VALUE_CHARS:
                    findings.append(_skipped(reason or '文本筛选值过长，未执行候选探查',table,column,value if len(value)<=MAX_VALUE_CHARS else None,kind))
                    continue
                key=(table,column,value)
                if key in seen:
                    continue
                seen.add(key)
                entry=available[table.casefold()]
                fields=list(dict.fromkeys(entry['primary_key']+[column]+entry['foreign_keys']+entry['text_columns']))
                candidate_columns=fields[:MAX_CANDIDATE_COLUMNS]
                columns_truncated=len(fields)>len(candidate_columns)
                table_sql,column_sql=quoted(table),quoted(column)
                try:
                    count=execute_sql(source,f'SELECT COUNT(*) AS "匹配记录数" FROM {table_sql} WHERE {column_sql} = ?',[value],stopped)
                    matched_rows=int(count['rows'][0][0])
                    order=entry['primary_key'] or [column]
                    projection=','.join(quoted(c) for c in candidate_columns)
                    ordering=','.join(quoted(c) for c in order)
                    candidates=execute_sql(source,f'SELECT {projection} FROM {table_sql} WHERE {column_sql} = ? ORDER BY {ordering} LIMIT {MAX_CANDIDATES}',[value],stopped)
                except InterruptedError:
                    findings.append(stop_record())
                    return findings
                except SQLExecutionError as exc:
                    findings.append(_skipped('候选探查未完成：'+str(exc)[:200],table,column,value,kind))
                    continue
                clipped=False
                rows=[]
                for row in candidates['rows']:
                    values=[]
                    for cell in row:
                        if isinstance(cell,str) and len(cell)>MAX_CELL_CHARS:
                            cell=cell[:MAX_CELL_CHARS]+'…'
                            clipped=True
                        values.append(cell)
                    rows.append(values)
                record={'status':'checked','table':table,'column':column,'value':value,
                        'predicate':kind,'matched_rows':matched_rows,'primary_key':entry['primary_key'],
                        'candidate_columns':candidate_columns,'candidates':rows,
                        'truncated':matched_rows>len(rows) or columns_truncated or clipped or candidates.get('truncated',False),
                        'stats_scope':'independent_equality',
                        'note':'仅统计该原表字段与筛选值相等的记录；不代表完整查询的最终匹配数，是否需要澄清取决于用户是否指向单个实体。'}
                while rows and len(json.dumps(findings+[record],ensure_ascii=False))>MAX_REPORT_CHARS:
                    rows.pop()
                    record['truncated']=True
                if len(json.dumps(findings+[record],ensure_ascii=False))>MAX_REPORT_CHARS:
                    findings.append(_skipped('候选报告达到长度预算，未继续探查'))
                    return findings
                findings.append(record)
    return findings
