"""AST policy tests; external DB dialect checks do not prove live DB support."""
import copy
import sqlite3
import unittest

from backend.sql_guard import allowed_sqlite_function, validate


def catalog(namespace="main"):
    return {"tables": {
        namespace + ".orders": {"columns": {"id": "INTEGER", "product_id": "INTEGER", "amount": "REAL", "year": "INTEGER", "region": "TEXT"}},
        namespace + ".products": {"columns": {"id": "INTEGER", "name": "TEXT"}},
        namespace + ".unrelated": {"columns": {"id": "INTEGER", "note": "TEXT"}},
    }, "relations": [{"left_table": namespace + ".orders", "left_columns": ["product_id"],
                       "right_table": namespace + ".products", "right_columns": ["id"],
                       "source": "foreign_key", "verified": True}]}


class SQLGuardTests(unittest.TestCase):
    def test_alias_syntax_feedback_explains_quotes_without_rewriting_the_query(self):
        with self.assertRaisesRegex(ValueError,'双引号'):
            validate('SELECT amount AS 占比% FROM orders',catalog())
        checked=validate('SELECT amount AS "占比%" FROM orders',catalog())
        self.assertIn('"占比%"',checked['sql'])

    def test_self_join_requires_actual_complete_unique_key_with_fixed_years(self):
        schema={'tables':{'main.stats':{'columns':{'region':'TEXT','metric':'TEXT','year':'INTEGER','value':'REAL'},
            'unique_keys':[{'columns':['region','metric','year']}] }},'relations':[]}
        sql="SELECT b.value-a.value AS difference FROM stats a JOIN stats b ON a.region=b.region AND a.metric=b.metric WHERE a.year=2023 AND b.year=2024"
        checked=validate(sql,schema)
        self.assertEqual(checked['relations'][0]['source'],'filtered_unique_key')
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE stats(region TEXT,metric TEXT,year INTEGER,value REAL,UNIQUE(region,metric,year))')
            db.executemany('INSERT INTO stats VALUES (?,?,?,?)',[('A','gdp',2023,100),('A','gdp',2024,110),('B','gdp',2023,200),('B','gdp',2024,230)])
            self.assertEqual(db.execute(checked['sql']).fetchall(),[(10.0,),(30.0,)])
        for unsafe in [sql.replace(' AND b.year=2024',''),sql.replace('a.year=2023 AND b.year=2024','(a.year=2023 OR a.year=2022) AND b.year=2024')]:
            with self.assertRaises(ValueError):validate(unsafe,schema)
        no_key=copy.deepcopy(schema);no_key['tables']['main.stats'].pop('unique_keys')
        with self.assertRaises(ValueError):validate(sql,no_key)

    def test_import_metadata_unique_key_is_not_used_as_database_constraint(self):
        from backend.database_query import guard_schema
        guarded=guard_schema({'tables':[{'qualified_name':'main.stats','columns':[{'name':'id','type':'INTEGER','primary_key':False}],
            'primary_key':['id'],'unique_keys':[{'columns':['id'],'source':'import_metadata'}]}],'relations':[]})
        self.assertEqual(guarded['tables']['main.stats']['primary_key'],[])
        self.assertEqual(guarded['tables']['main.stats']['unique_keys'],[])

    def test_unknown_physical_column_has_actionable_schema_feedback_without_relaxing_guard(self):
        with self.assertRaisesRegex(ValueError,'字段 p.artist_id.*表 products') as failed:
            validate('SELECT p.artist_id FROM products p',catalog())
        self.assertIn('id、name',str(failed.exception))
        checked=validate('SELECT o.id FROM orders o WHERE EXISTS(SELECT 1 FROM products p WHERE p.id=o.product_id)',catalog())
        self.assertEqual(checked['tables'],['main.orders','main.products'])

    def test_sqlite_final_alias_case_is_preserved_without_changing_catalog_scope(self):
        result=validate('SELECT product_id AS "产品ID",amount AS "Amount_CNY" FROM orders ORDER BY "Amount_CNY" DESC',catalog())
        self.assertEqual(result['unit_origins'],{'产品ID':'main.orders.product_id','Amount_CNY':'main.orders.amount'})
        self.assertEqual(result['tables'],['main.orders'])
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE orders(id INTEGER,product_id INTEGER,amount REAL,year INTEGER,region TEXT)')
            db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)',[(1,2,3,2024,'东'),(2,1,7,2024,'西')])
            cursor=db.execute(result['sql'])
            self.assertEqual([x[0] for x in cursor.description],['产品ID','Amount_CNY'])
            self.assertEqual(cursor.fetchall(),[(1,7),(2,3)])
        self.rejected('SELECT product_id AS ID,amount AS id FROM orders')

    def test_outer_alias_case_does_not_rename_inner_cte_keys(self):
        result=validate('WITH totals AS (SELECT product_id,SUM(amount) AS Sum_CNY FROM orders GROUP BY product_id) SELECT product_id AS "产品ID", Sum_CNY AS "SUM_CNY" FROM totals ORDER BY SUM_CNY DESC',catalog())
        self.assertEqual(result['unit_origins']['SUM_CNY'],'main.orders.amount')
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE orders(id INTEGER,product_id INTEGER,amount REAL,year INTEGER,region TEXT)')
            db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)',[(1,2,3,2024,'东'),(2,1,7,2024,'西')])
            cursor=db.execute(result['sql'])
            self.assertEqual([x[0] for x in cursor.description],['产品ID','SUM_CNY'])
            self.assertEqual(cursor.fetchall(),[(1,7),(2,3)])

    def test_complete_same_origin_group_keys_allow_safe_aggregate_join(self):
        sql='WITH a AS (SELECT product_id,SUM(amount) AS total FROM orders GROUP BY product_id), '
        sql+='b AS (SELECT product_id,COUNT(*) AS count FROM orders GROUP BY product_id) '
        sql+='SELECT a.product_id,a.total,b.count FROM a JOIN b ON a.product_id=b.product_id'
        result=validate(sql,catalog())
        self.assertEqual(result['relations'][0]['source'],'derived_group_key')
        self.assertTrue(result['relations'][0]['verified'])

    def test_partial_composite_or_computed_group_keys_do_not_authorize_join(self):
        prefix='WITH a AS (SELECT product_id,year,SUM(amount) AS total FROM orders GROUP BY product_id,year), '
        with self.assertRaisesRegex(ValueError,'完整'):
            validate(prefix+'b AS (SELECT product_id,COUNT(*) AS count FROM orders GROUP BY product_id) '
                'SELECT a.total,b.count FROM a JOIN b ON a.product_id=b.product_id',catalog())
        with self.assertRaises(ValueError):
            validate('WITH a AS (SELECT product_id+0 AS key,SUM(amount) AS total FROM orders GROUP BY product_id+0), '
                'b AS (SELECT product_id AS key,COUNT(*) AS count FROM orders GROUP BY product_id) '
                'SELECT a.total,b.count FROM a JOIN b ON a.key=b.key',catalog())
        with self.assertRaises(ValueError):
            validate('SELECT a.id FROM orders a JOIN orders b ON a.product_id=b.product_id',catalog())

    def test_raw_projection_can_lookup_complete_same_origin_group_without_fanout(self):
        prefix='WITH actual AS (SELECT id,product_id,amount FROM orders WHERE year=2024), totals AS (SELECT product_id,SUM(amount) AS total FROM actual GROUP BY product_id) '
        sql=prefix+'SELECT a.id,a.product_id,t.total FROM actual a LEFT JOIN totals t ON a.product_id=t.product_id ORDER BY a.id'
        result=validate(sql,catalog())
        self.assertEqual(result['relations'][0]['cardinality'],'many_to_one')
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE orders(id INTEGER,product_id INTEGER,amount REAL,year INTEGER,region TEXT)')
            db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)',[(1,1,4,2024,'东'),(2,1,5,2024,'东'),(3,2,7,2024,'西')])
            self.assertEqual(db.execute(result['sql']).fetchall(),[(1,1,9.0),(2,1,9.0),(3,2,7.0)])
        self.rejected(prefix+'SELECT t.total,a.id FROM totals t LEFT JOIN actual a ON a.product_id=t.product_id')

    def setUp(self):
        self.schema = catalog()

    def rejected(self, sql, **kwargs):
        with self.assertRaises(ValueError, msg=sql):
            validate(sql, self.schema, **kwargs)

    def test_unit_origins_follow_only_direct_columns_and_preserving_aggregates(self):
        result=validate('SELECT SUM(amount) AS total, COUNT(amount) AS n, SUM(amount)*2 AS changed FROM orders',self.schema)
        self.assertEqual(result['unit_origins'],{'total':'main.orders.amount'})
        result=validate('WITH subtotals AS (SELECT region,SUM(amount) AS total FROM orders GROUP BY region) SELECT SUM(total) AS amount FROM subtotals',self.schema)
        self.assertEqual(result['unit_origins'],{'amount':'main.orders.amount'})
        result=validate('WITH changed AS (SELECT amount*2 AS total FROM orders) SELECT SUM(total) AS amount FROM changed',self.schema)
        self.assertEqual(result['unit_origins'],{})

    def test_select_aggregate_and_bound_parameters_execute_on_sqlite(self):
        result = validate("SELECT region, SUM(amount) AS total FROM orders WHERE year=? GROUP BY region ORDER BY total DESC", self.schema)
        self.assertEqual(result["tables"], ["main.orders"])
        self.assertEqual(result["parameter_count"], 1)
        self.assertEqual(result["limit"], 200)
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE orders(id INTEGER,product_id INTEGER,amount REAL,year INTEGER,region TEXT)")
            db.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", [(1, 1, 20, 2024, "华东"), (2, 1, 10, 2024, "华东"), (3, 1, 2, 2023, "华北")])
            self.assertEqual(db.execute(result["sql"], (2024,)).fetchall(), [("华东", 30.0)])

    def test_join_requires_real_relationship(self):
        result = validate("SELECT p.name,SUM(o.amount) total FROM orders o JOIN products p ON o.product_id=p.id GROUP BY p.name", self.schema)
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual(result["relations"][0]["source"], "foreign_key")
        self.assertIn("main.orders.product_id", result["columns"])
        self.rejected("SELECT o.id FROM orders o JOIN products p ON o.id=p.id")
        self.rejected("SELECT o.id FROM orders o JOIN unrelated p ON o.product_id=p.id")

    def test_cte_and_derived_key_lineage(self):
        for prefix in ("WITH a AS (SELECT product_id,SUM(amount) total FROM orders GROUP BY product_id) SELECT p.name,a.total FROM a",
                       "SELECT p.name,a.total FROM (SELECT product_id,SUM(amount) total FROM orders GROUP BY product_id) a"):
            result = validate(prefix + " JOIN products p ON a.product_id=p.id", self.schema)
            self.assertEqual(len(result["relations"]), 1)
        self.rejected("WITH a AS (SELECT id AS product_id FROM orders) SELECT a.product_id FROM a JOIN products p ON a.product_id=p.id")
        self.rejected("WITH a AS (SELECT product_id+0 AS product_id FROM orders) SELECT a.product_id FROM a JOIN products p ON a.product_id=p.id")

    def test_using_join_and_left_join(self):
        schema = copy.deepcopy(self.schema)
        schema["tables"]["main.products"]["columns"]["product_id"] = "INTEGER"
        schema["relations"][0]["right_columns"] = ["product_id"]
        result = validate("SELECT o.id,p.name FROM orders o LEFT JOIN products p USING(product_id)", schema)
        self.assertEqual(result["relations"][0]["join_type"], "LEFT")

    def test_correlated_scope_and_trusted_relationship(self):
        sql = "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM products p WHERE p.id=o.product_id)"
        result = validate(sql, self.schema)
        self.assertEqual(result["relations"][0]["join_type"], "CORRELATED")
        self.rejected("SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM unrelated u WHERE u.id=o.product_id)")
        self.rejected("SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM products p WHERE p.id=o.product_id OR 1=1)")

    def test_composite_relationship_requires_all_columns(self):
        schema = copy.deepcopy(self.schema)
        schema["tables"]["main.products"]["columns"]["year"] = "INTEGER"
        schema["relations"][0].update(left_columns=["product_id", "year"], right_columns=["id", "year"])
        with self.assertRaises(ValueError):
            validate("SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id", schema)
        result = validate("SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id AND o.year=p.year", schema)
        self.assertEqual(result["relations"][0]["left_columns"], ["product_id", "year"])

    def test_unverified_or_model_relationships_are_not_accepted(self):
        for patch in ({"verified": False}, {"source": "model"}, {"source": "same_name"}):
            schema = copy.deepcopy(self.schema)
            schema["relations"][0].update(patch)
            with self.assertRaises(ValueError):
                validate("SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id", schema)
        schema = copy.deepcopy(self.schema)
        schema["relations"][0]["source"] = "verified_candidate"
        self.assertEqual(validate("SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id", schema)["relations"][0]["source"], "verified_candidate")

    def test_fields_aliases_ctes_and_unknown_sources(self):
        for sql in ("SELECT password FROM orders", "SELECT u.id FROM orders o", "SELECT id FROM orders o JOIN products p ON o.product_id=p.id",
                    "WITH a AS (SELECT amount FROM orders) SELECT a.password FROM a", "SELECT amount FROM absent",
                    "SELECT id FROM main.orders o JOIN main.products o ON o.product_id=o.id"):
            self.rejected(sql)

    def test_subquery_keys_and_aggregate_comparison(self):
        result = validate("SELECT o.id FROM orders o WHERE o.product_id IN (SELECT id FROM products)", self.schema)
        self.assertEqual(result["relations"][0]["join_type"], "SUBQUERY_KEY")
        self.rejected("SELECT o.id FROM orders o WHERE o.id IN (SELECT id FROM products)")
        self.rejected("SELECT o.id FROM orders o WHERE o.id=(SELECT id FROM unrelated LIMIT 1)")
        self.assertEqual(validate("SELECT id FROM orders WHERE amount>(SELECT AVG(amount) FROM orders)", self.schema)["limit"], 200)

    def test_malformed_sql_and_duplicate_outputs_fail_closed(self):
        for sql in ("SELECT SELECT id FROM orders", "SELECT amount FROM orders garbage tokens", "SELECT id FROM orders WHERE",
                    "SELECT id,id FROM orders", "SELECT * FROM orders o JOIN products p ON o.product_id=p.id"):
            self.rejected(sql)
        self.assertEqual(validate("SELECT COUNT(*) FROM orders", self.schema)["limit"], 200)

    def test_no_write_ddl_system_or_multiple_statements(self):
        for sql in ("DELETE FROM orders", "INSERT INTO orders(id) VALUES (1)", "UPDATE orders SET amount=1", "CREATE TABLE stolen AS SELECT * FROM orders",
                    "PRAGMA table_info(orders)", "ATTACH DATABASE 'x' AS stolen", "BEGIN", "COMMIT", "VACUUM", "SELECT * INTO stolen FROM orders",
                    "SELECT * FROM orders; SELECT * FROM products", "SELECT * FROM orders; DELETE FROM orders", "SELECT * FROM sqlite_master",
                    "SELECT * FROM information_schema.tables", "SELECT * FROM pg_catalog.pg_class", "SELECT * FROM orders FOR UPDATE"):
            self.rejected(sql)
        self.assertEqual(validate("SELECT amount FROM orders;", self.schema)["tables"], ["main.orders"])

    def test_no_cartesian_or_weakened_join(self):
        for sql in ("SELECT * FROM orders CROSS JOIN products", "SELECT * FROM orders,products", "SELECT * FROM orders NATURAL JOIN products",
                    "SELECT * FROM orders o JOIN products p ON 1=1", "SELECT * FROM orders o JOIN products p ON o.id=o.id",
                    "SELECT * FROM orders o JOIN products p ON o.product_id=p.id OR 1=1",
                    "SELECT * FROM orders o JOIN products p ON NOT(o.product_id<>p.id)"):
            self.rejected(sql)

    def test_functions_and_recursion_are_restricted(self):
        for sql in ("SELECT load_extension('x')", "SELECT readfile('x')", "SELECT randomblob(999999999)", "SELECT writefile('x','x')",
                    "SELECT * FROM read_csv('x')", "SELECT * FROM pragma_table_info('orders')", "SELECT my_private_function(amount) FROM orders",
                    "WITH RECURSIVE x AS (SELECT id FROM orders UNION ALL SELECT id FROM x) SELECT * FROM x"):
            self.rejected(sql)
        safe = validate("SELECT COALESCE(SUM(amount),0) total,MIN(amount) low,MAX(amount) high FROM orders", self.schema)
        self.assertEqual(safe["limit"], 200)
        self.assertTrue(allowed_sqlite_function("SUM"))
        self.assertFalse(allowed_sqlite_function("load_extension"))

    def test_limits_offsets_literals_and_complexity(self):
        self.assertEqual(validate("SELECT id FROM orders LIMIT 1000", self.schema)["limit"], 200)
        self.assertEqual(validate("SELECT id FROM orders LIMIT 3", self.schema)["limit"], 3)
        self.assertEqual(validate("SELECT id FROM orders", self.schema, max_rows=201)["limit"], 201)
        for sql in ("SELECT id FROM orders LIMIT -1", "SELECT id FROM orders LIMIT ?", "SELECT id FROM orders LIMIT 1+1", "SELECT id FROM orders LIMIT 3 OFFSET 10001"):
            self.rejected(sql)
        self.rejected("SELECT '" + "a" * 2001 + "' FROM orders")
        self.rejected("SELECT " + "+".join("amount" for _ in range(100)) + " FROM orders")
        self.rejected("SELECT * FROM orders", max_rows=100000)

    def test_percent_bindings_are_lexical_and_literals_remain_literals(self):
        for dialect, namespace in (("postgres", "public"), ("mysql", "demo")):
            schema = catalog(namespace)
            for placeholder in ("?", "%s"):
                sql = "SELECT '%s ? 50%' note,id FROM " + namespace + ".orders WHERE id>" + placeholder + " AND region LIKE ? -- %s ignored"
                result = validate(sql, schema, dialect=dialect)
                self.assertEqual(result["parameter_count"], 2)
                self.assertIn("%%s ? 50%%", result["driver_sql"])
                self.assertNotIn("ignored", result["sql"])
                self.assertEqual(result["driver_sql"].count("%s") - result["driver_sql"].count("%%s"), 2)
            result = validate("SELECT '50%' note FROM " + namespace + ".orders", schema, dialect=dialect)
            self.assertEqual(result["parameter_count"], 0)
            self.assertIn("50%", result["sql"])
        self.rejected("SELECT id FROM orders WHERE id=:named")

    def test_dialect_schema_and_quoted_names(self):
        for dialect, namespace in (("postgres", "public"), ("mysql", "demo")):
            schema = catalog(namespace)
            result = validate("SELECT p.name,SUM(o.amount) total FROM " + namespace + ".orders o JOIN " + namespace + ".products p ON o.product_id=p.id GROUP BY p.name", schema, dialect=dialect)
            self.assertEqual(result["tables"], [namespace + ".orders", namespace + ".products"])
        schema = {"tables": {"main.销售 明细": {"columns": {"年份": "INTEGER", "金额": "REAL"}}}, "relations": []}
        result = validate('SELECT "年份",SUM("金额") total FROM "销售 明细" GROUP BY "年份"', schema)
        self.assertEqual(result["tables"], ["main.销售 明细"])

    def test_scope_shadowing_and_ancestor_correlation(self):
        bad = [
            "WITH orders AS (SELECT id AS product_id FROM unrelated) SELECT o.product_id FROM orders o JOIN products p ON o.product_id=p.id",
            "WITH a AS (SELECT product_id FROM orders), b AS (SELECT id AS product_id FROM unrelated) SELECT b.product_id FROM b JOIN products p ON b.product_id=p.id",
            "SELECT x.product_id FROM (SELECT id AS product_id FROM unrelated) x JOIN products p ON x.product_id=p.id",
            "SELECT o.id FROM orders o WHERE EXISTS (SELECT o.id FROM unrelated o WHERE o.id=products.id)",
            "SELECT a.product_id FROM (SELECT product_id FROM orders) a JOIN products p ON a.amount=p.id",
        ]
        for sql in bad:
            self.rejected(sql)
        nested = "SELECT p.name FROM products p WHERE EXISTS (SELECT o.id FROM orders o WHERE o.product_id=p.id AND EXISTS (SELECT o2.id FROM orders o2 WHERE o2.product_id=p.id))"
        result = validate(nested, self.schema)
        self.assertEqual(sum(r["join_type"] == "CORRELATED" for r in result["relations"]), 2)

    def test_subquery_and_scalar_key_escape_attempts(self):
        for sql in (
            "SELECT o.id FROM orders o JOIN (SELECT id FROM unrelated) p ON o.product_id=p.id",
            "SELECT o.id FROM orders o JOIN products p ON o.product_id=COALESCE(p.id,o.product_id)",
            "SELECT o.id FROM orders o JOIN products p ON o.product_id=(SELECT p.id)",
            "SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id+0",
            "SELECT o.id FROM orders o WHERE o.amount>(SELECT id FROM products LIMIT 1)",
            "SELECT o.id FROM orders o WHERE o.product_id IN (SELECT id FROM unrelated)",
        ):
            self.rejected(sql)
        self.assertEqual(validate("SELECT id FROM orders WHERE id IN (SELECT id FROM orders)", self.schema)["limit"], 200)

    def test_composite_keys_cannot_be_split_or_bypassed(self):
        schema = copy.deepcopy(self.schema)
        schema["tables"]["main.products"]["columns"]["year"] = "INTEGER"
        schema["relations"][0].update(left_columns=["product_id", "year"], right_columns=["id", "year"])
        for sql in (
            "SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id OR o.year=p.year",
            "SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id AND (o.year=p.year OR 1=1)",
            "SELECT o.id FROM orders o JOIN products p ON o.product_id=p.id AND NOT(o.year<>p.year)",
            "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM products p WHERE p.id=o.product_id AND (p.year=o.year OR 1=1))",
        ):
            with self.assertRaises(ValueError, msg=sql):
                validate(sql, schema)
        sql = "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM products p WHERE p.id=o.product_id AND o.year=p.year)"
        self.assertEqual(validate(sql, schema)["relations"][0]["join_type"], "CORRELATED")

    def test_union_top_and_fetch_cannot_remove_row_cap(self):
        union = validate("SELECT id FROM orders UNION ALL SELECT id FROM orders LIMIT 999", self.schema)
        self.assertEqual(union["limit"], 200)
        self.assertTrue(union["sql"].endswith("LIMIT 200"))
        for sql in ("SELECT TOP 10 id FROM orders", "SELECT id FROM orders LIMIT 1 OFFSET -1", "SELECT id FROM orders UNION ALL SELECT id FROM products LIMIT ?"):
            self.rejected(sql)
        result = validate("SELECT id FROM public.orders FETCH FIRST 300 ROWS ONLY", catalog("public"), dialect="postgres")
        self.assertEqual(result["limit"], 200)
        self.assertTrue(result["sql"].endswith("LIMIT 200"))
        with self.assertRaises(ValueError):
            validate("SELECT id FROM public.orders FETCH FIRST 10 ROWS WITH TIES", catalog("public"), dialect="postgres")

    def test_external_functions_files_and_sql_controls_fail_closed(self):
        for dialect, namespace, bad in (
            ("sqlite", "main", ["SELECT sqlite_version()", "SELECT sqlite_source_id()", "SELECT * FROM pragma_database_list", "SELECT * FROM json_each('[1]')", "SELECT * FROM temp.orders", "EXPLAIN SELECT id FROM orders"]),
            ("postgres", "public", ["SELECT pg_read_file('/etc/passwd')", "SELECT pg_sleep(5)", "SELECT pg_catalog.abs(1)", "SELECT * FROM generate_series(1,999999)", "WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x", "SELECT id FROM orders FOR SHARE", "SELECT id FROM orders WHERE id=$1"]),
            ("mysql", "demo", ["SELECT LOAD_FILE('/etc/passwd')", "SELECT SLEEP(5)", "SELECT BENCHMARK(999999,1)", "SELECT id FROM orders INTO OUTFILE '/tmp/x'", "SELECT @@version", "SELECT * FROM mysql.user", "SELECT id FROM orders WHERE id=%(id)s"]),
        ):
            for sql in bad:
                with self.assertRaises(ValueError, msg=sql):
                    validate(sql, catalog(namespace), dialect=dialect)

    def test_binding_conversion_preserves_literal_percent_and_comment_content(self):
        for dialect, namespace in (("postgres", "public"), ("mysql", "demo")):
            sql = "SELECT 'not a ? or %s; DELETE FROM orders' text,id FROM " + namespace + ".orders WHERE id=%s AND amount % 2 = 0 /* ? %s */"
            result = validate(sql, catalog(namespace), dialect=dialect)
            self.assertEqual(result["parameter_count"], 1)
            self.assertIn("%%s; DELETE FROM orders", result["driver_sql"])
            self.assertIn("%% 2", result["driver_sql"])
            self.assertNotIn("/*", result["sql"])

    def test_subquery_arithmetic_cannot_hide_an_unverified_key_path(self):
        for sql in (
            "SELECT o.id FROM orders o WHERE o.product_id IN (SELECT id+0 FROM unrelated)",
            "SELECT o.id FROM orders o WHERE o.id=(SELECT COALESCE(id,0) FROM unrelated LIMIT 1)",
            "SELECT o.id FROM orders o WHERE o.id=(SELECT id+SUM(0) FROM unrelated GROUP BY id LIMIT 1)",
            "SELECT o.id FROM orders o WHERE o.id+0 IN (SELECT id FROM unrelated)",
            "SELECT o.id FROM orders o WHERE o.id IN (SELECT MAX(id) FROM unrelated)",
        ):
            self.rejected(sql)
        self.assertEqual(validate("SELECT id FROM orders WHERE amount>(SELECT COALESCE(AVG(amount),0) FROM orders)", self.schema)["limit"], 200)

    def test_custom_operators_and_type_conversions_are_execution_entries(self):
        schema = catalog("public")
        for sql in (
            "SELECT amount OPERATOR(public.+) 2 FROM public.orders",
            "SELECT amount OPERATOR(pg_catalog.+) 2 FROM public.orders",
            "SELECT CAST(amount AS attacker_type) FROM public.orders",
            "SELECT amount::public.attacker_type FROM public.orders",
            "SELECT CAST(amount AS public.attacker_type[]) FROM public.orders",
        ):
            with self.assertRaises(ValueError, msg=sql):
                validate(sql, schema, dialect="postgres")
        result = validate("SELECT CAST(amount AS NUMERIC(10,2)) total FROM public.orders", schema, dialect="postgres")
        self.assertEqual(result["limit"], 200)


if __name__ == "__main__":
    unittest.main()
