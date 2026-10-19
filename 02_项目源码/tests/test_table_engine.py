"""Single-table SQL, Decimal and source-row invariants on independent fixtures."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from backend import parsers
from backend.table_engine import TablePlanError, context_for_table, execute_plan, stable_order_for_table


def source_for(path, fmt="sqlite", metadata=None):
    tables, _ = parsers.sqlite_tables(path, observe_types=fmt != "sqlite")
    return {"id": "fixture", "name": path.name, "status": "ready", "archived": False,
            "data_path": str(path), "format": fmt, "tables": tables,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "parser_version": parsers.PARSER_VERSION, "metadata": metadata or {}, "warnings": []}


@pytest.fixture
def business(tmp_path):
    path = tmp_path / "business.sqlite"
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE "sales" ("code" TEXT, "region" TEXT, "amount" TEXT, "quantity" INTEGER, "margin_pct" TEXT)')
        c.executemany('INSERT INTO sales VALUES(?,?,?,?,?)', [
            ("001", "东", "0.10", 2, "5.5"), ("002", "东", "0.20", 3, "8.0"),
            ("003", "西", "12.80", 5, "-1.5"), ("004", "西", None, 4, None),
            ("005", "北", "2.00", 1, "0"),
        ])
    return source_for(path)


def test_actual_sql_filter_sort_and_original_row_locators(business):
    result = execute_plan(business, {"table": "sales", "select": ["code", "amount"],
        "filters": [{"column": "amount", "op": "gt", "value": 0.15}],
        "sort": [{"column": "amount", "direction": "desc"}], "limit": 2})
    assert result["rows"] == [["003", "12.80"], ["005", "2.00"]]
    assert result["matched_rows"] == 3 and result["truncated"] is True
    assert (result["result_rows_before_limit"], result["returned_rows"], result["limit"]) == (3, 2, 2)
    assert [r["row_number"] for r in result["evidence_rows"]] == [3, 5]
    assert "WHERE ZH_CMP" in result["sql"] and "ORDER BY ZH_DECIMAL" in result["sql"]
    assert result["parameters"] == ["0.15", 3]
    assert result["calculations"] == []


def test_exact_decimal_sum_and_independent_check(business):
    result = execute_plan(business, {"table": "sales", "filters": [{"column": "region", "op": "eq", "value": "东"}],
        "aggregates": [{"op": "sum", "column": "amount", "alias": "总金额"}]})
    assert result["rows"] == [["0.3"]]
    assert result["calculations"][0]["verified"] is True
    assert result["calculations"][0]["inputs"] == {"matched_rows": 2, "nonempty_values": 2, "missing_values": 0}
    assert "ZH_SUM" in result["sql"]


def test_group_sum_count_avg_and_sort(business):
    result = execute_plan(business, {"table": "sales", "group_by": ["region"],
        "aggregates": [{"op": "sum", "column": "amount", "alias": "sum_amount"}, {"op": "avg", "column": "amount", "alias": "avg_amount"}, {"op": "count", "column": "amount", "alias": "values"}, {"op": "count", "alias": "records"}],
        "sort": [{"column": "sum_amount", "direction": "desc"}]})
    assert result["rows"] == [["西", "12.8", "12.8", 1, 2], ["北", "2", "2", 1, 1], ["东", "0.3", "0.15", 2, 2]]
    assert result["matched_rows"] == 5
    assert "GROUP BY" in result["sql"]
    assert any("空值" in w for w in result["warnings"])


def test_percent_is_not_rescaled(business):
    result = execute_plan(business, {"table": "sales", "select": ["margin_pct"], "filters": [{"column": "code", "op": "eq", "value": "001"}]})
    assert result["rows"] == [["5.5"]]
    assert any("6.5表示6.5%" in w for w in result["warnings"])


def test_grouped_top_n_counts_separate_input_and_result_rows(business):
    result = execute_plan(business, {"table": "sales", "group_by": ["region"],
        "aggregates": [{"op": "sum", "column": "amount", "alias": "total"}],
        "sort": [{"column": "total", "direction": "desc"}], "limit": 2})
    assert result["rows"] == [["西", "12.8"], ["北", "2"]]
    assert result["matched_rows"] == 5
    assert result["result_rows_before_limit"] == 3
    assert result["returned_rows"] == 2 and result["limit"] == 2
    assert result["truncated"] is True  # Other groups exist; Top-2 still answered.


def test_ratio_executes_decimal_sql(business):
    result = execute_plan(business, {"table": "sales", "ratio": {
        "numerator": {"column": "amount", "filters": [{"column": "code", "op": "eq", "value": "001"}]},
        "denominator": {"column": "amount", "filters": [{"column": "region", "op": "eq", "value": "东"}]}, "scale": 100}})
    assert result["rows"] == [["0.1", "0.3", "33.33333333333333333333333333"]]
    assert result["matched_rows"] == 2
    assert result["returned_rows"] == result["result_rows_before_limit"] == 1
    assert result["calculations"][0]["verified"]
    assert "ZH_RATIO" in result["sql"] and "ZH_SUM" in result["sql"]
    assert [r["row_number"] for r in result["evidence_rows"]] == [1, 2]


def test_count_min_max_and_empty_results(business):
    result = execute_plan(business, {"table": "sales", "aggregates": [{"op": "min", "column": "quantity", "alias": "最低"}, {"op": "max", "column": "amount", "alias": "最高"}]})
    assert result["rows"] == [["1", "12.8"]]
    empty = execute_plan(business, {"table": "sales", "filters": [{"column": "region", "op": "eq", "value": "不存在"}], "aggregates": [{"op": "sum", "column": "amount", "alias": "总金额"}, {"op": "count", "column": "amount", "alias": "个数"}, {"op": "count", "alias": "行数"}]})
    assert empty["rows"] == [[None, 0, 0]] and empty["matched_rows"] == 0
    plain = execute_plan(business, {"table": "sales", "filters": [{"column": "region", "op": "eq", "value": "不存在"}]})
    assert plain["rows"] == []


@pytest.mark.parametrize("op,value,expected", [("eq", "001", ["001"]), ("eq", 1, ["001"]), ("in", ["001", "005"], ["001", "005"]), ("ne", "东", ["003", "004", "005"]), ("contains", "西", ["003", "004"]), ("eq", None, ["004"]), ("ne", None, ["001", "002", "003", "005"])])
def test_filter_semantics(business, op, value, expected):
    column = "code" if op == "in" or value in ("001", 1) else "amount" if value is None else "region"
    result = execute_plan(business, {"table": "sales", "select": ["code"], "filters": [{"column": column, "op": op, "value": value}]})
    assert [row[0] for row in result["rows"]] == expected


def test_missing_and_invalid_numeric_are_never_zero(tmp_path):
    path = tmp_path / "dirty.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE amounts (value TEXT)")
        c.executemany("INSERT INTO amounts VALUES(?)", [("1.2",), ("",), (None,), ("待核实",)])
    source = source_for(path)
    with pytest.raises(TablePlanError, match="未将其替换为0"):
        execute_plan(source, {"table": "amounts", "aggregates": [{"op": "sum", "column": "value", "alias": "total"}]})
    with pytest.raises(TablePlanError, match="未将其替换为0"):
        execute_plan(source, {"table": "amounts", "filters": [{"column": "value", "op": "gt", "value": 0}]})


def test_zero_and_missing_denominator_are_explicit(business):
    for filters, message in [([{"column": "code", "op": "eq", "value": "005"}], "分母为0"), ([{"column": "code", "op": "eq", "value": "004"}], "没有可计算")]:
        with pytest.raises(TablePlanError, match=message):
            execute_plan(business, {"table": "sales", "ratio": {"numerator": {"column": "quantity"}, "denominator": {"column": "margin_pct", "filters": filters}}})


def test_total_detail_mixed_aggregation_requires_clarification(tmp_path):
    path = tmp_path / "hierarchy.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE stats (层级 TEXT, 类目 TEXT, 金额 TEXT)")
        c.executemany("INSERT INTO stats VALUES(?,?,?)", [("合计", "全区", "30"), ("产业", "制造", "10"), ("产业", "服务", "20")])
    source = source_for(path, metadata={"note": "不同层级不可混合求和"})
    aggregate = [{"op": "sum", "column": "金额", "alias": "合计金额"}]
    with pytest.raises(TablePlanError) as error:
        execute_plan(source, {"table": "stats", "aggregates": aggregate})
    assert error.value.clarification
    grouped = execute_plan(source, {"table": "stats", "group_by": ["层级"], "aggregates": aggregate})
    assert sorted(grouped["rows"]) == [["产业", "30"], ["合计", "30"]]
    counts = execute_plan(source, {"table": "stats", "aggregates": [{"op": "count", "alias": "记录数"}]})
    assert counts["rows"] == [[3]]


def test_without_rowid_uses_composite_primary_key(tmp_path):
    path = tmp_path / "keys.sqlite"
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE facts (city TEXT, year INTEGER, value TEXT, PRIMARY KEY(city,year)) WITHOUT ROWID')
        c.executemany("INSERT INTO facts VALUES(?,?,?)", [("b", 2024, "12"), ("a", 2025, "9"), ("a", 2024, "4")])
        assert stable_order_for_table(c, "facts") == '"city" ASC, "year" ASC'
    result = execute_plan(source_for(path), {"table": "facts", "select": ["city", "year"], "filters": [{"column": "year", "op": "eq", "value": 2025}]})
    assert result["rows"] == [["a", 2025]]
    assert result["evidence_rows"][0]["row_number"] == 2


def test_shadowed_rowid_without_key_rejected(tmp_path):
    path = tmp_path / "shadow.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE shadow (rowid TEXT, _rowid_ TEXT, oid TEXT)")
    with pytest.raises(TablePlanError, match="稳定行标识"):
        execute_plan(source_for(path), {"table": "shadow"})


def test_shadowed_rowid_nullable_primary_key_does_not_prove_unique_rows(tmp_path):
    path = tmp_path / "shadow_pk.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE shadow (rowid TEXT, _rowid_ TEXT, oid TEXT, a TEXT, b TEXT, PRIMARY KEY(a,b))")
        c.executemany("INSERT INTO shadow VALUES(?,?,?,?,?)", [("x", "y", "z", None, "1"), ("q", "r", "s", None, "1")])
    with pytest.raises(TablePlanError, match="不能唯一定位"):
        execute_plan(source_for(path), {"table": "shadow"})


def test_literal_matching_and_grouping_override_source_nocase_collation(tmp_path):
    path = tmp_path / "collation.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE words (word TEXT COLLATE NOCASE, value TEXT, __ZH_ROW_NUMBER TEXT)")
        c.executemany("INSERT INTO words VALUES(?,?,?)", [("East", "1", "x"), ("east", "2", "y")])
    source = source_for(path)
    selected = execute_plan(source, {"table": "words", "filters": [{"column": "word", "op": "eq", "value": "East"}]})
    assert len(selected["rows"]) == 1
    grouped = execute_plan(source, {"table": "words", "group_by": ["word"], "aggregates": [{"op": "sum", "column": "value", "alias": "total"}]})
    assert grouped["rows"] == [["East", "1"], ["east", "2"]]


def test_large_integer_plan_value_is_a_controlled_error(business):
    with pytest.raises(TablePlanError):
        execute_plan(business, {"table": "sales", "filters": [{"column": "quantity", "op": "eq", "value": 10 ** 300}]})


def test_context_contains_real_values_and_is_bounded(business):
    context = context_for_table(business, "sales")
    assert context["row_count"] == 5 and not context["schema_truncated"]
    assert context["columns"][0]["sample_values"][0] == "001"
    assert len(json.dumps(context, ensure_ascii=False)) <= 8000


def test_result_and_evidence_limits_and_true_match_count(tmp_path):
    path = tmp_path / "many.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE measurements (n INTEGER, value TEXT)")
        c.executemany("INSERT INTO measurements VALUES(?,?)", [(n, "0.1") for n in range(137)])
    source = source_for(path)
    result = execute_plan(source, {"table": "measurements", "select": ["n"], "limit": 100})
    assert len(result["rows"]) == 100 and len(result["evidence_rows"]) == 20
    assert result["matched_rows"] == 137 and result["truncated"]
    summed = execute_plan(source, {"table": "measurements", "aggregates": [{"op": "sum", "column": "value", "alias": "total"}]})
    assert summed["rows"] == [["13.7"]] and summed["matched_rows"] == 137
    assert len(summed["evidence_rows"]) == 20 and not summed["truncated"]


@pytest.mark.parametrize("extra", [{"sql": "DROP TABLE sales"}, {"select": ["amount); DROP TABLE sales;--"]}, {"table": "sqlite_schema"}, {"limit": 101}, {"limit": True}, {"filters": [{"column": "amount", "op": "eq", "value": {"sql": "1"}}]}, {"filters": [{"column": "amount", "op": [], "value": 1}]}, {"sort": [{"column": "amount", "direction": []}]}, {"aggregates": [{"op": {}, "column": "amount", "alias": "x"}]}, {"aggregates": [{"op": "sum", "column": "amount", "alias": "AMOUNT"}]}, {"sort": [{"column": "amount", "direction": "DESC; DROP TABLE sales"}]}])
def test_plan_injection_and_unknown_fields_rejected(business, extra):
    before = Path(business["data_path"]).read_bytes()
    with pytest.raises(TablePlanError):
        execute_plan(business, {"table": "sales", **extra})
    assert Path(business["data_path"]).read_bytes() == before


def test_bound_values_are_literal_and_database_immutable(business):
    before = Path(business["data_path"]).read_bytes()
    value = "东' OR 1=1 --"
    result = execute_plan(business, {"table": "sales", "filters": [{"column": "region", "op": "eq", "value": value}]})
    assert result["rows"] == [] and value in result["parameters"] and value not in result["sql"]
    assert Path(business["data_path"]).read_bytes() == before


def test_cancelled_query_stops_before_reading(business):
    with pytest.raises(TablePlanError, match="已取消"):
        execute_plan(business, {"table": "sales"}, cancelled=lambda: True)


def test_real_csv_transcription_not_hardcoded(tmp_path):
    path = Path(__file__).resolve().parents[1] / "samples" / "石景山区2024年产业指标_原表转录.csv"
    database = tmp_path / "real.sqlite"
    parsed = parsers.parse(path, "csv", database, lambda *args: None)
    source = source_for(database, "csv", {"note": "合计、产业、行业为不同层级，不可混合求和。pct列保存百分数数值。"})
    assert parsed["tables"][0]["row_count"] == 19
    result = execute_plan(source, {"table": "数据表", "filters": [{"column": "分类", "op": "eq", "value": "产业"}], "aggregates": [{"op": "sum", "column": "增加值_亿元", "alias": "增加值合计"}]})
    assert result["rows"] == [["1312.9"]]
    ratio = execute_plan(source, {"table": "数据表", "ratio": {"numerator": {"column": "增加值_亿元", "filters": [{"column": "指标", "op": "eq", "value": "第三产业"}]}, "denominator": {"column": "增加值_亿元", "filters": [{"column": "指标", "op": "eq", "value": "地区生产总值"}]}, "scale": 100}})
    assert ratio["rows"] == [["1222.1", "1312.9", "93.08401249143118287759920786"]]
    assert [r["row_number"] for r in ratio["evidence_rows"]] == [1, 3]
