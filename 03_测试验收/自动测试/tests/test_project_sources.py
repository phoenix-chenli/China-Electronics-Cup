"""Project isolation, frozen sources, and public-data import integrity."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-project-sources-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import main, conversations, query_routing, public_statistics, connections, store as s, agent, documents


class ProjectSourceTests(unittest.TestCase):
    def test_managed_public_vocabulary_contains_definitions_not_observations(self):
        installed=public_statistics.install()
        self.assertTrue(installed['installed'])
        bound=query_routing.profile('public-statistics')
        context=agent.query_context(question='2024年各地GDP是多少',project_id='public-statistics',scope_override='database')
        vocabulary=context['source_vocabulary']
        self.assertEqual(len(vocabulary['indicators']),39)
        self.assertEqual({r['region_name'] for r in vocabulary['regions']},{'淮南市','新津区','石景山区'})
        self.assertEqual({r['region_id'] for r in vocabulary['regions']},{'huainan','xinjin','shijingshan'})
        self.assertNotIn('value_decimal',json.dumps(vocabulary))
        old={'metric':'database','query_scope':'database','database_connection_id':self.cid}
        isolated=query_routing.resolve_context(question='沿用原数据库',project_id='public-statistics',previous=old)
        self.assertIsNone(isolated.get('source_vocabulary'))
        self.assertNotEqual(bound['database_connection_id'],self.cid)

    @classmethod
    def setUpClass(cls):
        main.init_app()
        conversations.initialize()
        cls.curated = json.loads((public_statistics.SOURCE_DIR / '指标核验.json').read_text())
        cls.catalog = json.loads((public_statistics.SOURCE_DIR / '来源清单.json').read_text())
        cls.path = s.DATA / 'public-test.db'
        public_statistics.build_database(cls.path, cls.curated, cls.catalog)
        cfg=connections.create(connections.Config(name='公开来源测试',kind='sqlite',path=str(cls.path)))
        assert connections.test(cfg['id'])['last_test']['ok']
        cls.cid=connections.enabled(cfg['id'],connections.Enabled(enabled=True))['id']
        cls.did=s.doc_list()[0]['id']
        with s.conn() as db: db.execute('INSERT INTO ef_projects VALUES (?,?,?,?,?)',('public-test','公开统计测试','sales-demo',s.now(),s.now()))
        query_routing.bind('public-test',{'default_scope':'auto','managed_public':True,'document_ids':[cls.did],
                                       'database_connection_id':cls.cid,'source_label':'核验后的三地公报'})

    @classmethod
    def tearDownClass(cls):
        conversations.manager.pool.shutdown(wait=True)
        TEMP.cleanup()

    def test_native_official_word_preserves_table_roles_and_draft_provenance(self):
        before=s.digest(s.DATA/'public-statistics.db') if (s.DATA/'public-statistics.db').exists() else None
        public_statistics.install()
        bound=query_routing.profile('public-statistics')
        native=[x for x in s.doc_list() if x.get('provenance',{}).get('origin')=='original_docx_from_official_site']
        self.assertEqual(len(native),2)
        self.assertTrue(all(x['id'] in bound['primary_document_ids'] for x in native))
        doc=next(x for x in native if '征求意见稿_' in x['name'])
        view=documents.layout(doc['id'])
        tables=[t for page in view['pages'] for t in page.get('tables',[])]
        self.assertEqual(len(tables),1)
        self.assertEqual(tables[0]['rows'],22)
        cells=tables[0]['cells']
        rows={r:[c['text'] for c in cells if c['row']==r] for r in range(22)}
        self.assertIn('区卫生健康委',rows[13])
        self.assertIn('区民政局',rows[15])
        text=next(page for page in view['pages'] if page.get('tables'))['text']
        page=next(page for page in view['pages'] if page.get('tables'))['number']
        quote='区卫生健康委'
        start=text.index(quote)
        located=documents.resolve({'document_id':doc['id'],'sha256':doc['sha256'],
            'parse_id':view['parse_id'],'page':page,'text_range':{'start':start,'end':start+len(quote)}},quote)
        self.assertEqual(located['status'],'located')
        self.assertEqual(located['publication']['publication_status'],'public_consultation_draft')
        self.assertFalse(located['quality']['human_reviewed'])
        if before:self.assertEqual(s.digest(s.DATA/'public-statistics.db'),before)

    def test_supplemental_source_hash_failure_stops_before_import(self):
        import copy
        original=copy.deepcopy(self.catalog)
        original['supplemental_documents'][0]['file_sha256']='0'*64
        actual_loads=json.loads
        def replaced(data,*args,**kwargs):
            result=actual_loads(data,*args,**kwargs)
            return original if isinstance(result,dict) and 'supplemental_documents' in result else result
        with patch('backend.public_statistics.json.loads',side_effect=replaced):
            with self.assertRaisesRegex(ValueError,'摘要不一致'):
                public_statistics.install()

    def test_public_database_integrity_and_decimal_reference(self):
        before=s.digest(self.path)
        d=connections.ready(self.cid,connections.DB_KINDS)
        with connections.database(d) as db:
            values=connections.rows(db,"SELECT p.value/g.value*100 AS pct FROM public_observations p JOIN public_observations g ON p.region_id=g.region_id AND p.year=g.year WHERE p.region_id='xinjin' AND p.year=2024 AND p.metric_id='private_value' AND g.metric_id='gdp'")['rows']
            self.assertAlmostEqual(values[0]['pct'],354.57/534.94*100,places=9)
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM public_observations')
        self.assertEqual(before,s.digest(self.path))

    def test_same_metric_not_mixed_across_population_types(self):
        with connections.database(connections.ready(self.cid,connections.DB_KINDS)) as db:
            rows=connections.rows(db,"SELECT region_id,population_type FROM public_observations WHERE year=2024 AND metric_id='population_urban' ORDER BY region_id")['rows']
        self.assertEqual(rows,[{'region_id':'huainan','population_type':'常住人口'},{'region_id':'xinjin','population_type':'户籍人口'}])

    def test_unpublished_first_industry_not_filled_with_zero(self):
        with connections.database(connections.ready(self.cid,connections.DB_KINDS)) as db:
            rows=connections.rows(db,"SELECT * FROM public_observations WHERE region_id='shijingshan' AND metric_id='sector1_value'")['rows']
        self.assertEqual(rows,[])

    def test_public_rows_and_docs_do_not_expose_sales_project(self):
        tables=main.tables('public-test')
        self.assertEqual({x['name'] for x in tables},{'public_regions','public_indicators','public_sources','public_observations','public_source_anchors'})
        self.assertEqual([x['id'] for x in main.documents(project_id='public-test')],[self.did])
        with self.assertRaises(Exception): main.table_rows('orders',project_id='public-test')
        rows=main.table_rows('public_observations',limit=5,project_id='public-test')
        self.assertEqual(len(rows['rows']),5)
        self.assertEqual(rows['total'],73)

    def test_route_public_numeric_hybrid_and_policy_document(self):
        self.assertEqual(query_routing.preview('2024年新津GDP是多少？','public-test')['query_scope'],'hybrid')
        self.assertEqual(query_routing.preview('统计公报数据采用什么价格口径？','public-test')['query_scope'],'knowledge')
        self.assertEqual(query_routing.preview('2024年GDP是多少？','public-test','database')['query_scope'],'database')

    def test_readonly_text_filters_work_with_sqlite_authorizer(self):
        from backend import database_query
        result=database_query.execute("SELECT region_name FROM public_regions WHERE region_name LIKE ?",params=['%新津%'],connection_id=self.cid)
        self.assertEqual([{'region_name':'新津区'}],result['rows'])
        result=database_query.execute("SELECT region_name FROM public_regions WHERE region_name GLOB ?",params=['*新津*'],connection_id=self.cid)
        self.assertEqual([{'region_name':'新津区'}],result['rows'])

    def test_sales_scope_rejected_in_public_project(self):
        with self.assertRaisesRegex(ValueError,'没有销售示例'): query_routing.preview('2024年销售额多少','public-test','sales')

    def test_profile_source_and_options_are_project_specific(self):
        context=conversations.query_profile('public-test')
        self.assertEqual(context['database_connection_id'],self.cid)
        self.assertEqual(context['document_count'],1)
        self.assertEqual(context['default_scope'],'auto')
        self.assertNotIn('sales',{x['id'] for x in context['scope_options']})

    def test_queued_source_freeze_uses_public_binding(self):
        context=agent.query_context(question='2024年新津GDP是多少？',project_id='public-test')
        self.assertEqual(context['query_scope'],'hybrid')
        self.assertEqual(context['database_connection_id'],self.cid)
        self.assertEqual([x['document_id'] for x in context['rag_scope']['documents']],[self.did])

    def test_followup_keeps_old_source_even_in_another_preview_project(self):
        previous={'metric':'database','query_scope':'database','database_connection_id':None}
        resolved=query_routing.resolve_context('再统计一下', 'public-test', previous)
        self.assertEqual(resolved['query_scope'],'database')
        self.assertIsNone(resolved['database_connection_id'])
        self.assertIsNone(resolved['requested_rag_scope'])

    def test_legacy_sales_followup_retains_scope_and_public_project_rejects_it(self):
        previous={'metric':'net_sales','region':'华东'}
        resolved=query_routing.resolve_context('那华北呢','default',previous)
        self.assertEqual('sales',resolved['query_scope'])
        with self.assertRaisesRegex(ValueError,'没有销售示例'):
            query_routing.resolve_context('那华北呢','public-test',previous)

    def test_document_page_cannot_be_broadened(self):
        doc=s.doc_get(self.did)
        page={'document_id':self.did,'sha256':doc['sha256'],'page':1}
        resolved=query_routing.resolve_context('计算比例','public-test',document_context=page)
        self.assertEqual(resolved['query_scope'],'knowledge')
        self.assertIsNone(resolved['database_connection_id'])
        with self.assertRaisesRegex(ValueError,'冲突'):
            query_routing.resolve_context('查数量','public-test',requested_scope={'scope_type':'document','document_ids':[self.did]},scope_override='database')

    def test_official_provenance_only_bound_to_matching_version(self):
        doc=s.doc_get(self.did)
        quality=json.loads(doc['quality_json'])
        quality['source_provenance']={'file_sha256':doc['sha256'],'official_url':'https://example.gov/source','origin':'original_pdf_from_user_bundle'}
        with s.conn() as db: db.execute('UPDATE documents SET quality_json=? WHERE id=?',(json.dumps(quality),self.did))
        self.assertEqual(documents.view(self.did)['document']['official_url'],'https://example.gov/source')
        quality['source_provenance']['file_sha256']='0'*64
        with s.conn() as db: db.execute('UPDATE documents SET quality_json=? WHERE id=?',(json.dumps(quality),self.did))
        self.assertNotIn('official_url',documents.view(self.did)['document'])


if __name__ == '__main__': unittest.main()
