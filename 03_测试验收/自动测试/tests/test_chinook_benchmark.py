"""Chinook benchmark: shipped bytes, read-only binding, and the frozen objective cases."""
import os
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-chinook-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import agent, chinook_benchmark as chinook, connections, conversations, database_query, query_routing, settings, store as s


class ChinookBenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
        conversations.initialize()
        settings.initialize()
        connections.initialize()
        database_query.initialize()
        cls.result=chinook.install()
        cls.cases=chinook.cases()
        cls.connection_id=cls.result.get('connection_id')
        cls.before=s.digest(chinook.DB_PATH)

    @classmethod
    def tearDownClass(cls):
        conversations.manager.pool.shutdown(wait=True)
        TEMP.cleanup()

    def test_the_shipped_database_matches_its_recorded_digest_and_license(self):
        body,error=chinook.verify()
        self.assertEqual('',error)
        self.assertEqual('MIT',body['license'])
        self.assertEqual(body['file_sha256'],s.digest(chinook.DB_PATH))
        self.assertEqual(11,body['tables'])
        self.assertEqual('lerocha/chinook-database',body['upstream_project'])
        self.assertTrue((chinook.SOURCE_DIR/'LICENSE.txt').is_file())

    def test_install_binds_a_readonly_project_and_leaves_the_file_untouched(self):
        self.assertTrue(self.result['installed'],self.result)
        profile=query_routing.profile('chinook-benchmark')
        self.assertEqual('database',profile['default_scope'])
        self.assertEqual(self.connection_id,profile['database_connection_id'])
        self.assertTrue(profile['managed_public'])
        self.assertEqual([],profile['document_ids'])
        self.assertEqual(self.before,s.digest(chinook.DB_PATH))
        with self.assertRaisesRegex(ValueError,'没有销售示例'):
            query_routing.resolve_context('2024 年销售额多少','chinook-benchmark',scope_override='sales')

    def test_every_frozen_case_executes_to_its_independent_reference_rows(self):
        self.assertEqual(20,len(self.cases))
        for case in self.cases:
            value=database_query.execute(case['reference_sql'],connection_id=self.connection_id)
            self.assertEqual(case['columns'],value['columns'],case['id'])
            self.assertEqual(len(case['expected_rows']),len(value['rows']),case['id'])
            for expected,actual in zip(case['expected_rows'],value['rows']):
                for column,want in zip(case['columns'],expected):
                    got=actual[column]
                    if isinstance(want,(int,float)) and not isinstance(want,bool):
                        self.assertAlmostEqual(float(want),float(got),places=6,msg=f"{case['id']}.{column}")
                    else:
                        self.assertEqual(want,got,f"{case['id']}.{column}")
        self.assertEqual(self.before,s.digest(chinook.DB_PATH))

    def test_the_read_only_guard_still_rejects_writes_and_invented_joins(self):
        for statement in ('DELETE FROM Invoice',
                          'UPDATE Track SET UnitPrice = 0',
                          'SELECT COUNT(*) AS n FROM Artist JOIN Album ON 1 = 1'):
            with self.assertRaises(ValueError,msg=statement):
                database_query.execute(statement,connection_id=self.connection_id)
        self.assertEqual(self.before,s.digest(chinook.DB_PATH))

    def test_declared_foreign_keys_become_trusted_relations(self):
        snapshot=database_query.inspect(self.connection_id)
        declared={(r['left_table'].split('.')[-1],r['right_table'].split('.')[-1]) for r in snapshot['relations']}
        for pair in (('Album','Artist'),('Track','Album'),('Track','Genre'),('InvoiceLine','Invoice'),('Employee','Employee')):
            self.assertIn(pair,declared)
        self.assertTrue(all(r['source']=='foreign_key' and r['verified'] is True for r in snapshot['relations']))

    def test_a_capitalised_table_name_with_a_table_level_read_is_not_denied(self):
        # Regression: SQLite reports a table-level read (COUNT(*)) with the name
        # as written in the statement but a column read with the catalog name.
        # The guard normalises identifiers, so a case-sensitive whitelist refused
        # every COUNT(*) against a table whose name has capitals.
        value=database_query.execute('SELECT COUNT(*) AS tracks FROM Track',connection_id=self.connection_id)
        self.assertEqual([{'tracks':3503}],value['rows'])
        mixed=database_query.execute('SELECT COUNT(*) AS albums FROM Album',connection_id=self.connection_id)
        self.assertEqual([{'albums':347}],mixed['rows'])

    def test_a_chinese_alias_carries_through_to_a_readable_statement(self):
        # The planner is asked to alias in Chinese. The server never translates an
        # identifier itself: it cannot know what a model-invented name means, and
        # guessing one would be fabricated meaning rather than reported fact.
        value=database_query.execute('SELECT COUNT(*) AS 艺术家数量 FROM Artist',connection_id=self.connection_id)
        self.assertEqual(['艺术家数量'],value['columns'])
        self.assertEqual('艺术家数量 为 275。',
                         agent.row_fact_text(0,value['rows'][0],value['columns'],single=True))
        grouped=database_query.execute('SELECT g.Name AS 流派, COUNT(*) AS 曲目数 FROM Track t JOIN Genre g ON g.GenreId = t.GenreId GROUP BY g.GenreId, g.Name ORDER BY 曲目数 DESC LIMIT 2',connection_id=self.connection_id)
        self.assertEqual(['流派','曲目数'],grouped['columns'])
        self.assertEqual('返回结果第 1 行：流派 = "Rock"；曲目数 = 1297。',
                         agent.row_fact_text(0,grouped['rows'][0],grouped['columns']))


if __name__=='__main__':
    unittest.main()
