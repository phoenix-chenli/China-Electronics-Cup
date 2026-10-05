"""Disposable SQLite integration and external-driver protocol contracts.

PostgreSQL/MySQL fixtures prove parameter and read-only contracts only; they do
not claim connectivity, privileges or SQL results on a real external server.
"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="evidenceflow-database-test-")
os.environ["EVIDENCEFLOW_DATA_DIR"] = TEMP.name
from backend import connections as c, database_query as q, seed, settings, store as s


class DriverCursor:
    def __init__(self, db):
        self.db, self.description, self.values = db, [], []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
    def execute(self, sql, params=None):
        self.db.calls.append((sql, params))
        schema = "public" if self.db.kind == "postgresql" else "demo"
        if "information_schema.tables" in sql:
            columns, rows = ["schema", "name"], [(schema, "accounts")]
        elif "information_schema.columns" in sql:
            columns = ["column_name", "data_type", "is_nullable"]
            rows = [("id", "integer", "NO"), ("amount", "decimal", "YES"), ("note", "text", "YES")]
        elif "information_schema.table_constraints" in sql:
            columns = ["constraint_name", "constraint_type", "column_name", "ordinal_position"]
            rows = [("accounts_pkey", "PRIMARY KEY", "id", 1)]
        elif "pg_catalog.pg_constraint" in sql or "referenced_table_name IS NOT NULL" in sql:
            columns, rows = ["constraint_name"], []
        elif sql.lstrip().upper().startswith("SELECT"):
            if self.db.entered is not None:
                self.db.entered.set()
                self.db.release.wait(2)
            columns, rows = ["total"], [(Decimal("123.45"),)]
        else:
            columns, rows = [], []
        self.description = [(name,) for name in columns]
        self.values = rows
        return self
    def fetchmany(self, limit):
        out, self.values = self.values[:limit], self.values[limit:]
        return out
    def close(self):
        pass


class DriverDatabase:
    def __init__(self, kind):
        self.kind, self.calls, self.closed = kind, [], False
        self.entered = self.release = None
    def cursor(self):
        return DriverCursor(self)
    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)
    def close(self):
        self.closed = True


class DatabaseQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
        seed.seed_business()
        settings.initialize()
        c.initialize()
        q.initialize()
        cls.fixture = s.DATA / "external-fixture.sqlite"
        with sqlite3.connect(cls.fixture, factory=s.ClosingConnection) as db:
            db.executescript('''
                CREATE TABLE customers(customer_id INTEGER PRIMARY KEY,email TEXT UNIQUE,name TEXT);
                CREATE TABLE orders(order_id INTEGER PRIMARY KEY,customer_id INTEGER REFERENCES customers(customer_id),amount NUMERIC,note TEXT);
                CREATE TABLE loose_orders(order_id INTEGER,customer_id INTEGER,amount NUMERIC);
                CREATE TABLE orphan_orders(order_id INTEGER,customer_id INTEGER);
                CREATE TABLE duplicate_parent(customer_id INTEGER);
                INSERT INTO customers VALUES (1,'one@example.test','甲'),(2,'two@example.test','乙');
                INSERT INTO orders VALUES (1,1,12.50,'100%'),(2,2,24.75,'字符串%s');
                INSERT INTO loose_orders VALUES (1,1,3),(2,2,4);
                INSERT INTO orphan_orders VALUES (1,999);
                INSERT INTO duplicate_parent VALUES (1),(1);
            ''')
            db.executemany("INSERT INTO orders VALUES (?,?,?,?)", [(i, 1, 1, "行") for i in range(3, 253)])

    @classmethod
    def tearDownClass(cls):
        q._pool.shutdown(wait=True)
        TEMP.cleanup()

    def setUp(self):
        for item in c.listing():
            c.delete(item["id"])
        self.connection = c.create(c.Config(name="临时只读库", kind="sqlite", path=str(self.fixture)))
        tested = c.test(self.connection["id"])
        self.assertTrue(tested["last_test"]["ok"])
        self.connection = c.enabled(self.connection["id"], c.Enabled(enabled=True))

    def inspect_fixture(self, *names):
        return q.inspect(self.connection["id"], list(names) if names else None)

    def execute_fixture(self, sql, params=None, **extra):
        return q.execute(sql, params, connection_id=self.connection["id"], **extra)

    def test_local_source_has_real_fields_keys_and_no_samples_credentials_or_paths(self):
        snapshot = q.inspect()
        orders = next(t for t in snapshot["tables"] if t["name"] == "orders")
        self.assertEqual(orders["primary_key"], ["order_id"])
        self.assertTrue(any(f["name"] == "sales_amount" for f in orders["columns"]))
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(str(s.DATA), encoded)
        self.assertNotIn("sample", encoded)
        self.assertNotIn("username", encoded)
        self.assertNotIn("_secret", encoded)
        self.assertEqual(snapshot["source"]["connection_id"], None)
        self.assertEqual(snapshot["source"]["fingerprint_scope"], "schema_snapshot")
        self.assertEqual(len(snapshot["source"]["fingerprint"]), 64)

    def test_foreign_keys_and_candidates_require_unique_target_and_value_coverage(self):
        snapshot = self.inspect_fixture("customers", "orders", "loose_orders", "orphan_orders")
        declared = next(r for r in snapshot["relations"] if r["left_table"] == "main.orders")
        self.assertTrue(declared["declared"])
        self.assertTrue(declared["verified"])
        good = next(r for r in snapshot["relation_candidates"] if r["left_table"] == "main.loose_orders")
        bad = next(r for r in snapshot["relation_candidates"] if r["left_table"] == "main.orphan_orders")
        self.assertFalse(good["declared"])
        self.assertTrue(good["verified"])
        self.assertEqual(good["source"], "verified_candidate")
        self.assertFalse(bad["verified"])
        self.assertIn("未覆盖", bad["verification"])
        guard = q.guard_schema(snapshot)
        self.assertTrue(all(r["verified"] for r in guard["relations"]))
        self.assertNotIn(bad["id"], {r["id"] for r in guard["relations"]})

    def test_generic_join_aggregate_and_nested_query_execute_real_fixture(self):
        snapshot = self.inspect_fixture("orders", "customers")
        result = self.execute_fixture("SELECT c.name,SUM(o.amount) AS total FROM orders o "
            "JOIN customers c ON o.customer_id=c.customer_id GROUP BY c.name ORDER BY total DESC", snapshot=snapshot)
        self.assertEqual(result["columns"], ["name", "total"])
        self.assertEqual(result["rows"], [{"name": "甲", "total": 262.5}, {"name": "乙", "total": 24.75}])
        nested = self.execute_fixture("SELECT name FROM customers WHERE customer_id IN "
            "(SELECT customer_id FROM orders WHERE amount>?) ORDER BY name", [20], snapshot=snapshot)
        self.assertEqual(nested["rows"], [{"name": "乙"}])
        self.assertTrue(result["checks"])
        self.assertTrue(result["relations"])

    def test_result_limit_empty_columns_and_evidence_snapshot_are_truthful(self):
        result = self.execute_fixture("SELECT order_id,amount FROM orders ORDER BY order_id")
        self.assertEqual(len(result["rows"]), 200)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["row_limit"], 200)
        self.assertIn("201", result["sql"])
        locator = result["evidence"]["locator"]
        self.assertEqual(locator["query_scope"], "database")
        self.assertEqual(locator["rows"], result["rows"])
        self.assertEqual(locator["sql"], result["sql"])
        self.assertEqual(locator["result_sha256"], result["result_sha256"])
        self.assertEqual(locator["version"], result["source"]["fingerprint"])
        empty = self.execute_fixture("SELECT order_id FROM orders WHERE order_id<?", [0])
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["columns"], ["order_id"])
        self.assertFalse(empty["truncated"])

    def test_writes_functions_system_tables_and_bad_parameters_cannot_escape(self):
        before = s.digest(self.fixture)
        for sql, params in [("DELETE FROM orders", None), ("SELECT * FROM sqlite_master", None),
                            ("SELECT load_extension('anything')", None),
                            ("SELECT amount FROM orders WHERE order_id=?", []),
                            ("SELECT amount FROM orders WHERE order_id=?", [float("nan")])]:
            with self.subTest(sql=sql, params=params), self.assertRaises(ValueError):
                self.execute_fixture(sql, params)
        self.assertEqual(s.digest(self.fixture), before)
        value = self.execute_fixture("SELECT note FROM orders WHERE note=?", ["字符串%s"])
        self.assertEqual(value["rows"], [{"note": "字符串%s"}])

    def test_expected_revision_and_schema_fingerprint_reject_stale_source(self):
        snapshot = self.inspect_fixture("orders")
        c.enabled(self.connection["id"], c.Enabled(enabled=True))
        with self.assertRaisesRegex(ValueError, "版本已改变"):
            self.execute_fixture("SELECT order_id FROM orders", expected_revision=snapshot["source"]["revision"])
        snapshot = self.inspect_fixture("orders")
        with sqlite3.connect(self.fixture, factory=s.ClosingConnection) as db:
            db.execute("ALTER TABLE orders ADD COLUMN temporary_test TEXT")
        try:
            with self.assertRaisesRegex(ValueError, "结构指纹已改变"):
                self.execute_fixture("SELECT order_id FROM orders", snapshot=snapshot)
        finally:
            with sqlite3.connect(self.fixture, factory=s.ClosingConnection) as db:
                db.execute("ALTER TABLE orders DROP COLUMN temporary_test")

    def test_disabled_or_untested_external_connection_is_rejected(self):
        c.enabled(self.connection["id"], c.Enabled(enabled=False))
        with self.assertRaisesRegex(ValueError, "测试并启用"):
            self.inspect_fixture()
        fresh = c.create(c.Config(name="未测试", kind="sqlite", path=str(self.fixture)))
        with self.assertRaisesRegex(ValueError, "测试并启用"):
            q.execute("SELECT amount FROM orders", connection_id=fresh["id"])

    def test_more_than_fifty_tables_requires_selection_without_silent_catalog_loss(self):
        with sqlite3.connect(s.BUSINESS_DB, factory=s.ClosingConnection) as db:
            for i in range(51):
                db.execute('CREATE TABLE "noise_' + str(i) + '" (id INTEGER)')
        try:
            snapshot = q.inspect()
            self.assertTrue(snapshot["needs_selection"])
            self.assertEqual(snapshot["tables"], [])
            self.assertEqual(len(snapshot["table_listing"]), 57)
            with self.assertRaisesRegex(ValueError, "50 张表"):
                q.execute("SELECT order_id FROM orders")
            scoped = q.inspect(table_names=["main.orders", "main.products"])
            self.assertFalse(scoped["needs_selection"])
            result = q.execute("SELECT order_id FROM orders ORDER BY order_id", snapshot=scoped)
            self.assertTrue(result["rows"])
        finally:
            with sqlite3.connect(s.BUSINESS_DB, factory=s.ClosingConnection) as db:
                for i in range(51):
                    db.execute('DROP TABLE "noise_' + str(i) + '"')

    def test_import_metadata_is_not_trusted_without_current_data_verification(self):
        with sqlite3.connect(s.BUSINESS_DB, factory=s.ClosingConnection) as db:
            db.executescript("CREATE TABLE imported_parent(customer_id INTEGER);"
                             "CREATE TABLE imported_child(customer_id INTEGER,amount REAL);"
                             "INSERT INTO imported_parent VALUES(1),(1);"
                             "INSERT INTO imported_child VALUES(1,10);")
        parent = {"primary_key": ["customer_id"], "unique_keys": [], "foreign_keys": []}
        child = {"foreign_keys": [{"columns": ["customer_id"], "referenced_table": "imported_parent",
                                    "referenced_columns": ["customer_id"]}]}
        with s.conn() as db:
            db.executemany("INSERT INTO ef_database_import_metadata VALUES(?,?)", [
                ("imported_parent", json.dumps(parent)), ("imported_child", json.dumps(child))])
        try:
            snapshot = q.inspect(table_names=["imported_parent", "imported_child"])
            relation = next(r for r in snapshot["relation_candidates"] if r["left_table"] == "main.imported_child")
            self.assertFalse(relation["declared"])
            self.assertFalse(relation["verified"])
            self.assertIn("重复", relation["verification"])
            self.assertEqual(q.guard_schema(snapshot)["relations"], [])
            with sqlite3.connect(s.BUSINESS_DB, factory=s.ClosingConnection) as db:
                db.execute("DELETE FROM imported_parent WHERE rowid=(SELECT MAX(rowid) FROM imported_parent)")
            current = q.inspect(table_names=["imported_parent", "imported_child"])
            self.assertTrue(q.guard_schema(current)["relations"][0]["verified"])
        finally:
            with s.conn() as db:
                db.execute("DELETE FROM ef_database_import_metadata WHERE name IN ('imported_parent','imported_child')")
            with sqlite3.connect(s.BUSINESS_DB, factory=s.ClosingConnection) as db:
                db.executescript("DROP TABLE imported_child;DROP TABLE imported_parent;")

    def external_config(self, kind):
        return {"id": "fixture-" + kind, "revision": 3, "name": "外部协议夹具", "kind": kind,
                "host": "127.0.0.1", "port": 5432 if kind == "postgresql" else 3306,
                "database": "demo", "username": "fixture-user", "_secret": "fixture-only-secret"}

    def test_postgresql_and_mysql_protocol_apply_readonly_timeouts_and_driver_parameters(self):
        for kind, connect_target in [("postgresql", "psycopg.connect"), ("mysql", "pymysql.connect")]:
            with self.subTest(kind=kind):
                config = self.external_config(kind)
                db = DriverDatabase(kind)
                with patch.object(c, "ready", return_value=config) as ready, patch(connect_target, return_value=db) as connect:
                    result = q.execute("SELECT SUM(amount) AS total FROM accounts WHERE note=?", ["100%"], connection_id=config["id"])
                ready.assert_called_once_with(config["id"], c.DB_KINDS)
                self.assertEqual(result["rows"], [{"total": "123.45"}])
                self.assertTrue(db.closed)
                self.assertLessEqual(connect.call_args.kwargs["connect_timeout"], 5)
                self.assertTrue(any("READ ONLY" in sql for sql, _ in db.calls))
                if kind == "mysql":
                    self.assertLessEqual(connect.call_args.kwargs["read_timeout"], 5)
                    self.assertFalse(connect.call_args.kwargs["local_infile"])
                    self.assertTrue(any("max_execution_time" in sql for sql, _ in db.calls))
                else:
                    self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
                    self.assertTrue(any("statement_timeout" in sql for sql, _ in db.calls))
                    self.assertIn(("SET LOCAL search_path=pg_catalog", None), db.calls)
                user_sql, bound = db.calls[-1]
                self.assertIn("%s", user_sql)
                self.assertEqual(bound, ("100%",))
                self.assertNotIn(config["_secret"], json.dumps(result, ensure_ascii=False))
                self.assertNotIn(config["username"], json.dumps(result, ensure_ascii=False))

    def test_cancelled_external_read_returns_no_result_and_worker_closes_source(self):
        config = self.external_config("postgresql")
        db = DriverDatabase("postgresql")
        db.entered, db.release = threading.Event(), threading.Event()
        stop, errors = threading.Event(), []
        def execute():
            try:
                q.execute("SELECT SUM(amount) AS total FROM accounts", connection_id=config["id"], cancelled=stop)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(c, "ready", return_value=config), patch("psycopg.connect", return_value=db):
            worker = threading.Thread(target=execute)
            worker.start()
            try:
                self.assertTrue(db.entered.wait(1))
                stop.set()
                worker.join(.5)
                self.assertFalse(worker.is_alive())
                self.assertIsInstance(errors[0], InterruptedError)
            finally:
                db.release.set()
                worker.join(2)
                deadline = time.monotonic() + 1
                while not db.closed and time.monotonic() < deadline:
                    time.sleep(.01)
        self.assertTrue(db.closed)

    def test_presignaled_cancel_and_total_budget_stop_before_a_query_result(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            self.execute_fixture("SELECT amount FROM orders", cancelled=stop)
        with patch.object(q, "MAX_SECONDS", 0), self.assertRaisesRegex(ValueError, "预算"):
            self.inspect_fixture()

    def test_native_sqlite_execution_cancellation_closes_without_returning_rows(self):
        snapshot = self.inspect_fixture("orders")
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        stop, errors, results = threading.Event(), [], []
        original_connect = sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            user_sql = False
            def set_authorizer(self, callback):
                self.user_sql = True
                return super().set_authorizer(callback)
            def set_progress_handler(self, callback, count):
                def observed():
                    if self.user_sql:
                        entered.set()
                        release.wait(2)
                    return callback()
                return super().set_progress_handler(observed, count)
            def close(self):
                try:
                    super().close()
                finally:
                    closed.set()
        def connect(*args, **kwargs):
            if kwargs.get("uri"):
                kwargs["factory"] = ObservedConnection
            return original_connect(*args, **kwargs)
        def execute():
            try:
                results.append(self.execute_fixture(
                    "SELECT SUM(LENGTH(REPLACE(note,note,note))) total FROM orders", snapshot=snapshot, cancelled=stop))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(q.sqlite3, "connect", side_effect=connect):
            worker = threading.Thread(target=execute)
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "must reach the native SQLite VM progress callback")
                stop.set()
                worker.join(.5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results, [])
                self.assertIsInstance(errors[0], InterruptedError)
            finally:
                release.set()
                worker.join(2)
                self.assertTrue(closed.wait(2))

    def test_sqlite_readonly_authorizer_remains_when_ast_policy_is_bypassed(self):
        from backend import sql_guard
        before = s.digest(self.fixture)
        guarded = {"parameter_count": 0, "checks": [], "relations": [], "tables": ["main.orders"]}
        for unsafe in ("DELETE FROM orders", "CREATE TABLE stolen(id INTEGER)", "SELECT load_extension('anything')"):
            with self.subTest(sql=unsafe), patch.object(sql_guard, "validate", return_value={**guarded, "sql": unsafe}), self.assertRaises(ValueError):
                self.execute_fixture(unsafe)
        self.assertEqual(s.digest(self.fixture), before)

    def test_sqlite_cell_allocation_is_bounded_before_result_serialization(self):
        # The source contains small rows. Expansion happens inside SQLite, so a
        # post-fetch JSON byte limit alone would be too late to bound allocation.
        with patch.object(q, "MAX_RESULT_BYTES", 800):
            with self.assertRaisesRegex(ValueError, "只读操作失败"):
                self.execute_fixture("SELECT GROUP_CONCAT(note) total FROM orders", snapshot=self.inspect_fixture("orders"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
