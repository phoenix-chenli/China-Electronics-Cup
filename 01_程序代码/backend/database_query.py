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
from decimal import Decimal

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


def execute(sql, params=None, connection_id=None, expected_revision=None, expected_fingerprint=None,
            cancelled=None, snapshot=None):
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
            return {"rows": rows, "columns": columns, "row_count": len(rows), "row_limit": MAX_ROWS,
                    "truncated": len(raw) > MAX_ROWS, "sql": guarded["sql"], "params": values,
                    "dialect": current["dialect"], "source": source, "result_sha256": digest, "evidence": evidence,
                    "checks": guarded["checks"], "relations": guarded["relations"], "tables": guarded["tables"],
                    "validation": {k: copy.deepcopy(v) for k, v in guarded.items() if k != "driver_sql"}}
    return _bounded(operation, cancelled)
