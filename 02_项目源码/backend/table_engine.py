"""Compile one-table plans into read-only SQL and independently verify the result.

Model output is data, never SQL. All identifiers come from the parsed schema;
all user values are bound. Decimal aggregates execute inside SQLite. A second
implementation checks selection, ordering and calculations against a bounded
snapshot. Source row numbers refer to this snapshot's explicit stable ordering.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import cmp_to_key

from .parsers import quoted, readonly_database, value_json

MAX_SCAN_ROWS = 50_000
MAX_SCAN_CELLS = 1_000_000
MAX_SECONDS = 12
MAX_RESULTS = 100
MAX_EVIDENCE = 20
CONTEXT_CHARS = 8_000
DIVISION_PRECISION = 28

PLAN_INSTRUCTIONS = """输出一个 JSON 单表查询计划，不要输出 SQL 或 Markdown。
格式：{"table":"真实表名","filters":[{"column":"真实列名","op":"eq|ne|in|gt|gte|lt|lte|contains","value":"筛选值"}],"select":["列名"],"group_by":["列名"],"aggregates":[{"op":"sum|avg|min|max|count","column":"数值列名","alias":"结果列名"}],"sort":[{"column":"列名或聚合别名","direction":"asc|desc"}],"limit":50}。
仅使用上下文真实表名、列名和数据值；不允许表达式、函数、SQL、跨表和跨来源。
filters 按 AND 连接，in 的 value 是数组。数值比较必须用数字；字符串 eq 保留原值（001 不等于 1）。contains 是字面包含。缺失值可用 eq:null。
select 与 aggregates 二选一。分组必须带 aggregates。count 可省略 column，表示记录数；指定 column 则统计非空值。sum/avg/min/max 要求数值列。别名不得与真实列名重名。
排序 direction 默认 asc。最多返回100行；排序须明确指标，不能把百分数当数量。百分数列的6.5表示6.5%，不能再乘100。
需要计算占比时，改用 {"table":"表名","filters":[],"ratio":{"numerator":{"column":"数值列","filters":[]},"denominator":{"column":"数值列","filters":[]},"scale":100},"limit":1}。分子、分母分别求和后相除。scale 100输出百分数，1输出比值。ratio 不和 select/group_by/aggregates/sort 并用。直接读取原表比重列与重新计算占比是不同操作，按问题选择。
合计、小计和明细或不同统计层级不得混合求和；应筛选到一个可比层级或按层级分组。元数据中的质量提示仍须保留。
不明确的列、范围、指标、层级、分母应请求澄清，不要擅自选择或修改原始数据。空值不是0。单位沿用列名，不能自行换算。
执行器使用真实只读 SQL，ZH_SUM/ZH_AVG/ZH_MIN/ZH_MAX/ZH_CMP/ZH_RATIO 是 Decimal 函数；除法以28位有效数字、ROUND_HALF_UP计算。不要自己计算最终数值。
"""


class TablePlanError(ValueError):
    def __init__(self, message: str, clarification: bool = False):
        super().__init__(message)
        self.clarification = clarification


def _error(message, clarification=False):
    raise TablePlanError(message, clarification)


def _decimal(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        _error("数值计算遇到非数值单元格；请检查所选列或缩小范围", True)
    text = str(value).strip()
    if len(text) > 120 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?", text):
        _error(f"不能把单元格「{text[:60]}」当作数值；未将其替换为0", True)
    try:
        number = Decimal(text)
    except InvalidOperation:
        _error("存在不能精确读取的数值", True)
    if not number.is_finite() or len(number.as_tuple().digits) > 60 or abs(number.as_tuple().exponent) > 60:
        _error("数值位数或指数超过安全计算范围，请缩小数值范围", True)
    return number


def _number_text(value):
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _divide(numerator, denominator, scale=1):
    if numerator is None or denominator is None:
        _error("分子或分母没有可计算的数值，请检查筛选范围", True)
    if denominator == 0:
        _error("分母为0，占比没有定义；请核对分母范围", True)
    with localcontext() as ctx:
        ctx.prec = DIVISION_PRECISION
        ctx.rounding = ROUND_HALF_UP
        return numerator / denominator * Decimal(scale)


def _nonempty(value):
    return value is not None and not (isinstance(value, str) and not value.strip())


def stable_order_for_table(connection, table_name):
    """Return a quoted ORDER BY clause shared by evidence and table previews.

    Rowid is preferred for ordinary tables. WITHOUT ROWID or shadowed-rowid
    tables use their declared primary key. No unproven physical ordering is used.
    """
    definition = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table_name,)).fetchone()
    if not definition or re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE", definition[0] or "", re.I):
        _error("只允许查询当前资料中的普通数据表")
    info = connection.execute(f"PRAGMA table_xinfo({quoted(table_name)})").fetchall()
    names = {str(row[1]).casefold() for row in info}
    # Testing the candidate also handles unusual formatting in WITHOUT ROWID SQL.
    for candidate in ("_rowid_", "rowid", "oid"):
        if candidate.casefold() in names:
            continue
        try:
            # An unquoted name intentionally avoids SQLite's double-quoted-string
            # fallback, which otherwise makes a missing rowid look valid.
            connection.execute(f"SELECT {candidate} FROM {quoted(table_name)} LIMIT 0")
            return candidate
        except sqlite3.OperationalError:
            continue
    keys = sorted((row for row in info if row[5]), key=lambda row: row[5])
    if keys:
        # Rowid-table composite primary keys can contain repeated NULLs. Their
        # declaration alone does not prove that the fallback ordering is unique.
        key_sql = ", ".join(quoted(row[1]) for row in keys)
        if connection.execute(f"SELECT 1 FROM {quoted(table_name)} GROUP BY {key_sql} HAVING COUNT(*) > 1 LIMIT 1").fetchone():
            _error("主键值不能唯一定位数据行，请导出为CSV后查询", True)
        return ", ".join(f"{quoted(row[1])} ASC" for row in keys)
    _error("此表没有可确认的稳定行标识（rowid被遮蔽且无主键）；请导出为CSV后查询", True)


def _table(source, table_name):
    if source.get("archived") or source.get("status") not in {"ready", "attention"}:
        _error("资料尚未可用或已经归档")
    if not isinstance(table_name, str):
        _error("请指定当前资料中的一张表", True)
    metadata = next((t for t in source.get("tables", []) if t.get("name") == table_name), None)
    if not metadata or not source.get("data_path"):
        _error("指定的数据表不在当前资料中", True)
    return metadata


@contextmanager
def _connection(source, table_name, cancelled=lambda: False):
    metadata = _table(source, table_name)
    deadline = time.monotonic() + MAX_SECONDS

    def budget():
        if cancelled():
            _error("查询已取消")
        if time.monotonic() > deadline:
            _error("查询超过时间限制，请缩小数据表或筛选范围", True)

    budget()
    try:
        with readonly_database(source["data_path"]) as connection:
            connection.set_progress_handler(lambda: int(cancelled() or time.monotonic() > deadline), 500)
            order = stable_order_for_table(connection, table_name)
            actual = connection.execute(f"PRAGMA table_xinfo({quoted(table_name)})").fetchall()
            columns = [row[1] for row in actual if row[6] in {0, 2, 3}]
            if columns != [c["name"] for c in metadata["columns"]]:
                _error("数据表结构与解析记录不一致，请重新导入资料")
            count = connection.execute(f"SELECT COUNT(*) FROM {quoted(table_name)}").fetchone()[0]
            if count > MAX_SCAN_ROWS or count * len(columns) > MAX_SCAN_CELLS:
                _error(f"单表核验上限为{MAX_SCAN_ROWS:,}行、{MAX_SCAN_CELLS:,}个单元格，请拆分资料后查询", True)
            budget()
            yield connection, metadata, columns, order, count, budget
    except sqlite3.Error as exc:
        budget()
        raise TablePlanError("数据表查询失败；请检查文件完整性或重新导入") from exc


def context_for_table(source: dict, table_name: str) -> dict:
    """Bounded schema and actual-value context. No fabricated type/domain guesses."""
    with _connection(source, table_name) as (connection, metadata, columns, order, count, budget):
        selected = columns[:64]
        preview = connection.execute(f"SELECT {','.join(quoted(c) for c in selected)} FROM {quoted(table_name)} ORDER BY {order} LIMIT 5").fetchall()
        context = {
            "table": table_name,
            "row_count": count,
            "columns": [],
            "preview": [[_context_value(v) for v in row] for row in preview],
            "warnings": [str(w)[:500] for w in source.get("warnings", [])[:6]],
            "source_note": str(source.get("metadata", {}).get("note", ""))[:1000],
            "row_locator": "按稳定顺序编号的解析表数据行，不代表原文件物理行号",
            "schema_truncated": len(selected) < len(columns),
        }
        for column in metadata["columns"][:64]:
            budget()
            values = connection.execute(f"SELECT DISTINCT {quoted(column['name'])} FROM {quoted(table_name)} ORDER BY {order} LIMIT 24").fetchall()
            context["columns"].append({"name": column["name"], "type": column["type"], "sample_values": [_context_value(r[0]) for r in values]})
        # Preserve exact identifiers, progressively remove samples before schema.
        while len(json.dumps(context, ensure_ascii=False)) > CONTEXT_CHARS:
            budget()
            candidates = [c for c in context["columns"] if c.get("sample_values")]
            if candidates:
                max(candidates, key=lambda c: len(json.dumps(c["sample_values"], ensure_ascii=False)))["sample_values"].pop()
            elif context["preview"]:
                context["preview"].pop()
            elif len(context["columns"]) > 1:
                context["columns"].pop()
                context["schema_truncated"] = True
            elif context["warnings"]:
                context["warnings"].pop()
            else:
                _error("表名或字段名过长，无法形成有界查询上下文", True)
        return context


def _context_value(value):
    value = value_json(value)
    return value[:160] + "…" if isinstance(value, str) and len(value) > 160 else value


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        _error(f"{label}含不支持的结构或字段，请重新组织问题", True)


def _list(value, maximum, label):
    if not isinstance(value, list) or len(value) > maximum:
        _error(f"{label}必须是最多{maximum}项的列表", True)
    return value


def _value(value):
    if value is None or isinstance(value, str) and len(value) <= 2000:
        return value
    if type(value) is int or type(value) is float and math.isfinite(value):
        _decimal(value)
        return value
    _error("筛选值格式不受支持", True)


def _filters(filters, columns):
    output = []
    for item in _list(filters, 16, "筛选条件"):
        _keys(item, {"column", "op", "value"}, "筛选条件")
        column, op = item.get("column"), item.get("op")
        if column not in columns or not isinstance(op, str) or op not in {"eq", "ne", "in", "gt", "gte", "lt", "lte", "contains"} or "value" not in item:
            _error("筛选列或操作无效，请使用当前表中的列", True)
        value = item["value"]
        if op == "in":
            value = [_value(v) for v in _list(value, 50, "筛选值")]
            if not value:
                _error("in筛选需要至少一个值", True)
        else:
            value = _value(value)
        if op in {"gt", "gte", "lt", "lte"}:
            if _decimal(value) is None:
                _error("数值比较需要有效数字", True)
        if op == "contains" and not isinstance(value, str):
            _error("包含筛选需要文字", True)
        output.append({"column": column, "op": op, "value": value})
    return output


def _plan(plan, columns):
    _keys(plan, {"table", "filters", "select", "group_by", "aggregates", "sort", "limit", "ratio"}, "查询计划")
    result = {"table": plan["table"], "filters": _filters(plan.get("filters", []), columns)}
    limit = plan.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        _error("结果条数必须为1至100", True)
    result["limit"] = limit
    if "ratio" in plan:
        if any(plan.get(field) for field in ("select", "group_by", "aggregates", "sort")):
            _error("占比计划不能同时进行投影、分组、聚合或排序", True)
        ratio = plan["ratio"]
        _keys(ratio, {"numerator", "denominator", "scale"}, "占比")
        result["ratio"] = {"scale": ratio.get("scale", 100)}
        if type(result["ratio"]["scale"]) is not int or result["ratio"]["scale"] not in {1, 100}:
            _error("占比scale只支持1（比值）或100（百分数）", True)
        for side in ("numerator", "denominator"):
            value = ratio.get(side)
            _keys(value, {"column", "filters"}, "占比范围")
            if value.get("column") not in columns:
                _error("占比的分子和分母必须指定真实数值列", True)
            result["ratio"][side] = {"column": value["column"], "filters": _filters(value.get("filters", []), columns)}
        return result
    groups = _list(plan.get("group_by", []), 3, "分组列")
    if any(not isinstance(c, str) or c not in columns for c in groups) or len(set(groups)) != len(groups):
        _error("分组列无效或重复", True)
    result["group_by"] = groups
    aggregates = []
    for item in _list(plan.get("aggregates", []), 8, "聚合项"):
        _keys(item, {"op", "column", "alias"}, "聚合项")
        op, column, alias = item.get("op"), item.get("column"), item.get("alias")
        if not isinstance(op, str) or op not in {"sum", "avg", "min", "max", "count"} or (column not in columns and not (op == "count" and column is None)):
            _error("聚合操作或数值列无效", True)
        if not isinstance(alias, str) or not alias.strip() or len(alias) > 80 or alias.casefold() in {c.casefold() for c in columns} or any(a["alias"].casefold() == alias.casefold() for a in aggregates):
            _error("聚合别名需非空、唯一且不与原表列名相同", True)
        aggregates.append({"op": op, "column": column, "alias": alias})
    if groups and not aggregates:
        _error("分组查询需要同时指定统计项", True)
    if aggregates and plan.get("select"):
        _error("聚合查询请用group_by列，不能同时指定select", True)
    select = _list(plan.get("select", [] if aggregates else columns[:32]), 32, "结果列")
    if not aggregates and not select or any(not isinstance(c, str) or c not in columns for c in select) or len(set(select)) != len(select):
        _error("结果列无效、为空或重复", True)
    result.update(select=select, aggregates=aggregates)
    available = groups + [a["alias"] for a in aggregates] if aggregates else columns
    sorts = []
    for item in _list(plan.get("sort", []), 4, "排序项"):
        _keys(item, {"column", "direction"}, "排序项")
        column, direction = item.get("column"), item.get("direction", "asc")
        if column not in available or not isinstance(direction, str) or direction not in {"asc", "desc"}:
            _error("排序列或方向无效", True)
        sorts.append({"column": column, "direction": direction})
    result["sort"] = sorts
    return result


def _filter_sql(filters, parameters):
    def single(column, op, value):
        name = quoted(column)
        if value is None:
            return f"{name} IS {'NOT ' if op == 'ne' else ''}NULL"
        if op == "contains":
            parameters.append(value)
            return f"INSTR(CAST({name} AS TEXT), ?) > 0"
        if op in {"gt", "gte", "lt", "lte"} or type(value) in {int, float}:
            parameters.append(_number_text(_decimal(value)))
            comparison = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            return f"ZH_CMP({name}, ?) {comparison} 0"
        parameters.append(value)
        return f"CAST({name} AS TEXT) COLLATE BINARY {'!=' if op == 'ne' else '='} ?"
    parts = []
    for item in filters:
        if item["op"] == "in":
            parts.append("(" + " OR ".join(single(item["column"], "eq", v) for v in item["value"]) + ")")
        else:
            parts.append(single(item["column"], item["op"], item["value"]))
    return " AND ".join(parts) or "1=1"


def _matches(values, filters):
    def single(actual, op, wanted):
        if wanted is None:
            return actual is not None if op == "ne" else actual is None
        if actual is None:
            return False
        if op == "contains":
            return str(wanted) in str(actual)
        if op in {"gt", "gte", "lt", "lte"} or type(wanted) in {int, float}:
            left, right = _decimal(actual), _decimal(wanted)
            if left is None or right is None:
                return False
            return {"eq": left == right, "ne": left != right, "gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
        return str(actual) != wanted if op == "ne" else str(actual) == wanted
    for item in filters:
        actual = values[item["column"]]
        match = any(single(actual, "eq", v) for v in item["value"]) if item["op"] == "in" else single(actual, item["op"], item["value"])
        if not match:
            return False
    return True


def _register(connection):
    errors = []

    def guarded(function):
        def wrapped(*args):
            try:
                return function(*args)
            except TablePlanError as exc:
                errors.append(exc)
                raise
        return wrapped

    def compare(a, b):
        left, right = _decimal(a), _decimal(b)
        return None if left is None or right is None else (left > right) - (left < right)

    connection.create_function("ZH_CMP", 2, guarded(compare), deterministic=True)
    connection.create_function("ZH_DECIMAL", 1, guarded(lambda value: _number_text(_decimal(value))), deterministic=True)
    connection.create_function("ZH_RATIO", 3, guarded(lambda a, b, scale: _number_text(_divide(_decimal(a), _decimal(b), scale))), deterministic=True)
    connection.create_collation("ZH_DECIMAL", lambda a, b: compare(a, b) or 0)

    def aggregate_class(operation):
        class Aggregate:
            def __init__(self):
                self.total = Decimal(0)
                self.count = 0
                self.extreme = None

            def step(self, value):
                try:
                    if operation == "count":
                        self.count += _nonempty(value)
                        return
                    number = _decimal(value)
                    if number is None:
                        return
                    self.count += 1
                    if operation in {"sum", "avg"}:
                        with localcontext() as context:
                            context.prec = 100
                            self.total += number
                    elif self.extreme is None or (operation == "min" and number < self.extreme) or (operation == "max" and number > self.extreme):
                        self.extreme = number
                except TablePlanError as exc:
                    errors.append(exc)
                    raise

            def finalize(self):
                if operation == "count":
                    return self.count
                if not self.count:
                    return None
                result = _divide(self.total, Decimal(self.count)) if operation == "avg" else self.total if operation == "sum" else self.extreme
                return _number_text(result)
        return Aggregate

    for operation in ("sum", "avg", "min", "max", "count"):
        connection.create_aggregate("ZH_" + operation.upper(), 1, aggregate_class(operation))
    return errors


def _sum(values):
    numbers = [_decimal(v) for v in values if _nonempty(v)]
    if not numbers:
        return None
    with localcontext() as ctx:
        ctx.prec = 100
        return sum(numbers, Decimal(0))


def _aggregate_python(records, item):
    op, column = item["op"], item["column"]
    if op == "count":
        return len(records) if column is None else sum(_nonempty(r["values"][column]) for r in records)
    values = [_decimal(r["values"][column]) for r in records if _nonempty(r["values"][column])]
    if not values:
        return None
    if op in {"sum", "avg"}:
        total = _sum(values)
        result = total if op == "sum" else _divide(total, Decimal(len(values)))
    else:
        result = min(values) if op == "min" else max(values)
    return _number_text(result)


def _hierarchy_guard(records, columns, source, groups=()):
    if len(records) < 2:
        return
    note = " ".join(str(v) for v in [source.get("metadata", {}).get("note", ""), *source.get("warnings", [])])
    explicit = bool(re.search(r"(?:混合|层级|重复).{0,25}(?:求和|累加|汇总)|(?:求和|累加|汇总).{0,25}(?:混合|层级|重复)", note))
    total = re.compile(r"^(?:合计|总计|小计|总数|总量|全部|总额|total|subtotal|grand total|all)$", re.I)
    for column in columns:
        if column in groups:
            continue
        values = {str(r["values"][column]).strip() for r in records if _nonempty(r["values"][column])}
        if len(values) <= 1:
            continue
        named_level = bool(re.search(r"层级|层次|分类|口径|level|hierarchy", column, re.I))
        if (explicit and named_level) or any(total.fullmatch(v) for v in values):
            choices = "、".join(sorted(values)[:8])
            _error(f"「{column}」包含不同统计层级（{choices}），直接汇总可能重复计数；请筛选一个层级或按该列分组", True)


def _sort_kind(column, records, metadata, aggregate_aliases):
    if column in aggregate_aliases:
        return "numeric"
    values = [r["values"].get(column) for r in records if _nonempty(r["values"].get(column))]
    if not values:
        return "text"
    try:
        for value in values:
            _decimal(value)
        return "numeric"
    except TablePlanError:
        declared = next((str(c["type"]) for c in metadata["columns"] if c["name"] == column), "")
        if re.search(r"整数|小数|INT|REAL|NUM|DEC|FLOAT|DOUBLE", declared, re.I):
            _error(f"排序列「{column}」混入非数值内容，不能按数值排序", True)
        if any(isinstance(value, (bytes, dict, list)) for value in values):
            _error("二进制或复杂单元格不能用于排序", True)
        return "text"


def _compare_values(a, b, kind):
    if kind == "numeric":
        a, b = _decimal(a), _decimal(b)
    if a is None or b is None:
        return 0 if a is b else -1 if a is None else 1
    if kind == "numeric":
        return (a > b) - (a < b)
    # SQLite sorts storage classes before values. Reproduce its BINARY ordering.
    def key(value):
        return (0, value) if isinstance(value, (int, float)) else (1, str(value))
    return (key(a) > key(b)) - (key(a) < key(b))


def _calculation(item, records, result, group):
    values = [] if item["column"] is None else [r["values"][item["column"]] for r in records]
    nonempty = len(records) if item["column"] is None else sum(_nonempty(v) for v in values)
    return {
        "operation": item["op"], "column": item["column"], "group": group,
        "expression": f"{item['op'].upper()}({item['column'] or '*'})",
        "inputs": {"matched_rows": len(records), "nonempty_values": nonempty, "missing_values": len(records) - nonempty},
        "result": result, "verified": True,
        "precision": DIVISION_PRECISION if item["op"] == "avg" else "Decimal精确汇总",
        "rounding": "ROUND_HALF_UP" if item["op"] == "avg" else None,
    }


def execute_plan(source: dict, plan: dict, cancelled=lambda: False) -> dict:
    """Execute compiled SQL, reject unsupported plans, return verified lineage."""
    if not isinstance(plan, dict):
        _error("查询计划必须为对象", True)
    table_name = plan.get("table")
    with _connection(source, table_name, cancelled) as (connection, metadata, columns, order, count, budget):
        validated = _plan(plan, columns)
        errors = _register(connection)
        row_alias = "__zh_row_number"
        while row_alias.casefold() in {column.casefold() for column in columns}:
            row_alias += "_"
        table_sql = f"main.{quoted(table_name)}"
        cte = f"WITH \"__zh_input\" AS (SELECT ROW_NUMBER() OVER (ORDER BY {order}) AS {quoted(row_alias)}, {', '.join(quoted(c) for c in columns)} FROM {table_sql})"
        snapshot_sql = f"{cte} SELECT * FROM \"__zh_input\" ORDER BY {quoted(row_alias)}"
        snapshot = []
        for row in connection.execute(snapshot_sql):
            if len(snapshot) % 128 == 0:
                budget()
            snapshot.append({"row_number": row[0], "values": dict(zip(columns, row[1:]))})
        if len(snapshot) != count:
            _error("查询过程中数据表发生变化，请重新导入")

        def run(sql, parameters=()):
            budget()
            try:
                return connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as exc:
                if errors:
                    raise errors[-1] from exc
                budget()
                raise TablePlanError("SQL执行未成功，未生成可信结果") from exc

        def matching(filters):
            parameters = []
            where = _filter_sql(filters, parameters)
            sql_ids = [r[0] for r in run(f"{cte} SELECT {quoted(row_alias)} FROM \"__zh_input\" WHERE {where} ORDER BY {quoted(row_alias)}", parameters)]
            expected = []
            for index, record in enumerate(snapshot):
                if index % 128 == 0:
                    budget()
                if _matches(record["values"], filters):
                    expected.append(record)
            if sql_ids != [r["row_number"] for r in expected]:
                _error("SQL筛选与独立复核不一致，已停止返回答案")
            return expected, where, parameters

        warnings = list(dict.fromkeys(str(w) for w in source.get("warnings", [])))
        checks = ["SQL仅访问指定表，表列名白名单校验、值参数绑定、只读连接", "完整匹配行已通过独立筛选复核", "来源行按稳定顺序编号，可回看解析表"]
        if "ratio" in validated:
            output = _execute_ratio(validated, source, columns, cte, matching, run, warnings)
        else:
            output = _execute_standard(validated, source, metadata, columns, row_alias, cte, matching, run, warnings)
        budget()
        output["checks"] = checks + ["结果顺序与Decimal计算已通过独立复核"]
        # A query's Top-N limit is not itself a failure to answer the question.
        # Keep the legacy omission flag, alongside counts at each distinct level.
        output["limit"] = validated["limit"]
        output["returned_rows"] = len(output["rows"])
        output.setdefault("result_rows_before_limit", 1)  # Ratio has one result.
        output["warnings"] = list(dict.fromkeys(output["warnings"]))
        return output


def _execute_standard(plan, source, metadata, columns, row_alias, cte, matching, run, warnings):
    records, where, parameters = matching(plan["filters"])
    aggregates, groups = plan["aggregates"], plan["group_by"]
    aliases = {a["alias"] for a in aggregates}
    sort_specs = [{**s, "kind": _sort_kind(s["column"], records, metadata, aliases)} for s in plan["sort"]]
    calculations = []
    if aggregates:
        grouped = {}
        for record in records:
            key = tuple(record["values"][g] for g in groups)
            grouped.setdefault(key, []).append(record)
        if not groups and not records:
            grouped[()] = []
        result_columns = groups + [a["alias"] for a in aggregates]
        select = [quoted(g) for g in groups]
        for item in aggregates:
            expr = "COUNT(*)" if item["op"] == "count" and item["column"] is None else f"{'COALESCE(' if item['op'] == 'count' else ''}ZH_{item['op'].upper()}({quoted(item['column'])}){', 0)' if item['op'] == 'count' else ''}"
            select.append(f"{expr} AS {quoted(item['alias'])}")
        expected = []
        for key, members in grouped.items():
            if any(a["op"] in {"sum", "avg"} for a in aggregates):
                _hierarchy_guard(members, columns, source, groups)
            values = dict(zip(groups, key))
            for item in aggregates:
                value = _aggregate_python(members, item)
                values[item["alias"]] = value
                if item["column"] and any(not _nonempty(r["values"][item["column"]]) for r in members):
                    warning = f"「{item['column']}」有空值；{item['op']}仅使用非空值，未将空值当作0"
                    if warning not in warnings:
                        warnings.append(warning)
            expected.append({"values": values, "members": members})
        group_sql = " GROUP BY " + ", ".join(quoted(g) + " COLLATE BINARY" for g in groups) if groups else ""
        stable_tail = [f"{quoted(g)} COLLATE BINARY ASC" for g in groups]
    else:
        result_columns = plan["select"]
        select = [quoted(c) for c in result_columns]
        expected = [{"values": r["values"], "members": [r], "row_number": r["row_number"]} for r in records]
        group_sql = ""
        stable_tail = [quoted(row_alias) + " ASC"]

    def compare(left, right):
        for spec in sort_specs:
            result = _compare_values(left["values"][spec["column"]], right["values"][spec["column"]], spec["kind"])
            if result:
                return -result if spec["direction"] == "desc" else result
        for group in groups:
            result = _compare_values(left["values"][group], right["values"][group], "text")
            if result:
                return result
        return (left.get("row_number", 0) > right.get("row_number", 0)) - (left.get("row_number", 0) < right.get("row_number", 0))

    expected.sort(key=cmp_to_key(compare))
    order_items = [(f"ZH_DECIMAL({quoted(s['column'])}) COLLATE ZH_DECIMAL" if s["kind"] == "numeric" else quoted(s["column"]) + " COLLATE BINARY") + " " + s["direction"].upper() for s in sort_specs]
    order_items += stable_tail
    order_sql = " ORDER BY " + ", ".join(order_items) if order_items else ""
    sql = f"{cte} SELECT {', '.join(select)} FROM \"__zh_input\" WHERE {where}{group_sql}{order_sql} LIMIT ?"
    query_parameters = parameters + [plan["limit"] + 1]
    actual = run(sql, query_parameters)
    expected_rows = [[r["values"].get(c) for c in result_columns] for r in expected[:plan["limit"] + 1]]
    if [list(row) for row in actual] != expected_rows:
        _error("SQL结果与独立复核不一致，已停止返回答案")
    returned = expected[:plan["limit"]]
    # Calculation descriptions are for displayed groups only. Retaining one
    # verbose record for every aggregate in every hidden group wastes memory.
    for result in returned:
        for item in aggregates:
            calculations.append(_calculation(item, result["members"], result["values"][item["alias"]], {g: result["values"][g] for g in groups}))
    evidence = []
    for item in returned:
        for member in item["members"]:
            if len(evidence) < MAX_EVIDENCE:
                evidence.append(member)
    if len(records) > len(evidence):
        warnings.append(f"完整匹配{len(records)}行，来源区仅展示{len(evidence)}行样例；查询已检查全部匹配行")
    if any(re.search(r"pct|百分|比重|占比|率|%", c, re.I) for c in result_columns):
        warnings.append("百分数列保留原表数值与口径，例如6.5表示6.5%；未再次乘100")
    if any(a["op"] == "avg" for a in aggregates):
        warnings.append("平均值为所选非空数值的算术平均，未按业务权重加权；除法使用28位有效数字")
    return {"columns": result_columns, "rows": [[value_json(v) for v in row] for row in actual[:plan["limit"]]], "sql": sql, "parameters": query_parameters, "matched_rows": len(records), "result_rows_before_limit": len(expected), "truncated": len(actual) > plan["limit"], "evidence_rows": [{"row_number": r["row_number"], "values": {k: value_json(v) for k, v in r["values"].items()}} for r in evidence], "calculations": calculations, "warnings": warnings}


def _execute_ratio(plan, source, columns, cte, matching, run, warnings):
    ratio = plan["ratio"]
    scopes = {}
    sql_parts = []
    parameters = []
    for side in ("numerator", "denominator"):
        item = ratio[side]
        records, where, bound = matching(plan["filters"] + item["filters"])
        _hierarchy_guard(records, columns, source)
        total = _sum([r["values"][item["column"]] for r in records])
        scopes[side] = {"records": records, "total": total, "column": item["column"]}
        sql_parts.append(f"\"__zh_{side}\" AS (SELECT ZH_SUM({quoted(item['column'])}) AS value FROM \"__zh_input\" WHERE {where})")
        parameters.extend(bound)
        if any(not _nonempty(r["values"][item["column"]]) for r in records):
            warnings.append(f"{side}所用列「{item['column']}」存在空值；求和跳过空值，未将其当作0")
    value = _divide(scopes["numerator"]["total"], scopes["denominator"]["total"], ratio["scale"])
    label = "占比_pct" if ratio["scale"] == 100 else "比值"
    sql = cte + ", " + ", ".join(sql_parts) + f' SELECT n.value AS "分子", d.value AS "分母", ZH_RATIO(n.value, d.value, ?) AS {quoted(label)} FROM "__zh_numerator" AS n CROSS JOIN "__zh_denominator" AS d'
    parameters.append(ratio["scale"])
    actual = run(sql, parameters)
    expected = [_number_text(scopes["numerator"]["total"]), _number_text(scopes["denominator"]["total"]), _number_text(value)]
    if [list(r) for r in actual] != [expected]:
        _error("占比SQL与独立Decimal复核不一致，已停止返回答案")
    all_records = {r["row_number"]: r for scope in scopes.values() for r in scope["records"]}
    evidence = sorted(all_records.values(), key=lambda r: r["row_number"])[:MAX_EVIDENCE]
    warnings.append("占比为所选数据重新计算的结果；28位有效数字、ROUND_HALF_UP，不替换原表发布的比重或增长率")
    if len(all_records) > MAX_EVIDENCE:
        warnings.append(f"计算涉及{len(all_records)}行，来源区仅展示前{MAX_EVIDENCE}行样例")
    calculation = {"operation": "ratio", "expression": f"{expected[0]} / {expected[1]} × {ratio['scale']}", "result": expected[2], "inputs": {side: {"column": scope["column"], "matched_rows": len(scope["records"]), "value": _number_text(scope["total"])} for side, scope in scopes.items()}, "precision": DIVISION_PRECISION, "rounding": "ROUND_HALF_UP", "verified": True}
    return {"columns": ["分子", "分母", label], "rows": [expected], "sql": sql, "parameters": parameters, "matched_rows": len(all_records), "truncated": False, "evidence_rows": [{"row_number": r["row_number"], "values": {k: value_json(v) for k, v in r["values"].items()}} for r in evidence], "calculations": [calculation], "warnings": warnings}
