"""Bounded, metadata-aware read-only queries over explicitly selected sources.

Source fingerprints identify a schema snapshot, never an external database's
complete data version. Results carry their own immutable returned-row digest.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import date, datetime, time as daytime
from decimal import Decimal, localcontext

from . import connections as c, store as s

MAX_SECONDS = 5
MAX_ROWS = 200
MAX_TABLES = 50
MAX_RELATIONS = 100
MAX_RESULT_BYTES = 2 * 1024 * 1024
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evidenceflow-database")
_slots = threading.BoundedSemaphore(4)
_operation_state = threading.local()


def initialize():
    with s.conn() as db:
        db.execute("CREATE TABLE IF NOT EXISTS ef_database_import_metadata(name TEXT PRIMARY KEY,body TEXT NOT NULL)")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _guard(stop, deadline):
    if stop.is_set():
        raise InterruptedError("数据库查询已取消")
    if time.monotonic() >= deadline:
        raise ValueError("数据库操作超过 5 秒预算，请缩小表或查询范围")


def _bounded(operation, cancelled):
    stop = cancelled or threading.Event()
    deadline = time.monotonic() + MAX_SECONDS
    while not _slots.acquire(timeout=.05):
        _guard(stop, deadline)
    try:
        _guard(stop, deadline)
        future = _pool.submit(operation, stop, deadline)
    except BaseException:
        _slots.release()
        raise
    future.add_done_callback(lambda _: _slots.release())
    while True:
        try:
            _guard(stop, deadline)
            result = future.result(timeout=min(.05, max(.001, deadline - time.monotonic())))
            _guard(stop, deadline)
            return result
        except FutureTimeout:
            continue
        except (InterruptedError, ValueError):
            future.cancel()
            raise


def _configuration(connection_id):
    if connection_id is None:
        return {"kind": "sqlite", "path": str(s.BUSINESS_DB), "id": None,
                "revision": 0, "name": "本地业务数据库", "timeout": MAX_SECONDS}
    if not isinstance(connection_id, str) or not connection_id:
        raise ValueError("数据库连接编号不正确")
    return c.ready(connection_id, c.DB_KINDS)


@contextmanager
def _database(config, stop, deadline):
    db = None
    _guard(stop, deadline)
    try:
        remaining = max(1, min(MAX_SECONDS, int(math.ceil(deadline - time.monotonic()))))
        if config["kind"] == "sqlite":
            db = sqlite3.connect(s.readonly_uri(config["path"]), uri=True, timeout=min(1, remaining))
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            # SQLite constructs each cell before fetchmany returns. Bound that
            # construction too, so nested REPLACE/GROUP_CONCAT cannot allocate
            # the engine's default gigabyte-sized string before result checks.
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_RESULT_BYTES)
            db.set_progress_handler(lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000)
            db.execute("BEGIN")
        elif config["kind"] == "postgresql":
            import psycopg
            db = psycopg.connect(host=config["host"], port=config["port"], dbname=config["database"],
                user=config["username"], password=config.get("_secret", ""), connect_timeout=remaining,
                sslmode="prefer" if c.is_local(config) else "verify-full",
                options=f"-c default_transaction_read_only=on -c statement_timeout={remaining * 1000}")
            db.execute("SET TRANSACTION READ ONLY")
            # Physical tables are schema-qualified by the guard. Resolve bare
            # allowlisted functions only in the builtin namespace, even when a
            # database has an unsafe role/schema search_path configuration.
            db.execute("SET LOCAL search_path=pg_catalog")
        elif config["kind"] == "mysql":
            import pymysql
            db = pymysql.connect(host=config["host"], port=config["port"], database=config["database"],
                user=config["username"], password=config.get("_secret", ""), connect_timeout=remaining,
                read_timeout=remaining, write_timeout=remaining, charset="utf8mb4", local_infile=False,
                ssl=None if c.is_local(config) else {"check_hostname": True}, autocommit=False)
            with db.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET SESSION max_execution_time=" + str(remaining * 1000))
                cursor.execute("START TRANSACTION READ ONLY")
        else:
            raise ValueError("不支持的数据库类型")
        _guard(stop, deadline)
        _operation_state.context = (config, stop, deadline)
        try:
            yield db
        finally:
            _operation_state.context = None
    except (InterruptedError, ValueError):
        raise
    except Exception:
        _guard(stop, deadline)
        raise ValueError("数据库只读操作失败，请检查权限、字段、方言和服务超时；未执行写入") from None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _read(db, sql, params=(), limit=20000):
    context = getattr(_operation_state, "context", None)
    if context:
        config, stop, deadline = context
        _guard(stop, deadline)
        if config["kind"] != "sqlite":
            milliseconds = max(1, int((deadline - time.monotonic()) * 1000))
            with db.cursor() as setting:
                setting.execute(("SET LOCAL statement_timeout=" if config["kind"] == "postgresql"
                                 else "SET SESSION max_execution_time=") + str(milliseconds))
    cursor = db.cursor()
    try:
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        columns = [str(item[0]) for item in cursor.description]
        values = cursor.fetchmany(limit + 1)
        if len(values) > limit:
            raise ValueError("数据库元数据或核验结果超过安全上限，请缩小范围")
        if context:
            _guard(stop, deadline)
        return [dict(zip(columns, row)) for row in values]
    finally:
        cursor.close()


def _dialect(config):
    return "postgres" if config["kind"] == "postgresql" else config["kind"]


def _quote(config, name):
    return "`" + name.replace("`", "``") + "`" if config["kind"] == "mysql" else s.quoted(name)


def _qualified(config, table):
    return _quote(config, table["schema"]) + "." + _quote(config, table["name"])


def _key(table):
    return table["schema"] + "." + table["name"]


def _listing(db, config):
    if config["kind"] == "sqlite":
        return [{"schema": "main", "name": row["name"]} for row in _read(db,
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name", limit=1000)]
    if config["kind"] == "postgresql":
        sql = "SELECT table_schema AS schema,table_name AS name FROM information_schema.tables " \
              "WHERE table_type='BASE TABLE' AND table_schema NOT IN ('information_schema','pg_catalog') ORDER BY table_schema,table_name"
        return _read(db, sql, limit=1000)
    return _read(db, "SELECT table_schema AS `schema`,table_name AS name FROM information_schema.tables "
                     "WHERE table_type='BASE TABLE' AND table_schema=DATABASE() ORDER BY table_name", limit=1000)


def _import_metadata():
    with s.conn() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ef_database_import_metadata'").fetchone():
            return {}
        rows = db.execute("SELECT name,body FROM ef_database_import_metadata ORDER BY name").fetchall()
    result = {}
    for row in rows:
        try:
            body = json.loads(row["body"])
            if isinstance(body, dict):
                result[row["name"]] = body
        except (json.JSONDecodeError, TypeError):
            continue
    return result


def _sqlite_metadata(db, item, imports):
    name = item["name"]
    fields = _read(db, "SELECT * FROM pragma_table_xinfo(?)", (name,))
    columns = [{"name": f["name"], "type": f["type"] or "", "nullable": not bool(f["notnull"]),
                "primary_key": bool(f["pk"])} for f in fields if f.get("hidden", 0) != 1]
    primary = [f["name"] for f in sorted(fields, key=lambda f: f["pk"]) if f["pk"]]
    unique = []
    for index in _read(db, "SELECT * FROM pragma_index_list(?)", (name,)):
        if not index["unique"] or index.get("partial"):
            continue
        parts = _read(db, "SELECT * FROM pragma_index_info(?) ORDER BY seqno", (index["name"],))
        if parts and all(p["name"] is not None for p in parts):
            unique.append({"name": index["name"], "columns": [p["name"] for p in parts], "source": "database_constraint"})
    foreign = {}
    for part in _read(db, "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id,seq", (name,)):
        relation = foreign.setdefault(part["id"], {"id": "fk-" + str(part["id"]), "columns": [],
            "referenced_schema": "main", "referenced_table": part["table"], "referenced_columns": [],
            "declared": True, "source": "database_constraint"})
        relation["columns"].append(part["from"])
        relation["referenced_columns"].append(part["to"])
    imported = imports.get(name, {})
    valid_names = {f["name"] for f in columns}
    def valid_key(value):
        return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x in valid_names for x in value)
    if not primary and valid_key(imported.get("primary_key")):
        primary = imported["primary_key"]
    for key in imported.get("unique_keys", []):
        if isinstance(key, dict) and valid_key(key.get("columns")):
            unique.append({"name": str(key.get("name", "imported-unique")), "columns": key["columns"], "source": "import_metadata"})
    declared = list(foreign.values())
    for key in imported.get("foreign_keys", []):
        if isinstance(key, dict) and valid_key(key.get("columns")) and isinstance(key.get("referenced_table"), str):
            declared.append({"id": str(key.get("id", "imported-fk")), "columns": key["columns"],
                "referenced_schema": "main", "referenced_table": key["referenced_table"],
                "referenced_columns": key.get("referenced_columns", []), "declared": False, "source": "import_metadata"})
    return {**item, "qualified_name": _key(item), "columns": columns, "primary_key": primary,
            "unique_keys": unique, "foreign_keys": declared}


def _remote_metadata(db, config, item):
    schema, name = item["schema"], item["name"]
    fields = _read(db, "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                      "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, name))
    columns = [{"name": f["column_name"], "type": f["data_type"], "nullable": f["is_nullable"] == "YES",
                "primary_key": False} for f in fields]
    parts = _read(db, "SELECT tc.constraint_name,tc.constraint_type,kcu.column_name,kcu.ordinal_position "
        "FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_catalog=kcu.constraint_catalog AND tc.constraint_schema=kcu.constraint_schema "
        "AND tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema AND tc.table_name=kcu.table_name "
        "WHERE tc.table_schema=%s AND tc.table_name=%s AND tc.constraint_type IN ('PRIMARY KEY','UNIQUE') "
        "ORDER BY tc.constraint_name,kcu.ordinal_position", (schema, name))
    groups = {}
    for part in parts:
        key = groups.setdefault(part["constraint_name"], {"name": part["constraint_name"], "columns": [],
                                "source": "database_constraint", "type": part["constraint_type"]})
        key["columns"].append(part["column_name"])
    primary = next((k["columns"] for k in groups.values() if k["type"] == "PRIMARY KEY"), [])
    for col in columns:
        col["primary_key"] = col["name"] in primary
    unique = [{k: v for k, v in value.items() if k != "type"} for value in groups.values()]
    if config["kind"] == "mysql":
        foreign_rows = _read(db, "SELECT constraint_name,column_name,ordinal_position,referenced_table_schema AS referenced_schema,"
            "referenced_table_name AS referenced_table,referenced_column_name AS referenced_column FROM information_schema.key_column_usage "
            "WHERE table_schema=%s AND table_name=%s AND referenced_table_name IS NOT NULL ORDER BY constraint_name,ordinal_position", (schema, name))
    else:
        # pg_constraint retains FK column order even when a read-only role cannot
        # see information_schema.referential_constraints for the parent table.
        foreign_rows = _read(db, "SELECT co.conname AS constraint_name,a.attname AS column_name,k.ordinality AS ordinal_position,"
            "rn.nspname AS referenced_schema,rt.relname AS referenced_table,ra.attname AS referenced_column "
            "FROM pg_catalog.pg_constraint co JOIN pg_catalog.pg_class t ON t.oid=co.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
            "JOIN pg_catalog.pg_class rt ON rt.oid=co.confrelid JOIN pg_catalog.pg_namespace rn ON rn.oid=rt.relnamespace "
            "JOIN LATERAL unnest(co.conkey) WITH ORDINALITY k(attnum,ordinality) ON TRUE "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
            "JOIN pg_catalog.pg_attribute ra ON ra.attrelid=rt.oid AND ra.attnum=co.confkey[k.ordinality::int] "
            "WHERE co.contype='f' AND n.nspname=%s AND t.relname=%s ORDER BY co.conname,k.ordinality", (schema, name))
    foreign = {}
    for part in foreign_rows:
        relation = foreign.setdefault(part["constraint_name"], {"id": part["constraint_name"], "columns": [],
            "referenced_schema": part["referenced_schema"], "referenced_table": part["referenced_table"], "referenced_columns": [],
            "declared": True, "source": "database_constraint"})
        relation["columns"].append(part["column_name"])
        relation["referenced_columns"].append(part["referenced_column"])
    return {**item, "qualified_name": _key(item), "columns": columns, "primary_key": primary,
            "unique_keys": unique, "foreign_keys": list(foreign.values())}


def _verify_relation(db, config, left, left_columns, right, right_columns, stop, deadline):
    if not left_columns or len(left_columns) != len(right_columns):
        return False, "关联列数量不一致"
    if not set(left_columns).issubset({f["name"] for f in left["columns"]}) or not set(right_columns).issubset({f["name"] for f in right["columns"]}):
        return False, "关联列不在当前元数据中"
    _guard(stop, deadline)
    ltable, rtable = _qualified(config, left), _qualified(config, right)
    lcols = [_quote(config, name) for name in left_columns]
    rcols = [_quote(config, name) for name in right_columns]
    nonnull = " AND ".join(x + " IS NOT NULL" for x in rcols)
    duplicate = _read(db, f"SELECT 1 AS found FROM {rtable} WHERE {nonnull} GROUP BY " + ",".join(rcols) + " HAVING COUNT(*)>1 LIMIT 1", limit=1)
    if duplicate:
        return False, "目标关联键存在重复"
    _guard(stop, deadline)
    left_nonnull = " AND ".join("l." + x + " IS NOT NULL" for x in lcols)
    if not _read(db, f"SELECT 1 AS found FROM {ltable} l WHERE {left_nonnull} LIMIT 1", limit=1):
        return False, "没有非空子表值可验证覆盖"
    match = " AND ".join("r." + rc + "=l." + lc for lc, rc in zip(lcols, rcols))
    orphan = _read(db, f"SELECT 1 AS found FROM {ltable} l WHERE {left_nonnull} AND NOT EXISTS "
                       f"(SELECT 1 FROM {rtable} r WHERE {match}) LIMIT 1", limit=1)
    _guard(stop, deadline)
    return (False, "存在未覆盖的子表关联值") if orphan else (True, "已验证目标非空键唯一及全部非空子表值覆盖；不代表声明外键或业务语义已确认")


def _family(value):
    value = value.upper()
    if any(t in value for t in ("INT", "REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")):
        return "number"
    if any(t in value for t in ("CHAR", "TEXT", "CLOB", "UUID")):
        return "text"
    return value


def _relations(db, config, tables, stop, deadline):
    by_name = {_key(t): t for t in tables}
    declared, candidates, proposals, seen = [], [], [], set()
    def propose(left, lcols, right, rcols, declared_fk=False, basis="同名字段及目标主键/唯一键"):
        signature = (_key(left), tuple(lcols), _key(right), tuple(rcols))
        if signature in seen:
            return
        seen.add(signature)
        proposals.append((left, list(lcols), right, list(rcols), declared_fk, basis))
    for left in tables:
        for fk in left["foreign_keys"]:
            right = by_name.get(fk["referenced_schema"] + "." + fk["referenced_table"])
            if right:
                rcols = fk["referenced_columns"]
                if not rcols or any(x is None for x in rcols):
                    rcols = right["primary_key"]
                propose(left, fk["columns"], right, rcols, fk["declared"], fk["source"])
    for right in tables:
        keys = [right["primary_key"]] + [k["columns"] for k in right["unique_keys"]]
        for rcols in keys:
            if len(rcols) != 1:
                continue
            right_column = next((f for f in right["columns"] if f["name"] == rcols[0]), None)
            if not right_column:
                continue
            for left in tables:
                if left is right:
                    continue
                for field in left["columns"]:
                    exact = field["name"].casefold() == rcols[0].casefold() and field["name"].casefold() != "id"
                    singular = right["name"].rstrip("s").casefold() + "_id"
                    conventional = rcols[0].casefold() == "id" and field["name"].casefold() == singular
                    if (exact or conventional) and _family(field["type"]) == _family(right_column["type"]):
                        propose(left, [field["name"]], right, rcols)
    for left, lcols, right, rcols, fk, basis in proposals[:MAX_RELATIONS]:
        verified, detail = _verify_relation(db, config, left, lcols, right, rcols, stop, deadline)
        relation = {"id": "relation-" + _hash([_key(left), lcols, _key(right), rcols])[:16],
            "left_table": _key(left), "left_columns": lcols, "right_table": _key(right), "right_columns": rcols,
            "source": "foreign_key" if fk else "verified_candidate" if verified else "candidate",
            "declared": fk, "verified": verified, "basis": basis, "verification": detail,
            "cardinality": 'one_to_one' if any(set(lcols)==set(key) for key in [left['primary_key']]+[k['columns'] for k in left['unique_keys']] if key) else 'many_to_one_possible',
            "grain_note": '子表多行可能对应同一父表键；父表计数用明确粒度，金额先按目标粒度聚合，避免两个子表 JOIN 放大。'}
        (declared if fk else candidates).append(relation)
    return declared, candidates, len(proposals) > MAX_RELATIONS


def _candidates(items,query):
    if not query:return []
    words=set(re.findall(r'[a-z][a-z0-9]+',query.casefold()))
    def score(text):
        name=text.casefold();tokens=set(re.findall(r'[a-z][a-z0-9]+',name.replace('_',' ')))
        stems={t.rstrip('s') for t in tokens};wanted={t.rstrip('s') for t in words}
        return 5*int(name in query.casefold())+len(stems&wanted)
    ranked=[]
    for item in items:
        fields=[{'name':c['name'],'type':c.get('type'),'match_score':score(c['name'])} for c in item.get('columns',[]) if score(c['name'])]
        value=score(item['name'])+sum(f['match_score'] for f in fields)
        if value:ranked.append({'table':item.get('qualified_name') or _key(item),'score':value,'fields':sorted(fields,key=lambda f:-f['match_score'])[:16],
                               'basis':'问题中的实际表字段名匹配；未推断业务别名或读取样本值'})
    return sorted(ranked,key=lambda x:(-x['score'],x['table']))[:12]


def _inspect_on(db, config, table_names, stop, deadline, query=None):
    listing = _listing(db, config)
    _guard(stop, deadline)
    selected = listing
    if table_names is not None:
        if not isinstance(table_names, list) or not 1 <= len(table_names) <= MAX_TABLES or not all(isinstance(n, str) and n for n in table_names):
            raise ValueError("请选择 1 至 50 张真实表")
        selected = []
        for name in table_names:
            matches = [item for item in listing if name in (item["name"], _key(item))]
            if len(matches) != 1:
                raise ValueError("表名不存在或跨 Schema 有歧义，请使用完整表名：" + name[:100])
            if matches[0] in selected:
                raise ValueError("不能重复选择同一张表")
            selected.append(matches[0])
    source = {"kind": config["kind"], "connection_id": config["id"], "revision": config["revision"],
              "name": config["name"], "fingerprint_scope": "schema_snapshot"}
    if len(selected) > MAX_TABLES:
        source["fingerprint"] = _hash([source, listing])
        return {"dialect": _dialect(config), "source": source, "tables": [], "table_listing": listing,
                "needs_selection": True, "truncated": False, "relation_candidates": [], "relations": [],
                "candidate_tables":_candidates(listing,query),
                "boundary": "源含超过 50 张表，请明确选择相关表后读取字段；未静默丢弃剩余表。"}
    imports = _import_metadata() if config["id"] is None else {}
    tables = []
    for item in selected:
        _guard(stop, deadline)
        tables.append(_sqlite_metadata(db, item, imports) if config["kind"] == "sqlite" else _remote_metadata(db, config, item))
    # Fingerprints exclude time and row-dependent relation checks: they describe
    # schema identity, not a promise that external rows remain unchanged.
    source["fingerprint"] = _hash([source, tables])
    relations, candidates, relation_truncated = _relations(db, config, tables, stop, deadline)
    return {"dialect": _dialect(config), "source": source, "tables": tables, "table_listing": listing,
            "needs_selection": False, "truncated": False, "relations": relations,
            "relation_candidates": candidates, "relations_truncated": relation_truncated,
            "candidate_tables":_candidates(tables,query),
            "boundary": "字段与约束来自数据库；候选关联仅通过当前键唯一/值覆盖检查，未冒充声明外键。"}


def inspect(connection_id=None, table_names=None, cancelled=None, query=None):
    if query is not None and (not isinstance(query,str) or len(query)>2000):raise ValueError('结构定位问题应为不超过2000字的文字')
    def operation(stop, deadline):
        config = _configuration(connection_id)
        with _database(config, stop, deadline) as db:
            return _inspect_on(db, config, table_names, stop, deadline,query)
    return _bounded(operation, cancelled)


def guard_schema(snapshot):
    return {"tables": {t["qualified_name"]: {"columns": {f["name"]: f["type"] for f in t["columns"]}}
                        for t in snapshot["tables"]},
            "relations": [copy.deepcopy(r) for r in snapshot.get("relations", []) + snapshot.get("relation_candidates", []) if r.get("verified")]}


def _parameters(params, count):
    params = [] if params is None else params
    if not isinstance(params, (list, tuple)) or len(params) > 100 or len(params) != count:
        raise ValueError("SQL 参数必须是与位置占位符数量一致的列表")
    for value in params:
        if value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("SQL 参数只接受文字、有限数字、布尔值和空值")
        if isinstance(value, str) and len(value) > 10000 or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("SQL 参数过长或不是有限数值")
    return list(params)


def _scalar(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("数据库返回了非有限数值")
        return str(value)
    if isinstance(value, (datetime, date, daytime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return "[二进制 " + str(len(value)) + " 字节]"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("数据库返回了非有限数值")
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def validate_question_projection(sql, question, dialect="sqlite"):
    """Honor explicit output counts/names before execution, never trim returned data.

    Ordinary business wording is not a column contract. Quoted output names
    apply only after an explicit '列名依次为' directive; inner CTEs are untouched.
    """
    if not question:
        return None
    import sqlglot
    from sqlglot import exp
    counts = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
    matches = re.findall(r'(?:只|仅)(?:返回|输出|显示)\s*([1-9]\d?|[一二两三四五六七八九十])\s*(?:个)?列', question)
    matches += re.findall(r'(?:返回|输出|显示|保留|列出)(?!了|过|的)[^。；;\n]{0,120}?[，,]\s*(?:共|总共)\s*([1-9]\d?|[一二两三四五六七八九十])\s*(?:个)?列',question)
    count_values = {int(x) if x.isdigit() else counts[x] for x in matches}
    if len(count_values) > 1:
        raise ValueError('问题指定了不同的输出列数，请明确本轮需要几列')
    names = None
    directive = re.search(r'列名依次为\s*[:：]?\s*([^。；;\n]+)', question)
    if directive:
        raw = directive[1].strip()
        quoted = re.findall(r'`([^`]+)`|「([^」]+)」|“([^”]+)”', raw)
        remainder = re.sub(r'`[^`]+`|「[^」]+」|“[^”]+”', '', raw)
        if quoted and re.fullmatch(r'[\s、,，和及与]*', remainder):
            names = [next(value for value in group if value) for group in quoted]
    count = next(iter(count_values)) if count_values else len(names) if names else None
    if count is None:
        return None
    if names and (len(names) != count or len(names) != len(set(names))):
        raise ValueError('指定列名与列数不一致或列名重复，请明确输出字段')
    tree = sqlglot.parse_one(sql, read=dialect)
    # The final branch determines UNION/INTERSECT output names; validation of
    # compatible set-operation widths remains the database/SQL guard's job.
    while isinstance(tree, exp.SetOperation):
        tree = tree.this
    if not isinstance(tree, exp.Select):
        raise ValueError('未能确认最终SQL输出列，请使用明确的SELECT列表')
    actual = tree.expressions
    if any(isinstance(x, exp.Star) or isinstance(x, exp.Column) and x.is_star for x in actual):
        raise ValueError('问题已明确输出列，不能使用未展开的SELECT *')
    if len(actual) != count:
        raise ValueError(f'问题明确只返回{count}列，SQL最终返回{len(actual)}列；请在SELECT中修正，不新增单位、年份或来源列，不改变原筛选和粒度')
    actual_names = [x.alias_or_name for x in actual]
    if names and actual_names != names:
        raise ValueError('问题指定列名及顺序为'+json.dumps(names,ensure_ascii=False)+'，SQL最终列名为'+json.dumps(actual_names,ensure_ascii=False)+'；请按原要求设置SELECT别名及顺序')
    return {'expected_count':count,'expected_names':names,'actual_count':len(actual),'actual_names':actual_names,'status':'passed'}


def rounded_result_display(sql, columns, raw_rows, dialect="sqlite"):
    """Preserve outer ROUND precision as a view, without rounding data again.

    All set-operation branches must explicitly agree. Text identifiers, NULL,
    unrounded columns and the original result/hash remain untouched.
    """
    import sqlglot
    from sqlglot import exp
    tree = sqlglot.parse_one(sql, read=dialect)
    def branches(node):
        if isinstance(node, exp.SetOperation):
            return branches(node.this) + branches(node.expression)
        return [node] if isinstance(node, exp.Select) else []
    selects = branches(tree)
    if not selects or any(len(node.expressions) != len(columns) for node in selects):
        return {}
    precision = {}
    for index, column in enumerate(columns):
        digits = []
        for select in selects:
            expression = select.expressions[index]
            if isinstance(expression, exp.Alias):
                expression = expression.this
            if not isinstance(expression, exp.Round):
                break
            places = expression.args.get('decimals')
            if places is None:
                digits.append(0)
            elif isinstance(places, exp.Literal) and not places.is_string and re.fullmatch(r'\d+', places.this):
                digits.append(int(places.this))
            else:
                break
        if len(digits) == len(selects) and len(set(digits)) == 1 and 0 <= digits[0] <= 12:
            precision[column] = digits[0]
    if not precision:
        return {}
    rows = []
    for raw in raw_rows:
        row = {}
        for column, value in zip(columns, raw):
            shown = _scalar(value)
            if column in precision and (type(value) in (int, float) or isinstance(value, Decimal)):
                decimal = Decimal(str(value))
                # Formatting is bounded and may only pad zeros. A driver value
                # that disagrees with ROUND is left exact, never silently changed.
                if decimal.is_finite() and abs(decimal.adjusted()) <= 512:
                    with localcontext() as context:
                        context.prec = max(28, len(decimal.as_tuple().digits) + abs(decimal.adjusted()) + 16)
                        padded = decimal.quantize(Decimal(1).scaleb(-precision[column]))
                    if padded == decimal:
                        shown = format(padded, 'f')
            row[column] = shown
        rows.append(row)
    return {'display_rows': rows, 'display_decimals': precision,
            'display_policy': 'SQL ROUND precision; zero padding only; raw rows and digest unchanged'}


def validate_question_grain(sql, question, dialect="sqlite"):
    """Reject a proven granularity mismatch, without guessing a business metric.

    This runs after the SQL allowlist. A period filter ("March") is not a request
    to split the answer by date. Internal CTE aggregates remain untouched.
    """
    if not question:
        return
    import sqlglot
    from sqlglot import exp
    tree = sqlglot.parse_one(sql, read=dialect)
    group = tree.args.get("group") if isinstance(tree, exp.Select) else None
    if group is None:
        return
    monthly = bool(re.search(r"按月|每月|逐月|月度|by\s+month|monthly", question, re.I))
    breakdown = bool(re.search(r"分别|每[个台位种]|各[个台位种部地]|分组|按.{1,24}(?:统计|计数|计算|分组|汇总)|按(?:日|天|周|年|地区|区域|产品|站点)|排名|前\s*\d", question))
    single_total = bool(re.search(r"只返回(?:一个|单个)?(?:总数|计数)|(?:总计|合计|总数|共有|有多少)", question))
    if single_total and not monthly and not breakdown:
        # An equality in the outer WHERE fixes this exact column to one value.
        # Grouping by that column cannot split the answer into multiple rows.
        # Do not infer this from OR, HAVING, ranges or a nested subquery, and
        # never rewrite empty-group semantics into a manufactured zero row.
        fixed=set()
        def column_key(column):
            return tuple(getattr(column,key).casefold() for key in ('catalog','db','table','name'))
        def conjunctions(node):
            while isinstance(node, exp.Paren):node=node.this
            if isinstance(node, exp.And):
                yield from conjunctions(node.this);yield from conjunctions(node.expression)
            else:yield node
        where=tree.args.get('where')
        if where:
            for predicate in conjunctions(where.this):
                if not isinstance(predicate,exp.EQ):continue
                for column,value in [(predicate.this,predicate.expression),(predicate.expression,predicate.this)]:
                    if isinstance(column,exp.Column) and isinstance(value,(exp.Literal,exp.Placeholder)):
                        fixed.add(column_key(column))
        def resolved_group(item):
            if isinstance(item,exp.Literal) and item.is_int:
                position=int(item.this)-1
                if 0<=position<len(tree.expressions):item=tree.expressions[position]
            if isinstance(item,exp.Alias):item=item.this
            if isinstance(item,exp.Column) and not item.table:
                aliases=[x.this for x in tree.expressions if isinstance(x,exp.Alias) and x.alias.casefold()==item.name.casefold()]
                if len(aliases)==1:item=aliases[0]
            return item
        keys=[resolved_group(item) for item in group.expressions]
        if not keys or not all(isinstance(item,exp.Column) and
                column_key(item) in fixed for item in keys):
            raise ValueError("问题要求一个总计数或合计，SQL 却额外分组；仅允许WHERE已固定为单值的分组键，其余请去掉未请求的GROUP BY，保留原过滤条件")
    if not monthly:
        return
    for item in group.expressions:
        if isinstance(item, exp.Literal) and item.is_int:
            position = int(item.this) - 1
            if 0 <= position < len(tree.expressions):
                item = tree.expressions[position]
                if isinstance(item, exp.Alias):
                    item = item.this
        expression = item
        rendered = expression.sql(dialect=dialect).casefold()
        columns = list(item.find_all(exp.Column))
        temporal_pattern = r"(?:date|time|_at$|_on$|(?:^|_)day$|日期|时间|年月|月份)"
        temporal = any(re.search(temporal_pattern, col.name, re.I) for col in columns)
        # GROUP BY can reference a SELECT alias or ordinal.
        if isinstance(item, exp.Column) and not item.table:
            aliases = [x.this for x in tree.expressions if isinstance(x, exp.Alias) and x.alias.casefold() == item.name.casefold()]
            if aliases:
                expression = aliases[0]
                rendered = expression.sql(dialect=dialect).casefold()
                temporal = any(re.search(temporal_pattern, col.name, re.I) for col in expression.find_all(exp.Column))
        if not temporal:
            continue
        if not isinstance(expression, exp.Column):
            # A source named day is not a daily output when STRFTIME extracts
            # only year/month. Check function/format structure, not input names.
            rendered = expression.copy().transform(lambda node:
                exp.column('_temporal_input') if isinstance(node, exp.Column) else node).sql(dialect=dialect).casefold()
        has_month = bool(re.search(r"%m|yyyy-mm|year_month|\bmonth\b|(?:substr|substring)\(.+,\s*1,\s*7\)", rendered))
        has_day = bool(re.search(r"%d|yyyy-mm-dd|\bday\b", rendered))
        if not has_month or has_day:
            raise ValueError("问题要求按月汇总，SQL 却按原始日期或日分组；请使用年月粒度，保留原条件和其他明确维度")


def entity_ambiguities(question, snapshot, connection_id=None, expected_revision=None, cancelled=None):
    """Bounded exact-name lookups for explicitly singular entity questions.

    Only names literally present in the question and their keys are returned.
    This is neither a sample dump nor fuzzy entity guessing. A caller may show
    these keys for clarification; no business query is run on an arbitrary match.
    """
    if not isinstance(question, str) or len(question) > 2000:
        raise ValueError("实体消歧问题应为不超过2000字的文字")
    singular = re.search(r"只(?:指|查|看|问).*(?:一[个台位]|其中)|其中一[个台位]|单个|哪[个台位].*(?:属于|对应)|(?:属于|对应)哪[个台位]|是哪[个台位]", question)
    plural = re.search(r"所有|全部|每[个台位]|各[个台位]|分别|同名的?(?:都|全部)|两台|多台", question)
    identifier = re.search(r"(?:编号|\bid\b|\b\w+_id)\s*(?:为|是|[:：=])?\s*[A-Za-z0-9_-]+", question, re.I)
    if not singular or plural or identifier:
        return []
    if not isinstance(snapshot, dict) or snapshot.get("source", {}).get("connection_id") != connection_id:
        raise ValueError("实体消歧缺少当前源的字段快照")
    def operation(stop, deadline):
        config = _configuration(connection_id)
        if expected_revision is not None and config["revision"] != expected_revision:
            raise ValueError("数据库连接版本已改变，请重新读取数据结构")
        with _database(config, stop, deadline) as db:
            names = [t["qualified_name"] for t in snapshot.get("tables", [])]
            current = _inspect_on(db, config, names, stop, deadline)
            if current["source"]["fingerprint"] != snapshot["source"]["fingerprint"]:
                raise ValueError("数据库结构指纹已改变，请重新读取字段和关联")
            findings, probes = [], 0
            for table in current["tables"]:
                keys = table.get("primary_key", [])
                if not keys:
                    continue
                for field in table["columns"]:
                    name = field["name"]
                    if not re.search(r"(?:^name$|_name$|^title$|名称$|姓名$)", name, re.I) or name in keys:
                        continue
                    _guard(stop, deadline)
                    probes += 1
                    if probes > 12:
                        return findings
                    column = _quote(config, name)
                    placeholder = "?" if config["kind"] == "sqlite" else "%s"
                    containment = (f"INSTR({placeholder},{column})>0" if config["kind"] == "sqlite" else
                                   f"STRPOS({placeholder},{column})>0" if config["kind"] == "postgresql" else
                                   f"LOCATE({column},{placeholder})>0")
                    columns = list(dict.fromkeys(keys + [name]))
                    sql = "SELECT " + ",".join(_quote(config, k) for k in columns) + " FROM " + _qualified(config, table)
                    sql += f" WHERE LENGTH({column})>=2 AND {containment} LIMIT 21"
                    rows = _read(db, sql, [question], limit=21)
                    grouped = {}
                    for row in rows:
                        value = row.get(name)
                        if isinstance(value, str) and value in question:
                            grouped.setdefault(value, []).append({k: _scalar(row[k]) for k in keys})
                    for value, candidates in grouped.items():
                        unique = {json.dumps(x, sort_keys=True, ensure_ascii=False): x for x in candidates}
                        if len(unique) > 1:
                            findings.append({"table": table["qualified_name"], "name_field": name,
                                             "name": value, "key_fields": keys,
                                             "candidates": list(unique.values())[:10], "truncated": len(rows) > 20 or len(unique) > 10})
            return findings
    return _bounded(operation, cancelled)


def dimension_conditions(previous):
    value = (previous or {}).get('semantic_conditions')
    if not isinstance(value, dict):
        return {}
    out = {}
    if type(value.get('year')) is int and 1000 <= value['year'] <= 9999:
        out['year'] = value['year']
    types = value.get('population_type')
    if isinstance(types, list) and 1 <= len(types) <= 12 and all(isinstance(x, str) and 1 <= len(x) <= 80 for x in types):
        out['population_type'] = list(dict.fromkeys(types))
    return out


def dimension_clarification(question, snapshot, connection_id=None, expected_revision=None, previous=None, cancelled=None):
    """Ask about missing dimensions of time-indexed measures, not all records.

    Only a time-keyed measurement table is considered. Distinct year/type values
    are bounded metadata; definitions and deliberate all-period requests bypass
    this guard. A model still owns schema linking and scope interpretation.
    """
    text = str(question or '')
    previous = previous or {}
    original = str(previous.get('original_question') or previous.get('question', ''))
    if previous.get('clarification_question') and not re.search(r'只.*(?:解释|定义|含义|口径)|仅.*解释|不查数', text):
        text = original + '；用户补充：' + text
    unclear_ranking=bool(re.search(r'(?:发展|表现|综合实力|综合水平).{0,12}(?:最好|最优|排名)|(?:最好|最优).{0,8}(?:发展|表现)',text)) and not re.search(r'GDP|生产总值|增加值|人均|营收|收入|人口|浓度|revenue|population',text,re.I)
    numeric = unclear_ranking or bool(re.search(r'多少|数值|人数|数量|金额|合计|总计|排名|排序|比较|对比|占比|比例|GDP|增加值|人口|浓度', text, re.I))
    explanatory = bool(re.search(r'定义|含义|是什么|口径|计算方法|怎么算|如何计算|为什么|为何|能.*比较|可.*比较|历史|沿革|解释', text))
    requests_values = bool(re.search(r'多少|(?:给出|列出).*(?:数值|金额|人数|数据)|排名|排序|合计|总计|人口(?:总量|数量)', text))
    if not numeric or explanatory and not requests_values:
        return None
    if re.search(r'哪些.{0,12}(?:指标|年份|表|字段)|(?:指标|年份|表|字段).*(?:有哪些|清单|列表|种类|有多少)', text):
        return None
    statistical_request = bool(re.search(r'GDP|生产总值|增加值|人口|浓度|统计指标|年度指标|年度数据|观测值|营收|财政收入|居民收入|固定资产投资|revenue|population', text, re.I))
    if not isinstance(snapshot, dict) or snapshot.get('source', {}).get('connection_id') != connection_id:
        raise ValueError('统计口径检查缺少当前源的字段快照')
    candidates = []
    for table in snapshot.get('tables', []):
        fields = {f['name'].casefold(): f for f in table['columns']}
        if 'year' not in fields:
            continue
        measures = [f for name, f in fields.items() if name not in {'year', 'preliminary'}
                    and not name.endswith('_id') and re.search(r'REAL|NUMERIC|DECIMAL|DOUBLE|FLOAT|INT', f.get('type', ''), re.I)]
        # Birth/manufacture years in an ordinary entity table are not a missing
        # statistical period. We need time in the grain or an indicator key.
        named_measure = any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(f['name']) + r'(?![A-Za-z0-9_])', text, re.I) for f in measures)
        if measures and (statistical_request or named_measure or unclear_ranking) and ('year' in {k.casefold() for k in table.get('primary_key', [])}
                         or {'metric_id', 'indicator_id'} & fields.keys()):
            candidates.append(table)
    if not candidates:
        return None
    if unclear_ranking:
        return {'title':'请明确用于排名的指标、统计时期和评价规则；“发展或表现最好”没有统一比较口径，不能默认用某一项指标给出综合排名。',
                'dimensions':{'comparison_metric':{'options':[],'truncated':False}},'specified':dimension_conditions(previous)}
    inherited_text = str(previous.get('question', ''))
    known = dimension_conditions(previous)
    changed_year = bool(re.search(r'另.*年|其他年|换.*年', text))
    year_requested = bool(re.search(r'(?:19|20)\d{2}|\d{4}\s*年|今年|去年|上年|本年|最新|历年|历史|所有年|全部年|各年|每年|逐年|按年|不限.*年', text))
    if not year_requested and not changed_year:
        year_requested = bool(known.get('year') or re.search(r'(?:19|20)\d{2}|最新|历年|每年|逐年|按年', inherited_text))
        if not year_requested and previous.get('last_sql'):
            import sqlglot
            from sqlglot import exp
            try:
                old = sqlglot.parse_one(previous['last_sql'], read=snapshot.get('dialect', 'sqlite'))
                year_requested = any(col.name.casefold() == 'year' for where in old.find_all(exp.Where) for col in where.find_all(exp.Column))
            except (ValueError, sqlglot.errors.SqlglotError):
                pass
    wants_population = '人口' in text or 'population' in text.casefold()
    # Published per-capita indicators can be compared while their population
    # definition is explained. That does not request a population total or
    # authorize recomputing the indicator using a different denominator.
    definition_only=bool(re.search(r'(?:说明|注明|核对).{0,8}人口口径',text))
    population_value=bool(re.search(r'人口.{0,5}(?:总量|数量|多少|合计|排名)|(?:合计|比较|对比).{0,6}人口',text))
    if definition_only and not population_value:
        wants_population=False
    def operation(stop, deadline):
        config = _configuration(connection_id)
        if expected_revision is not None and config['revision'] != expected_revision:
            raise ValueError('数据库连接版本已改变，请重新读取数据结构')
        values = {'year': set(), 'population_type': set()}
        truncated = set()
        with _database(config, stop, deadline) as db:
            for table in candidates[:12]:
                fields = {f['name'].casefold(): f['name'] for f in table['columns']}
                for dim in ('year', 'population_type'):
                    if dim not in fields or dim == 'year' and year_requested or dim == 'population_type' and not wants_population:
                        continue
                    _guard(stop, deadline)
                    column = _quote(config, fields[dim])
                    rows = _read(db, 'SELECT DISTINCT ' + column + ' AS dimension_value FROM ' + _qualified(config, table)
                        + ' WHERE ' + column + ' IS NOT NULL ORDER BY ' + column + ' LIMIT 13', limit=13)
                    if len(rows) > 12:
                        truncated.add(dim)
                    for row in rows[:12]:
                        value = row['dimension_value']
                        if dim == 'year' and str(value).isdigit() and len(str(value)) == 4:
                            values[dim].add(str(value))
                        elif dim == 'population_type' and isinstance(value, str) and 1 <= len(value) <= 80:
                            values[dim].add(value)
        choices = {}
        specified = dict(known)
        explicit_year = re.findall(r'(?:19|20)\d{2}', text)
        if len(set(explicit_year)) == 1:
            specified['year'] = int(explicit_year[0])
        if not year_requested and len(values['year']) > 1:
            choices['year'] = {'options': sorted(values['year']), 'truncated': 'year' in truncated}
        prior_type = known.get('population_type')
        current_types = [value for value in values['population_type'] if value in text or
                         value.removesuffix('人口') and value.removesuffix('人口') in text]
        if current_types:
            specified['population_type'] = sorted(current_types)
        population_specified = bool(current_types or prior_type or re.search(r'常住|户籍|外来|全部口径|所有口径|分别.*口径', text + inherited_text))
        if wants_population and not population_specified and len(values['population_type']) > 1:
            choices['population_type'] = {'options': sorted(values['population_type']), 'truncated': 'population_type' in truncated}
        if not choices:
            return None
        prompts = []
        if 'year' in choices:
            prompts.append('请选择统计年度（资料包含 ' + '、'.join(choices['year']['options']) + ' 年），或明确要求最新/历年数据')
        if 'population_type' in choices:
            prompts.append('请选择人口口径（' + '、'.join(choices['population_type']['options']) + '），或明确要求分别展示；不同口径不能混合比较')
        if truncated:
            prompts.append('候选较多，仅列部分选项，请补充明确范围')
        return {'title': '；'.join(prompts) + '。', 'dimensions': choices, 'specified': specified}
    return _bounded(operation, cancelled)


def execute(sql, params=None, connection_id=None, expected_revision=None, expected_fingerprint=None,
            cancelled=None, snapshot=None, question=None):
    from . import sql_guard
    def operation(stop, deadline):
        config = _configuration(connection_id)
        if expected_revision is not None and expected_revision != config["revision"]:
            raise ValueError("数据库连接版本已改变，请重新读取数据结构")
        names = None
        if snapshot is not None:
            if not isinstance(snapshot, dict) or snapshot.get("source", {}).get("connection_id") != connection_id:
                raise ValueError("数据结构快照不属于当前数据库源")
            names = [t["qualified_name"] for t in snapshot.get("tables", [])]
            if not names:
                raise ValueError("请先选择相关表并取得字段快照")
        with _database(config, stop, deadline) as db:
            current = _inspect_on(db, config, names, stop, deadline)
            if current["needs_selection"]:
                raise ValueError("数据库超过 50 张表，请先选择相关表后查询")
            expected = expected_fingerprint or (snapshot or {}).get("source", {}).get("fingerprint")
            if expected is not None and expected != current["source"]["fingerprint"]:
                raise ValueError("数据库结构指纹已改变，请重新读取字段和关联")
            guarded = sql_guard.validate(sql, guard_schema(current), dialect=current["dialect"], max_rows=MAX_ROWS + 1, max_nodes=500)
            validate_question_grain(guarded["sql"], question, current["dialect"])
            projection = validate_question_projection(guarded["sql"], question, current["dialect"])
            if projection:
                guarded['question_projection'] = projection
                guarded['checks'].append({'name':'用户明确的输出列数、列名与顺序','status':'passed'})
            values = _parameters(params, guarded.get("parameter_count", 0))
            _guard(stop, deadline)
            if config["kind"] == "sqlite":
                allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
                table_names = {t["name"] for t in current["tables"]}
                def authorize(action, a, b, database, source):
                    if action not in allowed:
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and (database != "main" or a not in table_names):
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION and not sql_guard.allowed_sqlite_function(b or a or ""):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
            elif config["kind"] == "postgresql":
                milliseconds = max(1, int((deadline - time.monotonic()) * 1000))
                db.execute("SET LOCAL statement_timeout=" + str(milliseconds))
            else:
                milliseconds = max(1, int((deadline - time.monotonic()) * 1000))
                with db.cursor() as setting:
                    setting.execute("SET SESSION max_execution_time=" + str(milliseconds))
            actual_sql = guarded.get("driver_sql", guarded["sql"]) if values and config["kind"] != "sqlite" else guarded["sql"]
            cursor = db.cursor()
            try:
                cursor.execute(actual_sql, tuple(values)) if values else cursor.execute(actual_sql)
                columns = [str(item[0]) for item in cursor.description]
                if len(columns) != len(set(columns)):
                    raise ValueError("结果字段重名，请为结果列设置唯一别名")
                raw = cursor.fetchmany(MAX_ROWS + 1)
                rows = [{key: _scalar(value) for key, value in zip(columns, row)} for row in raw[:MAX_ROWS]]
            finally:
                cursor.close()
            _guard(stop, deadline)
            if len(c.encode(rows).encode()) > MAX_RESULT_BYTES:
                raise ValueError("查询结果超过 2 MB，请缩小返回列或范围")
            source = current["source"]
            digest = _hash({"columns": columns, "rows": rows})
            locator = {"query_scope": "database", "sql": guarded["sql"], "params": values,
                       "columns": columns, "rows": rows, "row_count": len(rows), "row_limit": MAX_ROWS,
                       "truncated": len(raw) > MAX_ROWS, "source": source, "version": source["fingerprint"],
                       "result_sha256": digest, "dialect": current["dialect"]}
            evidence = {"id": "database-" + uuid.uuid4().hex, "type": "sql_result", "source": source["name"],
                        "snippet": "已执行经过字段与关联检查的只读查询；来源版本指向结构快照，返回行另有摘要。",
                        "status": "executed", "locator": locator}
            display = rounded_result_display(guarded["sql"], columns, raw[:MAX_ROWS], current["dialect"])
            return {"rows": rows, "columns": columns, "row_count": len(rows), "row_limit": MAX_ROWS, **display,
                    "truncated": len(raw) > MAX_ROWS, "sql": guarded["sql"], "params": values,
                    "dialect": current["dialect"], "source": source, "result_sha256": digest, "evidence": evidence,
                    "checks": guarded["checks"], "relations": guarded["relations"], "tables": guarded["tables"],
                    "validation": {k: copy.deepcopy(v) for k, v in guarded.items() if k != "driver_sql"}}
    return _bounded(operation, cancelled)
