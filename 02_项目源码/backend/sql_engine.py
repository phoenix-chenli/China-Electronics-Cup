"""Bounded SQLite queries with actual authorization and honest source provenance.

The AST is a first filter; SQLite's authorizer and read-only connection are the
execution boundary. SQL is never rewritten to silently fix business semantics.
Complex queries expose table-level provenance, not invented contributing rows.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

import sqlglot
from sqlglot import exp

from .parsers import quoted, value_json
from .table_engine import TablePlanError, stable_order_for_table

MAX_SQL_CHARS = 30_000
MAX_AST_NODES = 4_000
MAX_RESULTS = 200
MAX_EVIDENCE = 20
MAX_SECONDS = 12
MAX_VM_STEPS = 4_000_000
MAX_CELL_BYTES = 1_000_000
MAX_RESULT_BYTES = 4_000_000
MAX_LOCATOR_ROWS = 50_000
MAX_TABLE_CONTEXT_CHARS = 16_000
MAX_TABLE_CONTEXT_PER_TABLE = 4_000


def attach_table_contexts(source, evidence):
    """Attach bounded source structure, never infer aggregate contribution rows."""
    from .table_context import context_for_table
    metadata = source.get('metadata') or {}
    structured = {t.get('table_name') for t in metadata.get('table_semantics', [])}
    structured.update(t.get('table_name') for t in metadata.get('table_regions', []))
    structured.update(t.get('table_name') or t.get('name') for t in metadata.get('table_layouts', []))
    document_table = source.get('format') in {'docx', 'pdf', 'xlsx', 'png', 'jpg', 'jpeg', 'image'}
    remaining = MAX_TABLE_CONTEXT_CHARS - 2
    boundary = {'has_table': True, 'truncated': True}
    reserve = len(json.dumps(boundary, ensure_ascii=False)) + 2
    result = []
    for index, original in enumerate(evidence):
        record = dict(original)
        allowance = min(MAX_TABLE_CONTEXT_PER_TABLE, remaining - (len(evidence) - index - 1) * reserve)
        # A small marker reserves room for every affected table, even when the
        # selected source has many tables. No missing structure becomes "safe".
        context = context_for_table(source, record['table_name'],
                                    row_numbers=record.get('row_numbers', []) if record.get('lineage') == 'row' else [],
                                    max_chars=max(128, allowance))
        if not context.get('has_table') and (document_table or record['table_name'] in structured):
            context = {'has_table': True, 'unresolved_structure': True, 'tables': []}
        if context.get('has_table'):
            size = len(json.dumps(context, ensure_ascii=False)) + 2
            if size > allowance:
                context = dict(boundary)
                size = reserve
            record['table_context'] = context
            remaining -= size
        result.append(record)
    return result


def table_review_requirements(evidence):
    """Return the mechanical result's relation review IDs and hard limitations."""
    required, reasons = False, []
    for record in evidence:
        context = record.get('table_context') or {}
        if not context.get('has_table'):
            continue
        required = True
        tables = context.get('tables') or []
        if (context.get('truncated') or context.get('unresolved_structure') or context.get('location_unknown')
                or not tables or any(t.get('truncated') or t.get('structure_unknown')
                                      or t.get('header_basis') in (None, 'unknown') for t in tables)):
            reasons.append(f"表「{record.get('table_name', '')}」的结构、表头或原格位置尚未完整确认")
    return (['C1'] if required else []), list(dict.fromkeys(reasons))

# No extension, filesystem, nondeterministic, or introspection functions.
SAFE_FUNCTIONS = frozenset('''abs round min max sum avg count total coalesce nullif ifnull
    iif if likely unlikely likelihood cast typeof length octet_length lower upper
    trim ltrim rtrim substr substring instr replace concat concat_ws format printf
    unicode char hex unhex quote sign mod ceil ceiling floor trunc pow power sqrt
    exp ln log log10 log2 sin cos tan asin acos atan atan2 sinh cosh tanh asinh acosh
    atanh degrees radians pi date time datetime julianday unixepoch strftime timediff
    current_date current_time current_timestamp group_concat string_agg row_number
    rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value
    nth_value json json_array json_object json_extract json_type json_valid
    json_array_length json_quote json_group_array json_group_object json_patch
    json_remove json_replace json_set json_insert decimal_sum decimal_avg
    decimal_add decimal_sub decimal_mul decimal_div decimal_cmp like glob
    '''.split())

SQL_INSTRUCTIONS = """使用 SQLite 单条只读 SELECT，可用 WITH/CTE、JOIN、子查询、EXISTS、窗口函数、GROUP BY/HAVING、UNION及日期函数。只使用提供的真实表和列，不查询 sqlite 元数据或任何外部库。SQL 字符串值用单引号，标识符用双引号；优先用 ? 占位符与 parameters 数组传筛选值。禁止 PRAGMA、ATTACH、写入、扩展和文件函数。
多表连接须说明真实外键或连接依据，注意一对多造成重复计数；不知道连接键、业务口径、日期范围时先澄清。NULL 是未知，不自动当0。百分数的单位、合计/明细层级由原数据决定，不擅自转换。
金额是整数分时 SUM 可保持整数精度；金额是小数文本时可用 DECIMAL_SUM(expr)、DECIMAL_AVG(expr) 返回十进制字符串。DECIMAL_ADD/SUB/MUL/DIV(a,b)可避免原生算术先产生浮点误差；DECIMAL_CMP(a,b)返回-1/0/1，ORDER BY expr COLLATE DECIMAL可按十进制数排序。DECIMAL_DIV/AVG用40位有效数字、ROUND_HALF_UP，分母为0返回NULL；空字符串或非法数值触发错误而非当0。原生 SUM/AVG/除法/REAL 使用SQLite自身精度，不能声称精确复算。
结果最多展示200行；LIMIT是问题自身TopN时可使用，不能用随意LIMIT改变聚合。结果为空时说明无匹配记录；不能把空集解释为0。复杂查询只有表级来源范围，不声称已经恢复每条贡献行。
"""


class SQLExecutionError(ValueError):
    def __init__(self, message: str, clarification: bool = False, code: str = 'invalid_sql'):
        super().__init__(message)
        self.clarification = clarification
        self.code = code


def _parse(sql):
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS or '\x00' in sql:
        raise SQLExecutionError('SQL 为空或超出长度限制')
    try:
        statements = sqlglot.parse(sql, read='sqlite')
    except (sqlglot.errors.ParseError, RecursionError) as exc:
        raise SQLExecutionError('SQL 语法无法解析，请重新生成查询', code='syntax') from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise SQLExecutionError('只允许一条 SELECT 或 WITH 查询', code='forbidden')
    tree = statements[0]
    nodes = list(tree.walk())
    if len(nodes) > MAX_AST_NODES:
        raise SQLExecutionError('SQL 结构过于复杂，请拆分查询', code='limit')
    for node in nodes:
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Into)):
            raise SQLExecutionError('查询包含不允许的写入或管理操作', code='forbidden')
        if isinstance(node, exp.Table):
            if node.db or node.catalog:
                raise SQLExecutionError('不允许查询其他数据库或带库名前缀的表', code='forbidden')
            if not isinstance(node.this, exp.Identifier):
                raise SQLExecutionError('不允许表函数或外部数据源', code='forbidden')
    return tree


def _parameters(parameters):
    if parameters is None:
        return []
    if not isinstance(parameters, (list, dict)) or len(parameters) > 200:
        raise SQLExecutionError('SQL 参数须为不超过200项的数组或对象')
    if isinstance(parameters, dict) and any(not isinstance(k, str) or len(k) > 100 for k in parameters):
        raise SQLExecutionError('SQL 参数名无效')
    for value in parameters.values() if isinstance(parameters, dict) else parameters:
        if value is not None and not isinstance(value, (str, int, float)):
            raise SQLExecutionError('SQL 参数仅允许字符串、数字或空值')
        if isinstance(value, str) and len(value) > 12_000:
            raise SQLExecutionError('SQL 参数值过长')
        if isinstance(value, int) and not -(2**63) <= value < 2**63:
            raise SQLExecutionError('SQL 整数参数超过64位范围，请使用十进制字符串')
        if isinstance(value, float) and not math.isfinite(value):
            raise SQLExecutionError('SQL 参数不能是无穷值或NaN')
    return parameters.copy()


def _decimal(value):
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > 140 or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?', text):
        raise SQLExecutionError('十进制计算遇到非数值或空字符串，未将其替换为0', True, 'numeric')
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise SQLExecutionError('数值无法解析', True, 'numeric') from exc
    if not number.is_finite() or len(number.as_tuple().digits) > 60 or abs(number.as_tuple().exponent) > 60:
        raise SQLExecutionError('十进制数值位数或指数超出计算范围', True, 'numeric')
    return number


def _decimal_text(number):
    if number is None:
        return None
    text = format(number, 'f')
    return (text.rstrip('0').rstrip('.') if '.' in text else text) or '0'



def _round_display_rows(tree, column_count, rows, raw_rows):
    """Keep SQLite values intact; honor only explicit outer ROUND precision.

    SQLGlot projections map to result positions only without star expansion or
    compound SELECTs. Never infer money/identifier formatting from column names
    or the user's wording. Format the executed ROUND value, not its input.
    """
    if not isinstance(tree, exp.Select) or len(tree.expressions) != column_count:
        return None
    expressions = [projection.this if isinstance(projection, exp.Alias) else projection
                   for projection in tree.expressions]
    if any(projection.is_star for projection in expressions):
        return None
    precisions = {}
    for index, projection in enumerate(expressions):
        if not isinstance(projection, exp.Round):
            continue
        precision = projection.args.get('decimals')
        if not isinstance(precision, exp.Literal) or precision.is_string or not re.fullmatch(r'\d+', precision.this):
            continue
        digits = precision.this.lstrip('0') or '0'
        if len(digits) > 2:
            continue
        places = int(digits)
        if 0 <= places <= 12:
            precisions[index] = places
    if not precisions:
        return None
    displayed = [list(row) for row in rows]
    formatted = False
    for row_index, raw in enumerate(raw_rows):
        for column_index, places in precisions.items():
            value = raw[column_index]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            number = Decimal(str(value))
            if not number.is_finite():
                continue
            with localcontext() as context:
                context.prec = max(28, len(number.as_tuple().digits), number.adjusted() + places + 2)
                rounded = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
                displayed[row_index][column_index] = format(rounded, f'.{places}f')
            formatted = True
    return displayed if formatted else None


def _decimal_functions(connection, faults, calculations):
    class DecimalSum:
        name = 'DECIMAL_SUM'
        def __init__(self):
            self.value = Decimal(0)
            self.count = 0
            self.float_inputs = 0
        def step(self, raw):
            try:
                number = _decimal(raw)
                if number is not None:
                    with localcontext() as context:
                        context.prec = 160
                        self.value += number
                    self.count += 1
                    self.float_inputs += isinstance(raw, float)
            except SQLExecutionError as exc:
                faults.append(exc)
                raise ValueError(str(exc)) from exc
        def finalize(self):
            result = _decimal_text(self.value) if self.count else None
            if len(calculations) < 200:
                calculations.append({'function': self.name, 'non_null_values': self.count, 'float_inputs': self.float_inputs, 'value': result, 'method': 'Decimal(原值字符串)累加，160位上下文'})
            return result
    class DecimalAvg(DecimalSum):
        name = 'DECIMAL_AVG'
        def finalize(self):
            with localcontext() as context:
                context.prec = 40
                context.rounding = ROUND_HALF_UP
                result = _decimal_text(self.value / self.count) if self.count else None
            if len(calculations) < 200:
                calculations.append({'function': self.name, 'non_null_values': self.count, 'float_inputs': self.float_inputs, 'value': result, 'method': 'Decimal累加后除以非空数量，40位有效数字'})
            return result
    def binary(operation):
        def calculate(raw_a, raw_b):
            try:
                a, b = _decimal(raw_a), _decimal(raw_b)
                if a is None or b is None:
                    return None
                if operation == 'cmp':
                    return (a > b) - (a < b)
                with localcontext() as context:
                    context.prec = 40 if operation == 'div' else 160
                    context.rounding = ROUND_HALF_UP
                    if operation == 'div':
                        return _decimal_text(a / b) if b else None
                    return _decimal_text({'add': lambda: a+b, 'sub': lambda: a-b, 'mul': lambda: a*b}[operation]())
            except SQLExecutionError as exc:
                faults.append(exc)
                raise ValueError(str(exc)) from exc
        return calculate
    connection.create_aggregate('DECIMAL_SUM', 1, DecimalSum)
    connection.create_aggregate('DECIMAL_AVG', 1, DecimalAvg)
    for operation in ('add', 'sub', 'mul', 'div', 'cmp'):
        connection.create_function('DECIMAL_' + operation.upper(), 2, binary(operation), deterministic=True)
    connection.create_collation('DECIMAL', binary('cmp'))


def _schema(connection, source):
    declared = {str(t['name']).casefold(): t for t in source.get('tables', []) if isinstance(t, dict) and isinstance(t.get('name'), str)}
    if not declared:
        raise SQLExecutionError('当前资料没有已导入的数据表', True, 'source')
    kinds = {(r[0], r[1]): r[2] for r in connection.execute('PRAGMA table_list')}
    schema, orders = {}, {}
    for name, statement in connection.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'"):
        if name.casefold() not in declared or name.casefold().startswith('sqlite_') or kinds.get(('main', name)) != 'table':
            continue
        if not statement or re.match(r'\s*CREATE\s+VIRTUAL\s+TABLE\b', statement, re.I):
            continue
        columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_xinfo({quoted(name)})') if row[6] in (0, 2, 3)]
        exposed = {str(c.get('name', '')).casefold() for c in declared[name.casefold()].get('columns', [])}
        if exposed != {c.casefold() for c in columns}:
            raise SQLExecutionError('数据表结构与导入记录不一致，请重新解析资料', False, 'source_changed')
        schema[name.casefold()] = {'name': name, 'columns': columns, 'row_count': declared[name.casefold()].get('row_count')}
    return schema, orders


def _simple_locator(tree, connection, schema, parameters=None):
    """Return a projection extension only when it preserves row multiplicity."""
    # Rendering can reorder placeholders: SQLite LIMIT offset,count becomes
    # LIMIT count OFFSET offset. Sequence bindings follow textual slot order,
    # even when placeholders have names. Keep the original SQL and honest
    # table-level provenance whenever parameters are supplied positionally.
    if isinstance(parameters, list) and parameters:
        return None
    if any(isinstance(node, exp.Placeholder) and not node.name for node in tree.walk()):
        return None
    if not isinstance(tree, exp.Select) or any(tree.args.get(k) for k in ('joins', 'group', 'having', 'distinct', 'with_', 'qualify')):
        return None
    if any(isinstance(node, (exp.Subquery, exp.AggFunc, exp.Window)) or isinstance(node, exp.Anonymous) and node.name.lower() in ('decimal_sum', 'decimal_avg', 'total') for node in tree.walk()):
        return None
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or not isinstance(tree.args.get('from_').this, exp.Table):
        return None
    table = tables[0]
    entry = schema.get(table.name.casefold())
    if not entry or not isinstance(entry['row_count'], int) or entry['row_count'] > MAX_LOCATOR_ROWS:
        return None
    try:
        order = stable_order_for_table(connection, entry['name'])
    except TablePlanError:
        return None
    qualifier = table.alias_or_name
    if order in ('rowid', '_rowid_', 'oid'):
        keys = [order]
    else:
        keys = [row[1] for row in sorted(connection.execute(f'PRAGMA table_xinfo({quoted(entry["name"])})'), key=lambda r:r[5]) if row[5]]
    augmented = tree.copy()
    for key in keys:
        augmented.select(exp.column(key, table=qualifier, quoted=True), append=True, copy=False)
    key_sql = ','.join(quoted(key) for key in keys)
    key_rows = connection.execute(f'SELECT {key_sql} FROM {quoted(entry["name"])} ORDER BY {order} LIMIT {MAX_LOCATOR_ROWS+1}').fetchall()
    if len(key_rows) > MAX_LOCATOR_ROWS:
        return None
    return {'sql': augmented.sql(dialect='sqlite'), 'table': entry['name'], 'keys': keys, 'positions': {tuple(row): i+1 for i,row in enumerate(key_rows)}}


def execute_sql(source: dict, sql: str, parameters=None, cancelled=lambda: False) -> dict:
    """Optionally reuse a source-checked mechanical result, never model review."""
    from .deterministic_cache import mechanical_call, sql_source_in_scope
    from .source_snapshot import snapshot
    if cancelled():
        raise InterruptedError('查询已取消')
    tree = _parse(sql)
    parameters = _parameters(parameters)
    # Conservative: all clock/calendar/environment functions bypass caching,
    # even when a particular literal call could be deterministic.
    unstable = {'current_date', 'current_time', 'current_timestamp', 'date', 'time',
                'datetime', 'julianday', 'unixepoch', 'strftime', 'timediff',
                'random', 'randomblob', 'changes', 'total_changes', 'last_insert_rowid'}
    functions = {str(node.name if isinstance(node, exp.Anonymous) else node.sql_name()).casefold()
                 for node in tree.walk() if isinstance(node, exp.Func)}
    # sqlglot normalizes strftime/random into different function node names;
    # checking the original spelling as well avoids silently caching a clock.
    unstable_sql = re.search(r'\b(?:' + '|'.join(sorted(unstable)) + r')\s*(?:\(|\b)', sql, re.IGNORECASE)
    source_scope = snapshot(source) if sql_source_in_scope(source) else None
    payload = {'source': source_scope, 'sql': sql, 'parameters': parameters,
        'strategy': {'version': 'readonly-sql-cache-v1', 'sqlite': sqlite3.sqlite_version,
            'sqlglot': sqlglot.__version__, 'safe_functions': sorted(SAFE_FUNCTIONS),
            'limits': [MAX_SQL_CHARS, MAX_AST_NODES, MAX_RESULTS, MAX_EVIDENCE, MAX_SECONDS,
                MAX_VM_STEPS, MAX_CELL_BYTES, MAX_RESULT_BYTES, MAX_LOCATOR_ROWS,
                MAX_TABLE_CONTEXT_CHARS, MAX_TABLE_CONTEXT_PER_TABLE]}}
    result, audit = mechanical_call('sql_execution', payload,
        lambda: _execute_sql_uncached(source, sql, parameters, cancelled),
        source_ids=[source.get('id')], cancelled=cancelled,
        cacheable=source_scope is not None and not unstable_sql and not functions.intersection(unstable))
    if audit['status'] != 'bypass':
        result['cache'] = {**audit, 'original_execution_elapsed_ms': result.get('elapsed_ms'),
            'note': '命中仅复用已执行的机械查询结果，本次仍需问题语义和来源核验；未重新执行SQL。'
                    if audit['status'] == 'hit' else '已执行只读SQL并保存机械结果，未缓存答案或模型复核。'}
    return result


def _execute_sql_uncached(source: dict, sql: str, parameters=None, cancelled=lambda: False) -> dict:
    """Execute one bounded, read-only SQLite query on the selected source.

    ``total_rows`` counts query output rows only when completely fetched. It is
    not the number of contributing source rows. A SQL LIMIT is respected as part
    of the user's query, independently from this function's display cap.
    """
    started = time.monotonic()
    if cancelled():
        raise InterruptedError('查询已取消')
    tree = _parse(sql)
    parameters = _parameters(parameters)
    path = source.get('data_path')
    if not path or not Path(path).is_file():
        raise SQLExecutionError('资料中的数据库副本不存在', False, 'source')
    faults, calculations, used, reads, denied = [], [], set(), {}, []
    steps = 0
    interrupted = None
    def progress():
        nonlocal steps, interrupted
        steps += 1000
        if cancelled():
            interrupted = 'cancelled'
        elif time.monotonic() - started > MAX_SECONDS or steps > MAX_VM_STEPS:
            interrupted = 'limit'
        return bool(interrupted)
    connection = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    try:
        connection.set_progress_handler(progress, 1000)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA temp_store=MEMORY')
        # Strict identifiers: SQLite otherwise accepts SELECT "typo" as a string.
        if not hasattr(connection, 'setconfig') or not hasattr(sqlite3, 'SQLITE_DBCONFIG_DQS_DML'):
            raise SQLExecutionError('当前Python/SQLite版本无法关闭宽松双引号规则，请使用项目启动环境', False, 'runtime')
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        if hasattr(connection, 'enable_load_extension'):
            connection.enable_load_extension(False)
        for category, limit in ((sqlite3.SQLITE_LIMIT_LENGTH, MAX_CELL_BYTES), (sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_CHARS*4), (sqlite3.SQLITE_LIMIT_COLUMN, 300), (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100), (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 30), (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 200), (sqlite3.SQLITE_LIMIT_ATTACHED, 0), (sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 1000)):
            connection.setlimit(category, limit)
        connection.execute('BEGIN')
        schema, _ = _schema(connection, source)
        _decimal_functions(connection, faults, calculations)
        locator = _simple_locator(tree, connection, schema, parameters)
        cte_names = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
        def authorizer(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_READ:
                entry = schema.get(str(arg1).casefold())
                if (database != 'main' and not (database is None and arg2 == '')) or not entry or trigger and str(trigger).casefold() not in cte_names:
                    denied.append('只允许读取当前资料已导入的普通表')
                    return sqlite3.SQLITE_DENY
                if arg2 and str(arg2).casefold() not in {name.casefold() for name in entry['columns']} and str(arg2).casefold() not in ('rowid', '_rowid_', 'oid'):
                    denied.append('查询列不在导入结构中')
                    return sqlite3.SQLITE_DENY
                used.add(entry['name'])
                if arg2:
                    reads.setdefault(entry['name'], set()).add(str(arg2))
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                if str(arg2).casefold() in SAFE_FUNCTIONS:
                    return sqlite3.SQLITE_OK
                denied.append('查询使用了未允许的函数')
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            denied.append('查询包含不允许的数据库操作')
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorizer)
        # Prepare original SQL before any optional provenance projection. This is
        # the definitive column/name/function authorization check, not AST guesses.
        plan = connection.execute('EXPLAIN QUERY PLAN '+sql, parameters).fetchall()
        execution_sql = locator['sql'] if locator else sql
        cursor = connection.execute(execution_sql, parameters)
        all_columns = [column[0] for column in cursor.description or []]
        fetched, result_bytes, truncated = [], 0, False
        for row in cursor:
            row_bytes = sum(len(value.encode('utf-8')) if isinstance(value, str) else len(value) if isinstance(value, bytes) else 16 for value in row)
            if len(fetched) >= MAX_RESULTS or result_bytes + row_bytes > MAX_RESULT_BYTES:
                truncated = True
                break
            fetched.append(row)
            result_bytes += row_bytes
            if cancelled():
                raise InterruptedError('查询已取消')
        key_count = len(locator['keys']) if locator else 0
        columns = all_columns[:-key_count] if key_count else all_columns
        raw_rows = [row[:-key_count] if key_count else row for row in fetched]
        rows = [[str(value) if isinstance(value, int) and abs(value) > 2**53-1 else value_json(value) for value in row] for row in raw_rows]
        if cancelled():
            raise InterruptedError('查询已取消')
        evidence = []
        for index, name in enumerate(sorted(used), 1):
            exact = bool(locator and locator['table'] == name)
            row_numbers = [locator['positions'].get(tuple(row[-key_count:])) for row in fetched[:MAX_EVIDENCE]] if exact else []
            exact = exact and all(number is not None for number in row_numbers)
            evidence.append({'id': f'E{index}', 'kind': 'table', 'source_id': source.get('id'), 'source_name': source.get('name'), 'sha256': source.get('sha256'), 'parser_version': source.get('parser_version'), 'table_name': name, 'label': f'{source.get("name", "资料")} · {name}', 'lineage': 'row' if exact else 'table', 'row_numbers': row_numbers if exact else [], 'columns': columns if exact else [], 'rows': rows[:MAX_EVIDENCE] if exact else [], 'total_rows': None, 'stale': False, 'note': ('所列行号对应原表稳定行序；仅列出前20个结果的来源位置。' if exact else '此查询提供表级来源范围；聚合、连接、窗口或子查询的逐行贡献未展开，不将表中抽样行当作完整计算依据。')})
        evidence = attach_table_contexts(source, evidence)
        checks = ['单条只读查询通过语法检查和SQLite权限检查', '仅访问当前资料已导入的普通数据表', '字段由SQLite严格解析，未知双引号字段不会退化为文本', '执行设置了步数、时间、单元格大小和结果行数上限']
        warnings = []
        if truncated:
            warnings.append(f'结果达到展示上限（最多{MAX_RESULTS}行或4MB），当前展示{len(rows)}行；完整结果行数未继续计算。')
        if not used:
            warnings.append('查询未引用真实数据表；常量或生成序列不能作为资料事实的来源证据。')
        native_aggregates = any(isinstance(node, (exp.Sum, exp.Avg)) for node in tree.walk())
        floating = native_aggregates or any(isinstance(node, (exp.Div, exp.Mul, exp.Add, exp.Sub)) for node in tree.walk()) or any(isinstance(value, float) for row in raw_rows for value in row)
        if floating:
            warnings.append('原生SQL算术遵循SQLite数值规则，REAL或小数文本可能产生浮点误差；SUM/AVG还会把非数值文本按0处理。金额等精确口径应使用整数最小单位或DECIMAL函数。')
        if any(isinstance(value, int) and abs(value)>2**53-1 for row in raw_rows for value in row):
            warnings.append('超过浏览器安全整数范围的结果以十进制字符串返回，避免展示时丢失末位。')
        if any(calculation.get('float_inputs') for calculation in calculations):
            warnings.append('DECIMAL聚合包含数据库REAL值；它精确累计当前读取值的字符串表示，无法恢复原文件写入REAL之前的精度。')
        if calculations:
            checks.append('DECIMAL聚合按原值字符串执行；记录了各分组的非空数量和结果，未将NULL补为0')
        if any(isinstance(value, (bytes, str)) and (isinstance(value, bytes) or len(value)>12000) for row in raw_rows for value in row):
            warnings.append('部分长文本或二进制单元格仅显示预览，结果表不是完整原值导出。')
        display_rows = _round_display_rows(tree, len(columns), rows, raw_rows)
        return {'sql': sql.strip(), 'parameters': parameters, 'columns': columns, 'rows': rows, **({'display_rows': display_rows} if display_rows is not None else {}), 'returned_rows': len(rows), 'total_rows': None if truncated else len(rows), 'matched_rows': None, 'truncated': truncated, 'used_tables': sorted(used), 'read_columns': {name: sorted(values) for name,values in reads.items()}, 'evidence': evidence, 'checks': checks, 'warnings': warnings, 'elapsed_ms': round((time.monotonic()-started)*1000), 'query_plan': [str(row[3]) for row in plan[:80]], 'calculations': calculations, 'verification': {'status': 'limited' if warnings or not evidence else 'passed', 'checks': checks, 'numeric_policy': 'SQLite原生语义；DECIMAL_SUM/MUL/ADD/SUB使用160位上下文，DECIMAL_AVG/DIV使用40位有效数字', 'lineage': 'row' if locator else 'table', 'independent_recalculation': False}}
    except sqlite3.Error as exc:
        if cancelled() or interrupted == 'cancelled':
            raise InterruptedError('查询已取消') from exc
        if interrupted == 'limit':
            raise SQLExecutionError('查询超过时间或运算步数限制，请缩小范围或简化关联', True, 'limit') from exc
        if faults:
            raise faults[0] from exc
        if denied:
            raise SQLExecutionError(denied[0], False, 'forbidden') from exc
        message = str(exc)
        if 'no such column' in message or 'ambiguous column' in message or 'no such table' in message:
            code = 'unknown_table' if 'no such table' in message else 'unknown_column'
            raise SQLExecutionError('查询的表或字段不存在或有歧义：'+message[:240], True, code) from exc
        if any(marker in message.lower() for marker in ('wrong number of arguments', 'syntax error', 'misuse of aggregate', 'misuse of window', 'incorrect number of bindings', 'did not supply a value for binding parameter', 'has no name, but you supplied a dictionary', 'named parameter, but you supplied a sequence')):
            raise SQLExecutionError('查询语法或函数参数不正确：'+message[:240], False, 'syntax') from exc
        raise SQLExecutionError('SQLite无法执行本次查询：'+message[:240], False, 'execution') from exc
    finally:
        connection.close()
