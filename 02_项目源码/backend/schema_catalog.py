"""Bounded schema discovery for unfamiliar local databases.

Names, samples and source metadata are untrusted data. Relationships are only
reported when SQLite declares a foreign key; matching column names are not an
inferred relationship. Sample statistics never claim full-table uniqueness.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict

from .parsers import quoted, readonly_database, value_json

MAX_TABLES = 256
MAX_DETAIL_TABLES = 16
MAX_COLUMNS = 256
MAX_SAMPLE_ROWS = 80
MAX_SAMPLE_CELLS = 2400
MAX_VALUE_CHARS = 160
MAX_DISTINCT_VALUES = 12
MAX_CATALOG_CHARS = 100_000
MAX_SECONDS = 10


class SchemaCatalogError(ValueError):
    pass


def _value(value):
    result = value_json(value)
    if isinstance(result, str) and len(result) > MAX_VALUE_CHARS:
        return result[:MAX_VALUE_CHARS] + "…"
    return result


def build_catalog(source, table_names=None, cancelled=None):
    """Return declared schema plus optional, explicitly sampled value profiles.

    ``table_names=[]`` requests overview only. ``None`` details the first 16
    allowed tables. Explicit names must belong to the imported source. All
    schema discovery is checked against the current immutable SQLite file.
    """
    if not source.get("data_path"):
        raise SchemaCatalogError("当前资料没有可读取的数据库")
    if table_names is not None and (not isinstance(table_names, (list, tuple)) or any(not isinstance(n, str) for n in table_names)):
        raise SchemaCatalogError("待查看的表名必须是列表")
    allowed = {str(t["name"]): t for t in source.get("tables", []) if isinstance(t, dict) and isinstance(t.get("name"), str)}
    requested = list(dict.fromkeys(table_names if table_names is not None else allowed))
    unknown = [name for name in requested if name not in allowed]
    if unknown:
        raise SchemaCatalogError("所选表不属于当前资料：" + "、".join(unknown[:5]))
    deadline = time.monotonic() + MAX_SECONDS
    warnings, tables, relationships = [], [], []
    truncated = False

    def interrupted():
        return bool((cancelled and cancelled()) or time.monotonic() > deadline)

    def check():
        if cancelled and cancelled():
            raise SchemaCatalogError("操作已取消")
        if time.monotonic() > deadline:
            raise SchemaCatalogError("数据库结构读取超时，请缩小资料范围")

    if len(allowed) > MAX_TABLES:
        warnings.append(f"仅读取前 {MAX_TABLES} 张表的结构")
        truncated = True
    selected = requested[:MAX_DETAIL_TABLES]
    if len(requested) > MAX_DETAIL_TABLES:
        warnings.append(f"本次仅展开前 {MAX_DETAIL_TABLES} 张表的取值样本；可按表名重新查看")
        truncated = True
    from .sql_engine import attach_table_contexts, MAX_TABLE_CONTEXT_CHARS, MAX_TABLE_CONTEXT_PER_TABLE
    # Prioritize the requested tables while reserving an explicit boundary for
    # every omitted structure. Header context counts toward the catalog budget.
    context_names = sorted(list(allowed)[:MAX_TABLES], key=lambda name: name not in selected)
    contexts = {r['table_name']: r['table_context'] for r in attach_table_contexts(
        source, [{'table_name': name, 'lineage': 'table'} for name in context_names]) if 'table_context' in r}
    try:
        with readonly_database(source["data_path"]) as connection:
            connection.set_progress_handler(lambda: int(interrupted()), 2000)
            definitions = dict(connection.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            for name, imported in list(allowed.items())[:MAX_TABLES]:
                check()
                definition = definitions.get(name)
                if not definition or str(definition).lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    warnings.append(f"表「{name}」不是当前文件中的普通表，未读取")
                    truncated = True
                    continue
                info = [r for r in connection.execute(f"PRAGMA table_xinfo({quoted(name)})") if r[6] in (0, 2, 3)]
                if len(info) > MAX_COLUMNS:
                    warnings.append(f"表「{name}」字段超过 {MAX_COLUMNS} 个，未展开")
                    truncated = True
                    continue
                columns = [dict(name=r[1], type=r[2] or "未声明", nullable=not bool(r[3]), primary_key_position=r[5], generated=r[6] in (2, 3), origin="ddl") for r in info]
                primary_key = [r[1] for r in sorted(info, key=lambda x: x[5]) if r[5]]
                # SQLite ordinary composite PRIMARY KEY may still allow NULL;
                # report the declared NOT NULL constraint rather than guessing.
                row_count = imported.get("row_count")
                row_count_source = "import_snapshot"
                if not isinstance(row_count, int) or row_count < 0:
                    row_count = connection.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted(name)} LIMIT 50001)").fetchone()[0]
                    row_count_source = "bounded_count"
                unique_keys = []
                for idx in connection.execute(f"PRAGMA index_list({quoted(name)})"):
                    if idx[2] and not idx[4]:
                        key = [r[2] for r in connection.execute(f"PRAGMA index_info({quoted(idx[1])})")]
                        if key and all(isinstance(v, str) for v in key):
                            unique_keys.append(key)
                grouped = defaultdict(list)
                for fk in connection.execute(f"PRAGMA foreign_key_list({quoted(name)})"):
                    grouped[fk[0]].append(fk)
                foreign_keys = []
                for ident, rows in sorted(grouped.items()):
                    rows.sort(key=lambda r: r[1])
                    target = rows[0][2]
                    target_columns = [r[4] for r in rows]
                    target_allowed = target in allowed and target in definitions and not str(definitions[target]).lstrip().upper().startswith("CREATE VIRTUAL TABLE")
                    if target_allowed and any(col is None for col in target_columns):
                        target_info = list(connection.execute(f"PRAGMA table_xinfo({quoted(target)})"))
                        target_pk = [r[1] for r in sorted(target_info, key=lambda r: r[5]) if r[5]]
                        if len(target_pk) == len(target_columns):
                            target_columns = target_pk
                    fk = dict(id=ident, columns=[r[3] for r in rows], referenced_table=target, referenced_columns=target_columns, on_update=rows[0][5], on_delete=rows[0][6], target_available=target_allowed, origin="ddl", data_integrity_verified=False)
                    foreign_keys.append(fk)
                    relationships.append(dict(table=name, **fk))
                entry = dict(name=name, row_count=row_count, row_count_source=row_count_source, row_count_truncated=row_count_source == "bounded_count" and row_count == 50001, columns=columns, primary_key=primary_key, foreign_keys=foreign_keys, unique_keys=unique_keys, constraint_origin="ddl")
                if name in contexts:
                    entry['table_context'] = contexts[name]
                if name in selected and columns:
                    sample_limit = min(MAX_SAMPLE_ROWS, max(1, MAX_SAMPLE_CELLS // len(columns)))
                    names = [c["name"] for c in columns]
                    # Row samples are context, not evidence row numbers. Avoid
                    # claiming positional lineage or requiring a synthetic ID.
                    sample = connection.execute(f"SELECT {','.join(quoted(c) for c in names)} FROM {quoted(name)} LIMIT ?", (sample_limit,)).fetchall()
                    profiles = []
                    for index, col in enumerate(names):
                        seen = {}
                        nulls = 0
                        for row in sample:
                            value = row[index]
                            if value is None:
                                nulls += 1
                            else:
                                seen.setdefault(json.dumps(value_json(value), ensure_ascii=False, sort_keys=True), value)
                        profiles.append(dict(name=col, sampled_rows=len(sample), null_count=nulls, distinct_count=len(seen), distinct_values=[_value(v) for v in list(seen.values())[:MAX_DISTINCT_VALUES]], values_truncated=len(seen) > MAX_DISTINCT_VALUES))
                    entry["details"] = dict(sample_rows=[[_value(v) for v in row] for row in sample[:3]], column_profiles=profiles, sample_limit=sample_limit, stats_scope="bounded_sample", sample_truncated=row_count is None or row_count > len(sample))
                # Preserve compact overview when values would exceed budget.
                projected = len(json.dumps(tables + [entry], ensure_ascii=False))
                if projected > MAX_CATALOG_CHARS and "details" in entry:
                    del entry["details"]
                    truncated = True
                    warnings.append(f"表「{name}」仅保留结构，取值样本超过本次上下文上限")
                if len(json.dumps(tables + [entry], ensure_ascii=False)) > MAX_CATALOG_CHARS:
                    warnings.append("数据库结构达到本次上下文上限，后续表未展开")
                    truncated = True
                    break
                tables.append(entry)
    except sqlite3.Error as exc:
        if cancelled and cancelled():
            raise SchemaCatalogError("操作已取消") from exc
        if interrupted():
            raise SchemaCatalogError("数据库结构读取超时，请缩小资料范围") from exc
        raise SchemaCatalogError("无法读取数据库结构，请重新导入资料") from exc
    included = {t["name"] for t in tables}
    relationships = [r for r in relationships if r["table"] in included]
    catalog = dict(tables=tables, relationships=relationships, selected_tables=selected, detailed_tables=[t["name"] for t in tables if "details" in t], total_allowed_tables=len(allowed), truncated=truncated, detail_budget=dict(max_tables=MAX_DETAIL_TABLES, max_rows_per_table=MAX_SAMPLE_ROWS, max_value_chars=MAX_VALUE_CHARS, max_catalog_chars=MAX_CATALOG_CHARS, max_table_context_chars=MAX_TABLE_CONTEXT_CHARS, max_table_context_per_table=MAX_TABLE_CONTEXT_PER_TABLE), warnings=warnings, metadata_untrusted=True, value_statistics_scope="bounded_sample_only")
    from .schema_semantics import build_semantic_layer, MAX_SEMANTIC_CHARS
    catalog['semantic_layer'] = build_semantic_layer(catalog, source)
    catalog['detail_budget']['max_semantic_chars'] = MAX_SEMANTIC_CHARS
    return catalog
