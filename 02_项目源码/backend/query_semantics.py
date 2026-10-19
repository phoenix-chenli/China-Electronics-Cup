"""Bounded structural diagnostics for SQL business semantics.

These diagnostics complement, never replace, the SQL executor's AST/authorizer
and independent interpretation review. Sample joins are explicitly previews;
they cannot establish global uniqueness or completeness of an undeclared link.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext

import sqlglot
from sqlglot import exp

from .parsers import quoted, readonly_database

MAX_JOIN_PROBES = 4
MAX_PROBE_ROWS = 200
MAX_PROBE_SECONDS = 3


def _columns(table):
    return {c["name"]: c for c in table.get("columns", [])}


def _unique(table, columns):
    wanted = set(columns)
    # A superset of a unique key is also unique for ordinary equality joins.
    # Nullable unique keys still permit NULL duplicates, but '=' never matches
    # those NULLs; this bound is explicitly not used for IS/NULL-safe joins.
    keys = [table.get("primary_key", []), *table.get("unique_keys", [])]
    return any(key and set(key).issubset(wanted) for key in keys)


def _pairs(condition, aliases):
    if condition is None:
        return []
    # OR predicates and null-safe equality are not ordinary equijoins.
    if any(isinstance(n, (exp.Or, exp.NullSafeEQ)) for n in condition.walk()):
        return []
    pairs, conjuncts = [], [condition]
    equals = []
    while conjuncts:
        item = conjuncts.pop()
        if isinstance(item, exp.Paren):conjuncts.append(item.this)
        elif isinstance(item, exp.And):conjuncts.extend([item.this, item.expression])
        elif isinstance(item, exp.EQ):equals.append(item)
    for eq in equals:
        left, right = eq.this, eq.expression
        if (isinstance(left, exp.Column) and isinstance(right, exp.Column)
                and left.table in aliases and right.table in aliases and left.table != right.table):
            pairs.append((left.table, left.name, right.table, right.name))
    return pairs


def _declared(catalog, left, lcols, right, rcols):
    wanted = set(zip(lcols, rcols))
    for rel in catalog.get("relationships", []):
        if not rel.get('columns') or len(rel['columns']) != len(rel.get('referenced_columns', [])):
            continue
        if rel.get("table") == left and rel.get("referenced_table") == right:
            if set(zip(rel.get("columns", []), rel.get("referenced_columns", []))).issubset(wanted):
                return {"origin": "ddl", "direction": "left_references_right", "foreign_key_id": rel.get("id")}
        if rel.get("table") == right and rel.get("referenced_table") == left:
            if {(b, a) for a, b in zip(rel.get("columns", []), rel.get("referenced_columns", []))}.issubset(wanted):
                return {"origin": "ddl", "direction": "right_references_left", "foreign_key_id": rel.get("id")}
    return {"origin": "query_candidate", "data_integrity_verified": False}


def diagnose_query(sql, catalog, contract=None):
    tables = {t["name"]: t for t in catalog.get("tables", [])}
    result = {"version": "query-semantics-1", "joins": [], "findings": [],
              "scope": "AST_and_declared_constraints", "requires_review": False,
              "unsupported_scopes": [], "truncated": False}
    def finding(code, detail, **data):
        item = {"code": code, "detail": detail, **data}
        if item not in result["findings"]:
            result["findings"].append(item)
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except (sqlglot.errors.ParseError, TypeError, RecursionError):
        finding("unparsed_sql", "SQL尚未形成可诊断结构；由只读执行器决定可否执行")
        result["requires_review"] = True
        return result
    for scope_index, select in enumerate(tree.find_all(exp.Select)):
        if scope_index >= 32:
            result["truncated"] = True; break
        direct = []
        from_clause = select.args.get("from_")
        if from_clause:
            direct.append(from_clause.this)
        direct.extend(join.this for join in select.args.get("joins", []))
        aliases = {t.alias_or_name: t.name for t in direct if isinstance(t, exp.Table) and t.name in tables}
        if any(not isinstance(t, exp.Table) or t.name not in tables for t in direct):
            result["unsupported_scopes"].append({"scope": scope_index, "reason": "derived_or_CTE_relation_requires_independent_review"})
        join_specs = []
        for join in select.args.get("joins", []):
            pairs = _pairs(join.args.get("on"), aliases)
            if not pairs:
                finding("non_equijoin_or_derived_relation", "连接不是可机械定位的声明字段等值关系，须核对实际条件和粒度", scope=scope_index)
                continue
            grouped = {}
            for a, ac, b, bc in pairs:
                if (b, a) in grouped:
                    grouped[b, a].append((bc, ac))
                else:
                    grouped.setdefault((a, b), []).append((ac, bc))
            for (a, b), cols in grouped.items():
                left, right = aliases[a], aliases[b]
                lc, rc = [c[0] for c in cols], [c[1] for c in cols]
                if not set(lc).issubset(_columns(tables[left])) or not set(rc).issubset(_columns(tables[right])):
                    finding("unknown_join_column", "连接引用不存在的字段", scope=scope_index); continue
                lu, ru = _unique(tables[left], lc), _unique(tables[right], rc)
                spec = {"scope": scope_index, "left_table": left, "left_alias": a, "left_columns": lc,
                        "right_table": right, "right_alias": b, "right_columns": rc,
                        "left_keys_declared_unique_for_equality": lu,
                        "right_keys_declared_unique_for_equality": ru,
                        "cardinality_bound": "one_to_one" if lu and ru else "one_to_many" if lu else "many_to_one" if ru else "many_to_many_or_unknown",
                        "relationship": _declared(catalog, left, lc, right, rc),
                        "join_kind": join.args.get("side") or join.args.get("kind") or "INNER",
                        "null_keys": "excluded_by_ordinary_equality",
                        "data_integrity_verified": False}
                if len(result["joins"]) < 16:
                    result["joins"].append(spec); join_specs.append(spec)
                else:
                    result["truncated"] = True
                if spec["relationship"]["origin"] != "ddl":
                    finding("undeclared_join", "字段等值或样本重合仅是关系候选，不能当作已声明外键", scope=scope_index, tables=[left, right])
                if not lu and not ru:
                    finding("unbounded_join_multiplicity", "连接两侧都没有可用的声明唯一键，可能重复行", scope=scope_index, tables=[left, right])
        for projection in select.expressions:
            # Descendant SELECTs receive their own scope; do not attribute their
            # aggregate to the parent query's join aliases.
            for node in projection.walk(prune=lambda n: n is not projection and isinstance(n, exp.Select)):
                is_sum = isinstance(node, (exp.Sum, exp.Avg)) or isinstance(node, exp.Anonymous) and node.name.upper() in {"DECIMAL_SUM", "DECIMAL_AVG", "TOTAL"}
                if is_sum:
                    cols = list(node.find_all(exp.Column))
                    for col in cols:
                        alias = col.table or (next(iter(aliases)) if len(aliases) == 1 else "")
                        for spec in join_specs:
                            multiplying = (alias == spec["left_alias"] and not spec["right_keys_declared_unique_for_equality"]
                                           or alias == spec["right_alias"] and not spec["left_keys_declared_unique_for_equality"])
                            if multiplying:
                                finding("aggregate_fanout", "聚合所在行可能被另一侧多次匹配；应核对按实体汇总、先聚合或EXISTS的口径", scope=scope_index, table=aliases.get(alias), column=col.name)
                    if node.find(exp.Distinct):
                        finding("distinct_measure_not_identity", "SUM/AVG(DISTINCT值)按数值去重，不等于按业务主键去重", scope=scope_index)
                if isinstance(node, exp.Count) and node.find(exp.Column):
                    finding("count_excludes_null", "COUNT(字段)不计NULL；核对是否应统计行数或非空值", scope=scope_index)
                if isinstance(node, exp.Coalesce):
                    finding("null_coalesced", "COALESCE改变缺失值语义，核对其业务依据", scope=scope_index)
                if isinstance(node, exp.Div) or isinstance(node, exp.Anonymous) and node.name.upper() == "DECIMAL_DIV":
                    finding("division_scope", "核对分母、零/NULL、比例与百分点以及浮点精度", scope=scope_index)
        for predicate in select.find_all(exp.Where):
            if predicate.find(exp.Not) and predicate.find(exp.In):
                finding("not_in_null", "NOT IN右侧含NULL会影响排除结果；核对是否应使用NOT EXISTS", scope=scope_index)
    if any(isinstance(n, (exp.Lag, exp.Lead)) for n in tree.walk()):
        finding("comparison_period", "窗口相邻记录不一定是相邻日/月/年；需核对缺期、基期和分区")
    if tree.find(exp.Limit):
        finding("topk_ties", "前N需核对排序方向、并列策略和稳定次序")
    sql_text = str(sql).casefold()
    if "strftime" in sql_text or any(isinstance(n, (exp.Date, exp.DateDiff, exp.DateAdd)) for n in tree.walk()):
        finding("time_boundary", "核对原始日期格式、时区、区间端点和跨年分组，不能由样本猜覆盖范围")
    if contract and contract.get("issues"):
        finding("invalid_element_contract", "候选要素引用或预算无效，不能以模型自报通过覆盖", issues=contract["issues"])
    result["findings"] = result["findings"][:32]
    result["requires_review"] = bool(result["findings"] or result["unsupported_scopes"] or result["truncated"])
    return result


def _number(value):
    if value is None or isinstance(value, (bool, bytes)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and -1000 <= number.adjusted() <= 1000 else None
    except (InvalidOperation, ValueError):
        return None


def probe_joins(source, catalog, diagnostics, cancelled=lambda: False):
    """Probe bounded physical-table subsets with SQLite's actual equality rules.

    SQL preview joins use SQLite itself (affinity/collation are not reimplemented
    with Python keys). All previews ignore the user's filters deliberately and
    are labelled as such. Reaching either side's cap never proves full coverage.
    """
    output = {"scope": "bounded_physical_table_preview_not_final_query", "probes": [],
              "max_rows_per_side": MAX_PROBE_ROWS, "max_probes": MAX_JOIN_PROBES,
              "truncated": len(diagnostics.get("joins", [])) > MAX_JOIN_PROBES}
    table_map = {t["name"]: t for t in catalog.get("tables", [])}
    deadline = time.monotonic() + MAX_PROBE_SECONDS
    def check():
        if cancelled(): raise InterruptedError("已取消")
        if time.monotonic() > deadline: raise TimeoutError
    try:
        with readonly_database(source["data_path"]) as connection:
            connection.set_progress_handler(lambda: int(cancelled() or time.monotonic() > deadline), 1000)
            for spec in diagnostics.get("joins", [])[:MAX_JOIN_PROBES]:
                check()
                lt, rt = spec["left_table"], spec["right_table"]
                if lt not in table_map or rt not in table_map: continue
                lc, rc = spec["left_columns"], spec["right_columns"]
                if not set(lc).issubset(_columns(table_map[lt])) or not set(rc).issubset(_columns(table_map[rt])): continue
                # Avoid rowid assumptions: a bounded subquery assigns local row
                # ordinals; these are audit sample identities, never evidence IDs.
                lcols, rcols = list(_columns(table_map[lt]))[:256], list(_columns(table_map[rt]))[:256]
                lcount = connection.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted(lt)} LIMIT ?)", (MAX_PROBE_ROWS + 1,)).fetchone()[0]
                rcount = connection.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted(rt)} LIMIT ?)", (MAX_PROBE_ROWS + 1,)).fetchone()[0]
                left_rows, right_rows = min(lcount, MAX_PROBE_ROWS), min(rcount, MAX_PROBE_ROWS)
                # Local ordinals are projected under collision-free names.
                lindex = "__zh_sample_ordinal__"
                while lindex in lcols or lindex in rcols: lindex += "_"
                prefix = f"WITH l AS (SELECT ROW_NUMBER() OVER () AS {quoted(lindex)}, * FROM {quoted(lt)} LIMIT {MAX_PROBE_ROWS}), r AS (SELECT ROW_NUMBER() OVER () AS {quoted(lindex)}, * FROM {quoted(rt)} LIMIT {MAX_PROBE_ROWS}) "
                condition = " AND ".join(f"l.{quoted(a)}=r.{quoted(b)}" for a, b in zip(lc, rc))
                metrics = [("left", lt, c, "l") for c in lcols if c not in lc][:4]
                metrics += [("right", rt, c, "r") for c in rcols if c not in rc][:4]
                metric_sql = [f"CASE WHEN typeof({alias}.{quoted(col)}) IN ('integer','real') OR (typeof({alias}.{quoted(col)})='text' AND length({alias}.{quoted(col)})<=100) THEN {alias}.{quoted(col)} END" for _, _, col, alias in metrics]
                projection = [f"l.{quoted(lindex)}", f"r.{quoted(lindex)}", *metric_sql]
                matches = connection.execute(prefix + f"SELECT {','.join(projection)} FROM l JOIN r ON {condition} LIMIT ?", (MAX_PROBE_ROWS * MAX_PROBE_ROWS + 1,)).fetchall()
                left_freq, right_freq = Counter(m[0] for m in matches), Counter(m[1] for m in matches)
                sample = {"left_table": lt, "left_columns": lc, "right_table": rt, "right_columns": rc,
                          "relationship_origin": spec["relationship"]["origin"],
                          "left_sample_rows": left_rows, "right_sample_rows": right_rows,
                          "left_complete": lcount <= MAX_PROBE_ROWS, "right_complete": rcount <= MAX_PROBE_ROWS,
                          "joined_sample_rows": len(matches), "matched_left_rows": len(left_freq),
                          "matched_right_rows": len(right_freq),
                          "left_sample_match_rate": len(left_freq) / left_rows if left_rows else None,
                          "right_sample_match_rate": len(right_freq) / right_rows if right_rows else None,
                          "max_matches_per_left_sample_row": max(left_freq.values(), default=0),
                          "max_matches_per_right_sample_row": max(right_freq.values(), default=0),
                          "sample_uniqueness_is_global_constraint": False,
                          "uses_original_sql_filters": False, "aggregate_previews": []}
                for side, alias, cols, complete in (("left", "l", lc, lcount <= MAX_PROBE_ROWS), ("right", "r", rc, rcount <= MAX_PROBE_ROWS)):
                    keys = ','.join(f'{alias}.{quoted(c)}' for c in cols)
                    nonnull = ' AND '.join(f'{alias}.{quoted(c)} IS NOT NULL' for c in cols)
                    nonnull_count = connection.execute(prefix + f'SELECT COUNT(*) FROM {alias} WHERE {nonnull}').fetchone()[0]
                    distinct_count = connection.execute(prefix + f'SELECT COUNT(*) FROM (SELECT {keys} FROM {alias} WHERE {nonnull} GROUP BY {keys})').fetchone()[0]
                    sample[f'{side}_key_profile'] = {
                        'nonnull_rows': nonnull_count, 'distinct_nonnull_keys': distinct_count,
                        'repeated_nonnull_rows': nonnull_count - distinct_count,
                        'observed_unique_nonnull_keys': nonnull_count == distinct_count,
                        'scope': 'entire_table_at_snapshot' if complete else 'bounded_sample_only',
                        'is_declared_unique_constraint': False}
                # Report actual before/after sums for numeric sample columns.
                # A changed sum is a multiplicity observation, not proof that
                # this is the intended metric or that the final query is wrong.
                for i, (side, name, col, _) in enumerate(metrics, 2):
                    values = [(row[0 if side == "left" else 1], _number(row[i])) for row in matches]
                    numeric = [(ordinal, n) for ordinal, n in values if n is not None]
                    if not numeric: continue
                    with localcontext() as context:
                        context.prec = 4096
                        before = sum(dict(numeric).values(), Decimal(0))
                        after = sum((n for _, n in numeric), Decimal(0))
                    sample["aggregate_previews"].append({"side": side, "table": name, "column": col,
                        "matched_source_sum": str(before), "joined_sample_sum": str(after),
                        "changed_by_sample_multiplicity": before != after, "not_a_business_metric": True,
                        "numeric_conversion": "finite_decimal_candidates_only",
                        "non_numeric_matched_cells": len(values) - len(numeric)})
                output["probes"].append(sample)
    except InterruptedError:
        raise
    except (sqlite3.Error, TimeoutError, KeyError):
        if cancelled(): raise InterruptedError("已取消")
        output["truncated"] = True
        output["limitation"] = "关联样本探查未完整结束，不能据此确认不存在或全表唯一"
    return output
