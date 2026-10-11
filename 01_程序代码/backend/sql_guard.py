"""Validate model SQL against a server-owned catalog before read-only execution.

This module never connects to a database. Catalog relations must come from a
declared foreign key or a candidate whose key/coverage checks the server ran.
The caller still owns read-only credentials, authorizers, deadlines, response
budgets and cancellation. SQL validation cannot prove a business interpretation.
"""
from __future__ import annotations

from collections import defaultdict

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema
from sqlglot.tokens import TokenType


MAX_SQL_CHARS = 12000
MAX_DEPTH = 30
MAX_SCOPES = 20
MAX_TABLE_REFERENCES = 16
MAX_JOINS = 12
MAX_OFFSET = 10000

# Deliberately finite. Unknown functions, extension functions and table-valued
# functions are rejected even when their spelling looks read-only.
SQLITE_FUNCTIONS = frozenset({
    "abs", "avg", "count", "sum", "total", "min", "max", "round", "coalesce",
    "ifnull", "nullif", "iif", "length", "lower", "upper", "trim", "ltrim",
    "rtrim", "replace", "substr", "substring", "instr", "date", "time",
    "datetime", "strftime", "julianday", "unixepoch", "ceil", "ceiling",
    "floor", "concat", "concat_ws", "group_concat", "string_agg", "row_number",
    "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "nth_value",
    # SQLite invokes these builtins for the already permitted SQL operators.
    "like", "glob",
})
SAFE_FUNCTIONS = frozenset({
    "ABS", "AVG", "COUNT", "SUM", "MIN", "MAX", "ROUND", "COALESCE", "NULLIF",
    "IF", "CASE", "CAST", "AND", "OR", "EXISTS", "LENGTH", "LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM",
    "REPLACE", "SUBSTRING", "STR_POSITION", "DATE", "TIME", "TIMESTAMP",
    "DATE_TRUNC", "TIMESTAMP_TRUNC", "EXTRACT", "TIME_TO_STR", "STR_TO_TIME",
    "TS_OR_DS_TO_TIMESTAMP", "TS_OR_DS_TO_DATE", "DATE_ADD", "DATE_SUB",
    "DATE_DIFF", "CEIL", "FLOOR", "CONCAT", "CONCAT_WS", "GROUP_CONCAT",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE",
    "LAST_VALUE", "NTH_VALUE", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
})
SAFE_ANONYMOUS = SQLITE_FUNCTIONS | frozenset({
    "char_length", "character_length", "date_format", "date_part", "to_char",
    "string_agg", "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp",
})
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "mysql", "sys", "performance_schema", "temp"}
FORBIDDEN_TYPES = tuple(getattr(exp, name) for name in (
    "DML", "DDL", "Command", "Into", "Lock", "Pragma", "Attach", "Detach",
    "Transaction", "Commit", "Rollback", "Set", "Use", "Copy", "LoadData",
    "Lateral", "Unnest", "UDTF", "Values", "TableSample", "Pivot", "Unpivot",
    "Parameter", "SessionParameter", "Property", "Hint", "Operator",
) if hasattr(exp, name))


def allowed_sqlite_function(name):
    """Function policy for SQLITE_FUNCTION authorizer calls (which use SQL names)."""
    return isinstance(name, str) and name.casefold() in SQLITE_FUNCTIONS


def _error(message):
    raise ValueError(message)


def _norm(name, dialect):
    return name.casefold() if dialect == "sqlite" else name


def _unsafe_name(table, namespace=""):
    lower = table.casefold()
    return (namespace.casefold() in SYSTEM_SCHEMAS or lower.startswith(("sqlite_", "pg_"))
            or lower in {"information_schema", "pg_catalog", "sqlite_master", "sqlite_schema"})


def _catalog(schema, dialect):
    if not isinstance(schema, dict) or not isinstance(schema.get("tables"), dict) or not schema["tables"]:
        _error("SQL 查询缺少服务端表结构")
    tables, nested = {}, {}
    for qualified, table in schema["tables"].items():
        if not isinstance(qualified, str) or not qualified or not isinstance(table, dict):
            _error("服务端表结构格式不正确")
        namespace, name = qualified.split(".", 1) if "." in qualified else ("main" if dialect == "sqlite" else "public", qualified)
        if not namespace or not name or _unsafe_name(name, namespace):
            _error("不允许查询系统表或系统命名空间")
        columns = table.get("columns")
        if not isinstance(columns, dict) or not columns or not all(isinstance(k, str) and k for k in columns):
            _error("服务端表结构缺少字段")
        namespace, name = _norm(namespace, dialect), _norm(name, dialect)
        key = namespace + "." + name
        if key in tables:
            _error("服务端表名重复")
        normalized = {_norm(k, dialect): str(v or "UNKNOWN") for k, v in columns.items()}
        tables[key] = {"namespace": namespace, "name": name, "columns": normalized}
        nested.setdefault(namespace, {})[name] = normalized
    return tables, MappingSchema(nested, dialect=dialect, normalize=dialect == "sqlite")


def _table_key(node, tables, dialect):
    if not isinstance(node.this, exp.Identifier) or node.catalog:
        _error("只允许服务端已授权的普通数据表")
    name, namespace = _norm(node.name, dialect), _norm(node.db, dialect)
    if _unsafe_name(name, namespace):
        _error("不允许查询系统表或系统命名空间")
    candidates = [k for k, v in tables.items() if v["name"] == name and (not namespace or v["namespace"] == namespace)]
    if len(candidates) != 1:
        _error("SQL 表不存在、未授权或名称不明确")
    return candidates[0]


def _placeholder_input(sql, dialect):
    """Convert driver %s outside strings/comments to AST qmark placeholders."""
    tokens = sqlglot.tokenize(sql, read=dialect)
    if not tokens or tokens[0].token_type not in {TokenType.SELECT, TokenType.WITH}:
        _error("只允许单条 SELECT 或 WITH 查询")
    replacements = []
    for index, token in enumerate(tokens):
        if (dialect != "sqlite" and token.token_type == TokenType.MOD and index + 1 < len(tokens)
                and tokens[index + 1].text == "s" and tokens[index + 1].start == token.end + 1):
            replacements.append((token.start, tokens[index + 1].end + 1, "?"))
    for start, end, replacement in reversed(replacements):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def _driver_sql(sql, dialect, parameter_count):
    if dialect == "sqlite":
        return sql
    tokens = sqlglot.tokenize(sql, read=dialect)
    spans = []
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.PLACEHOLDER and token.text == "?":
            spans.append((token.start, token.end + 1))
        elif (token.token_type == TokenType.MOD and index + 1 < len(tokens)
              and tokens[index + 1].text == "s" and tokens[index + 1].start == token.end + 1):
            spans.append((token.start, tokens[index + 1].end + 1))
    if len(spans) != parameter_count:
        _error("SQL 参数占位符无法安全转换")
    pieces, position = [], 0
    for start, end in spans:
        text = sql[position:start]
        pieces.extend([text.replace("%", "%%") if parameter_count else text, "%s"])
        position = end
    tail = sql[position:]
    pieces.append(tail.replace("%", "%%") if parameter_count else tail)
    return "".join(pieces)


def _budget(tree, max_nodes):
    stack, count = [(tree, 1)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > max_nodes or depth > MAX_DEPTH:
            _error("SQL 结构超过复杂度上限，请拆分问题")
        stack.extend((child, depth + 1) for child in node.iter_expressions())
    if len(list(tree.find_all(exp.Join))) > MAX_JOINS:
        _error("SQL 关联数量超过上限")


def _structural(tree):
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        _error("只允许单条 SELECT 或 WITH 查询")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_TYPES):
            _error("SQL 包含写入、系统指令或不支持的执行结构")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            _error("不允许递归 CTE")
        if isinstance(node, exp.Table) and (not isinstance(node.this, exp.Identifier) or node.args.get("pivots")):
            _error("不允许外部数据源、表函数或动态表")
        if isinstance(node, exp.Dot) and any(isinstance(n, exp.Func) for n in node.walk()):
            _error("不允许带命名空间的函数")
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.USERDEFINED:
            _error("不允许自定义类型转换或服务器自定义执行入口")
        if isinstance(node, exp.Func):
            name = node.name.casefold() if isinstance(node, exp.Anonymous) else node.sql_name()
            allowed = name in SAFE_ANONYMOUS if isinstance(node, exp.Anonymous) else name in SAFE_FUNCTIONS
            if not allowed:
                _error("SQL 包含未授权函数")
        if isinstance(node, exp.Placeholder) and node.this:
            _error("SQL 只支持位置参数 ?，不支持命名参数")
        if isinstance(node, exp.Literal) and len(str(node.this)) > 2000:
            _error("SQL 字面量过长，请使用绑定参数")
        if isinstance(node, exp.Join):
            if node.kind in {"CROSS", "ASOF"} or node.method == "NATURAL" or node.args.get("global"):
                _error("不允许笛卡尔积、自然关联或特殊关联")
            if node.args.get("side") == "FULL":
                _error("当前不支持全外关联")
            if not node.args.get("on") and not node.args.get("using"):
                _error("关联必须包含已验证的键条件")
            if node.args.get("on") and any(isinstance(n, (exp.Or, exp.Not)) for n in node.args["on"].walk()):
                _error("关联键不能被 OR 或 NOT 条件绕过")


def _relations(schema, tables, dialect):
    accepted = []
    for relation in schema.get("relations", []):
        if not isinstance(relation, dict) or relation.get("source") not in {"foreign_key", "verified_candidate"}:
            continue
        if relation.get("verified") is not True:
            continue
        left, right = relation.get("left_table"), relation.get("right_table")
        if not isinstance(left, str) or not isinstance(right, str):
            continue
        left, right = _norm(left, dialect), _norm(right, dialect)
        a, b = relation.get("left_columns"), relation.get("right_columns")
        if (left not in tables or right not in tables or not isinstance(a, list) or not isinstance(b, list)
                or not a or len(a) != len(b) or not all(isinstance(c, str) for c in a + b)):
            continue
        a, b = [_norm(c, dialect) for c in a], [_norm(c, dialect) for c in b]
        if not all(c in tables[left]["columns"] for c in a) or not all(c in tables[right]["columns"] for c in b):
            continue
        if len(set(a)) != len(a) or len(set(b)) != len(b):
            continue
        accepted.append({"left_table": left, "left_columns": a, "right_table": right,
                         "right_columns": b, "source": relation["source"], "verified": True})
    return accepted


def _origin(scope, column, tables, dialect, depth=0):
    """Trace only direct key projections; aggregate/expression-created keys fail."""
    if depth > MAX_DEPTH or not isinstance(column, exp.Column) or not column.table:
        return None
    alias, name = column.table, _norm(column.name, dialect)
    source = scope.selected_sources.get(alias)
    if source is None:
        # Only correlated subqueries may resolve ancestor aliases. qualify()
        # separately rejects references that SQL's visibility rules disallow.
        if scope.can_be_correlated and scope.parent is not None:
            return _origin(scope.parent, column, tables, dialect, depth + 1)
        return None
    source = source[1]
    if isinstance(source, exp.Table):
        return (_table_key(source, tables, dialect), name)
    if isinstance(source, Scope):
        if not isinstance(source.expression, exp.Select):
            return None
        projections = [item for item in source.expression.selects if _norm(item.alias_or_name, dialect) == name]
        if len(projections) != 1:
            return None
        target = projections[0].this if isinstance(projections[0], exp.Alias) else projections[0]
        return _origin(source, target, tables, dialect, depth + 1)
    return None


def _unit_origin(scope,node,tables,dialect,depth=0):
    """Only direct fields and unit-preserving aggregates can inherit a declared unit."""
    if depth>MAX_DEPTH:return None
    if isinstance(node,exp.Alias):return _unit_origin(scope,node.this,tables,dialect,depth+1)
    if isinstance(node,(exp.Sum,exp.Avg,exp.Min,exp.Max)):
        return _unit_origin(scope,node.this,tables,dialect,depth+1)
    if not isinstance(node,exp.Column) or not node.table:return None
    source=scope.selected_sources.get(node.table)
    if source is None:return None
    source=source[1];name=_norm(node.name,dialect)
    if isinstance(source,exp.Table):return _table_key(source,tables,dialect)+'.'+name
    if isinstance(source,Scope) and isinstance(source.expression,exp.Select):
        selections=[p for p in source.expression.selects if _norm(p.alias_or_name,dialect)==name]
        if len(selections)==1:return _unit_origin(source,selections[0],tables,dialect,depth+1)
    return None


def _conjuncts(node):
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _matching_relation(pairs, relations):
    for relation in relations:
        required = [frozenset(((relation["left_table"], a), (relation["right_table"], b)))
                    for a, b in zip(relation["left_columns"], relation["right_columns"])]
        if all(pair in pairs for pair in required):
            return relation
    return None


def _group_unique(scope,alias,origins,tables,dialect):
    """Prove a derived row is unique on the complete, directly projected key."""
    selected=scope.selected_sources.get(alias)
    source=selected[1] if selected else None
    if not isinstance(source,Scope) or not isinstance(source.expression,exp.Select):
        return False
    group=source.expression.args.get('group')
    if not group or set(group.args)-{'expressions'}:
        return False
    keys=[_origin(source,node,tables,dialect) for node in group.expressions]
    return bool(keys) and all(keys) and set(keys)==origins


def _joins(scopes, tables, relations, dialect):
    used = []
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            continue
        root = scope.expression.args.get("from_")
        if root is None:
            continue
        prior = {root.this.alias_or_name}
        for join in scope.expression.args.get("joins", []):
            alias = join.this.alias_or_name
            condition = join.args.get("on")
            # qualify_columns expands USING into qualified equality predicates.
            if condition is None:
                _error("关联键无法解析到已授权字段")
            grouped,identical = defaultdict(set),defaultdict(set)
            for predicate in _conjuncts(condition):
                if not isinstance(predicate, exp.EQ) or not isinstance(predicate.left, exp.Column) or not isinstance(predicate.right, exp.Column):
                    continue
                a, b = predicate.left, predicate.right
                if a.table == alias and b.table in prior:
                    other = b.table
                elif b.table == alias and a.table in prior:
                    other = a.table
                else:
                    continue
                x, y = _origin(scope, a, tables, dialect), _origin(scope, b, tables, dialect)
                if x and y and x != y:
                    grouped[other].add(frozenset((x, y)))
                elif x and x==y:
                    identical[other].add(x)
            matched = next((relation for pairs in grouped.values() if (relation := _matching_relation(pairs, relations))), None)
            if matched is None:
                for other,origins in identical.items():
                    right_unique=_group_unique(scope,alias,origins,tables,dialect)
                    left_unique=_group_unique(scope,other,origins,tables,dialect)
                    def grouped_source(name):
                        source=scope.selected_sources[name][1]
                        return isinstance(source,Scope) and bool(source.expression.args.get('group'))
                    side=join.side or 'INNER'
                    # A non-grouped projection may safely look up one complete
                    # grouped key on the same original fields. Preserve the
                    # stricter complete-key rule when both sides are grouped.
                    lookup=(right_unique and not grouped_source(other) and side in {'INNER','LEFT'}) or (left_unique and not grouped_source(alias) and side in {'INNER','RIGHT'})
                    if (right_unique and left_unique) or lookup:
                        ordered=sorted(origins)
                        matched={'left_table':other,'right_table':alias,'left_columns':[x[1] for x in ordered],
                                 'right_columns':[x[1] for x in ordered],'source':'derived_group_key','verified':True,
                                 'cardinality':'one_to_one' if right_unique and left_unique else 'many_to_one' if right_unique else 'one_to_many',
                                 'basis':'直接投影同一已授权原始键；分组侧使用完整键且每键至多一行，双侧分组时键须完整相同。'}
                        break
            if matched is None:
                _error("关联未使用声明外键或服务端验证关系的完整键；同表不同指标请用条件聚合，分组结果必须使用完整同源键")
            used.append({**matched, "join_alias": alias, "join_type": join.side or join.kind or "INNER"})
            prior.add(alias)
    return used


def _correlations(scopes, tables, relations, dialect):
    """Require correlated table-to-table keys to use a trusted relationship too."""
    used = []
    for scope in scopes:
        if not scope.external_columns or not scope.can_be_correlated:
            continue
        where = scope.expression.args.get("where")
        grouped = defaultdict(set)
        if where:
            for predicate in _conjuncts(where.this):
                if not isinstance(predicate, exp.EQ) or not isinstance(predicate.left, exp.Column) or not isinstance(predicate.right, exp.Column):
                    continue
                a, b = predicate.left, predicate.right
                local_a, local_b = a.table in scope.selected_sources, b.table in scope.selected_sources
                if local_a == local_b:
                    continue
                x, y = _origin(scope, a, tables, dialect), _origin(scope, b, tables, dialect)
                if x and y and x != y:
                    grouped[tuple(sorted((a.table, b.table)))].add(frozenset((x, y)))
        matched = next((relation for pairs in grouped.values() if (relation := _matching_relation(pairs, relations))), None)
        if matched is None:
            _error("相关子查询必须使用已验证的完整关联键")
        used.append({**matched, "join_type": "CORRELATED"})
    return used


def _subquery_keys(scopes, tables, relations, dialect):
    """A key IN/scalar-equality subquery is a relationship, not an escape hatch."""
    by_expression = {id(scope.expression): scope for scope in scopes}
    used = []
    for scope in scopes:
        for predicate in scope.walk():
            outer, subquery = None, None
            if isinstance(predicate, exp.In) and predicate.args.get("query"):
                outer, subquery = predicate.this, predicate.args["query"]
            elif isinstance(predicate, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                if isinstance(predicate.left, exp.Subquery):
                    subquery, outer = predicate.left, predicate.right
                elif isinstance(predicate.right, exp.Subquery):
                    outer, subquery = predicate.left, predicate.right
            if not isinstance(subquery, exp.Subquery):
                continue
            inner = by_expression.get(id(subquery.this))
            if inner is None or not isinstance(inner.expression, exp.Select):
                _error("键比较子查询必须直接查询已授权字段")
            outer_columns = list(outer.expressions) if isinstance(outer, exp.Tuple) else [outer]
            inner_columns = [item.this if isinstance(item, exp.Alias) else item for item in inner.expression.selects]
            # Arithmetic cannot conceal a model-invented key relationship. A
            # scalar aggregate threshold (amount > AVG(amount)) is a calculation;
            # IN always compares sets of keys and must preserve direct lineage.
            if not all(isinstance(item, exp.Column) for item in outer_columns + inner_columns):
                aggregates = [node for item in inner_columns for node in item.walk() if isinstance(node, exp.AggFunc)]
                free_columns = []
                for item in inner_columns:
                    for column in item.find_all(exp.Column):
                        ancestor = column.parent
                        while ancestor is not None and ancestor is not item and not isinstance(ancestor, exp.AggFunc):
                            ancestor = ancestor.parent
                        if not isinstance(ancestor, exp.AggFunc):
                            free_columns.append(column)
                if not isinstance(predicate, exp.In) and aggregates and not free_columns:
                    continue
                _error("键比较子查询只能直接投影关联列，标量计算需要真实聚合")
            if len(outer_columns) != len(inner_columns):
                _error("键比较子查询的字段数量不一致")
            pairs = set()
            identical = True
            for a, b in zip(outer_columns, inner_columns):
                x, y = _origin(scope, a, tables, dialect), _origin(inner, b, tables, dialect)
                identical = identical and x is not None and x == y
                if x and y and x != y:
                    pairs.add(frozenset((x, y)))
            if identical:
                continue
            if not isinstance(predicate, (exp.In, exp.EQ)):
                _error("不同来源的键比较仅支持已验证关系的等值条件")
            matched = _matching_relation(pairs, relations)
            if matched is None:
                _error("键比较子查询未使用声明或验证关系")
            used.append({**matched, "join_type": "SUBQUERY_KEY"})
    return used


def _row_budget(tree, max_rows):
    def number_for(node):
        options = node.args.get("limit_options")
        if options and (options.args.get("percent") or options.args.get("with_ties")):
            _error("不支持百分比行数或 WITH TIES，请使用固定行数")
        return (node.args.get("count") or exp.Literal.number(1)) if isinstance(node, exp.Fetch) else node.expression

    for query in tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except):
        limit, offset = query.args.get("limit"), query.args.get("offset")
        for node, cap, label in ((limit, 100000, "行数"), (offset, MAX_OFFSET, "偏移")):
            if node is None:
                continue
            number = number_for(node)
            if not isinstance(number, exp.Literal) or number.is_string or not str(number.this).isdigit() or int(number.this) > cap:
                _error("SQL " + label + "必须为受限的非负整数")
    current = tree.args.get("limit")
    limit = min(int(number_for(current).this), max_rows) if current else max_rows
    tree.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
    return limit


def _output_names(tree):
    names = tree.named_selects
    if not names or len(names) > 200 or len(set(names)) != len(names):
        _error("结果字段名称为空、重复或超过上限；请给字段设置唯一别名")


def validate(sql, schema, *, dialect="sqlite", max_rows=200, max_nodes=500):
    """Return bounded SQL, used catalog paths and positional binding metadata.

    ``sql`` is display SQL in the chosen DB dialect. ``driver_sql`` is for
    execute(sql, tuple(params)) on PostgreSQL/MySQL and safely escapes literal
    percent signs. With no parameters, callers must execute ``sql`` without an
    empty parameter tuple. Neither schema nor budgets may come from a model.
    """
    if dialect not in {"sqlite", "postgres", "mysql"}:
        _error("不支持的 SQL 方言")
    if type(max_rows) is not int or not 1 <= max_rows <= 1001 or type(max_nodes) is not int or not 20 <= max_nodes <= 2000:
        _error("服务端 SQL 查询预算不正确")
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS:
        _error("SQL 查询为空或超过长度上限")
    tables, mapping = _catalog(schema, dialect)
    try:
        statements = sqlglot.parse(_placeholder_input(sql, dialect), read=dialect, error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or statements[0] is None:
            _error("只允许单条 SELECT 或 WITH 查询")
        tree = statements[0]
        # SQLite qualification folds identifiers for catalog lookup. Explicit
        # final-output aliases are display labels; preserve their original case
        # after scope/relationship checks without changing physical identifiers.
        output_aliases = {item.alias.casefold(): item.alias for item in tree.selects
                          if isinstance(item, exp.Alias)} if dialect == 'sqlite' else {}
        _budget(tree, max_nodes)
        _structural(tree)
        scopes = list(traverse_scope(tree))
        if not scopes or len(scopes) > MAX_SCOPES:
            _error("SQL 子查询数量超过上限")
        physical, table_references = set(), 0
        for scope in scopes:
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    key = _table_key(source, tables, dialect)
                    entry = tables[key]
                    source.set("db", exp.to_identifier(entry["namespace"], quoted=True))
                    source.set("this", exp.to_identifier(entry["name"], quoted=True))
                    physical.add(key)
                    table_references += 1
        if table_references > MAX_TABLE_REFERENCES:
            _error("SQL 数据表引用超过上限")
        tree = qualify(tree, dialect=dialect, schema=mapping, infer_schema=False,
                       validate_qualify_columns=True, allow_partial_qualification=False,
                       quote_identifiers=True, identify=True)
        _budget(tree, max_nodes)
        _structural(tree)
        _output_names(tree)
        scopes = list(traverse_scope(tree))
        relations = _relations(schema, tables, dialect)
        root_scope=scopes[-1]
        unit_origins={p.alias_or_name:origin for p in tree.selects
                      if (origin:=_unit_origin(root_scope,p,tables,dialect)) is not None}
        used = (_joins(scopes, tables, relations, dialect) + _correlations(scopes, tables, relations, dialect)
                + _subquery_keys(scopes, tables, relations, dialect))
        columns = set()
        for scope in scopes:
            for column in scope.columns:
                origin = _origin(scope, column, tables, dialect)
                if origin:
                    columns.add(origin[0] + "." + origin[1])
        for projection in tree.selects:
            previous_name = projection.alias_or_name
            original_name = output_aliases.get(previous_name.casefold())
            if original_name and original_name != previous_name:
                projection.set('alias', exp.to_identifier(original_name, quoted=True))
                if previous_name in unit_origins:
                    unit_origins[original_name] = unit_origins.pop(previous_name)
        _output_names(tree)
        limit = _row_budget(tree, max_rows)
        # No provider text/comments should be retained in executable SQL.
        for node in tree.walk():
            node.comments = None
        count = sum(1 for _ in tree.find_all(exp.Placeholder))
        normalized = tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE)
        driver_sql = _driver_sql(normalized, dialect, count)
    except (ParseError, TokenError, OptimizeError, sqlglot.errors.UnsupportedError, RecursionError):
        _error("SQL 无法安全解析，或表字段及作用域不匹配")
    return {"sql": normalized, "driver_sql": driver_sql, "tables": sorted(physical),
            "columns": sorted(columns), "relations": used, "unit_origins":unit_origins,"limit": limit,
            "parameter_count": count, "parameter_style": "qmark" if dialect == "sqlite" else "format",
            "checks": [{"name": "单条只读查询及结构", "status": "passed"},
                       {"name": "表字段和作用域", "status": "passed"},
                       {"name": "声明或验证的关联键", "status": "passed"},
                       {"name": "返回行数预算", "status": "passed", "limit": limit}]}
