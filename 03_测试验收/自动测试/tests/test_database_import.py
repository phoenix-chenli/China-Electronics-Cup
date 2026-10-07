"""Disposable SQLite/import/API contracts; no real remote database claims."""
import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

from tests.test_conversations import LocalClient
from backend import agent, connections, conversations, database_query, engine, main, store as s


class DatabaseImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=LocalClient()
        cls.source=s.DATA/'import-source.sqlite'
        with sqlite3.connect(cls.source,factory=s.ClosingConnection) as db:
            db.executescript('''
                CREATE TABLE accounts(id INTEGER PRIMARY KEY,account_no TEXT NOT NULL UNIQUE);
                CREATE TABLE tickets(id INTEGER PRIMARY KEY,account_id INTEGER REFERENCES accounts(id),amount REAL);
                INSERT INTO accounts VALUES(1,'A'),(2,'B');
                INSERT INTO tickets VALUES(10,1,12.5),(11,2,9.5);
                CREATE TABLE routes(country TEXT,code TEXT,PRIMARY KEY(country,code));
                CREATE TABLE legs(id INTEGER PRIMARY KEY,country TEXT,code TEXT,
                  FOREIGN KEY(country,code) REFERENCES routes(country,code));
                INSERT INTO routes VALUES('CN','SH');
                INSERT INTO legs VALUES(1,'CN','SH');
                CREATE TABLE spaced(" id " TEXT PRIMARY KEY);
                CREATE TABLE spaced_child(" parent id " TEXT REFERENCES spaced(" id "));
                INSERT INTO spaced VALUES('P');
                INSERT INTO spaced_child VALUES('P');
            ''')
        response=cls.client.post('/api/connections',json={'name':'导入契约夹具','kind':'sqlite','path':str(cls.source)})
        cls.connection_id=response.json()['id']
        cls.client.post('/api/connections/'+cls.connection_id+'/test')
        cls.client.patch('/api/connections/'+cls.connection_id+'/enabled',json={'enabled':True})

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        with s.conn() as db:db.execute("DELETE FROM ef_settings WHERE key='agent'")

    def tearDown(self):
        with s.conn() as db:db.execute("DELETE FROM ef_settings WHERE key='agent'")

    def metadata(self,names):
        with s.conn() as db:
            bodies=[json.loads(db.execute('SELECT body FROM ef_database_import_metadata WHERE name=?',(name,)).fetchone()[0]) for name in names]
        return {body['original_table']['name']:(name,body) for name,body in zip(names,bodies)}

    def upload(self):
        boundary='database-import-'+uuid.uuid4().hex
        data=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="unseen.sqlite"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+
              self.source.read_bytes()+f'\r\n--{boundary}--\r\n'.encode())
        request=urllib.request.Request(self.client.url+'/api/import',data=data,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        try:response=urllib.request.urlopen(request,timeout=15)
        except urllib.error.HTTPError as exc:response=exc
        body=response.read().decode()
        self.assertEqual(response.code,200,body)
        return json.loads(body)

    def import_tables(self,*names):
        response=self.client.post('/api/connections/'+self.connection_id+'/import',json={'tables':[{'schema_name':'main','name':n} for n in names]})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_uploaded_sqlite_preserves_unique_and_composite_foreign_keys(self):
        before=s.digest(self.source)
        result=self.upload();metadata=self.metadata(result['tables'])
        account_name,account=metadata['accounts'];ticket_name,ticket=metadata['tickets']
        self.assertEqual(account['primary_key'],['id'])
        self.assertIn(['account_no'],[u['columns'] for u in account['unique_keys']])
        self.assertEqual(ticket['foreign_keys'][0]['referenced_table'],account_name)
        route_name,route=metadata['routes'];leg_name,leg=metadata['legs']
        self.assertEqual(route['primary_key'],['country','code'])
        self.assertEqual(leg['foreign_keys'][0]['referenced_columns'],['country','code'])
        inspected=database_query.inspect(table_names=[route_name,leg_name])
        self.assertTrue(any(r['verified'] and r['left_columns']==['country','code'] for r in inspected['relation_candidates']))
        self.assertEqual(s.digest(self.source),before)

    def test_uploaded_trimmed_columns_remain_bound_to_keys(self):
        metadata=self.metadata(self.upload()['tables'])
        parent_name,parent=metadata['spaced'];child_name,child=metadata['spaced_child']
        self.assertEqual(parent['primary_key'],['id'])
        self.assertEqual(child['foreign_keys'][0]['columns'],['parent id'])
        self.assertEqual(child['foreign_keys'][0]['referenced_columns'],['id'])
        inspected=database_query.inspect(table_names=[parent_name,child_name])
        self.assertTrue(any(r['verified'] for r in inspected['relation_candidates']))

    def test_connection_batch_maps_only_imported_parents(self):
        before=s.digest(self.source)
        result=self.import_tables('accounts','tickets')
        metadata=self.metadata(result['tables'])
        parent_name,parent=metadata['accounts'];child_name,child=metadata['tickets']
        self.assertEqual(child['foreign_keys'][0]['referenced_table'],parent_name)
        self.assertEqual(child['origin']['connection_id'],self.connection_id)
        inspected=database_query.inspect(table_names=[parent_name,child_name])
        sql=f'SELECT a.account_no,SUM(t.amount) AS total FROM "{parent_name}" a JOIN "{child_name}" t ON t.account_id=a.id GROUP BY a.account_no ORDER BY a.account_no'
        queried=database_query.execute(sql,snapshot=inspected)
        self.assertEqual(queried['rows'],[{'account_no':'A','total':12.5},{'account_no':'B','total':9.5}])
        self.assertEqual(s.digest(self.source),before)

    def test_unimported_parent_is_unresolved_and_never_joins_other_snapshot(self):
        metadata=self.metadata(self.import_tables('tickets')['tables'])
        child_name,child=metadata['tickets']
        self.assertEqual(child['foreign_keys'],[])
        self.assertEqual(child['unresolved_foreign_keys'][0]['referenced_table'],'accounts')
        self.assertEqual(database_query.inspect(table_names=[child_name])['relation_candidates'],[])

    def test_metadata_failure_rolls_back_business_table_and_catalog(self):
        with s.conn() as db:
            before=db.execute('SELECT COUNT(*) FROM table_catalog').fetchone()[0]
            db.execute("CREATE TRIGGER reject_metadata BEFORE INSERT ON ef_database_import_metadata BEGIN SELECT RAISE(ABORT,'fixture-metadata-write-failure'); END")
        source_before=s.digest(self.source)
        with sqlite3.connect(s.BUSINESS_DB,factory=s.ClosingConnection) as db:
            tables_before={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        try:
            response=self.client.post('/api/connections/'+self.connection_id+'/import',json={'tables':[{'name':'accounts'}]})
            self.assertNotEqual(response.status_code,200)
            with s.conn() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM table_catalog').fetchone()[0],before)
            with sqlite3.connect(s.BUSINESS_DB,factory=s.ClosingConnection) as db:
                self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},tables_before)
            self.assertEqual(s.digest(self.source),source_before)
        finally:
            with s.conn() as db:db.execute('DROP TRIGGER reject_metadata')

    def test_single_query_passes_server_frozen_source(self):
        with s.conn() as db:db.execute("INSERT OR REPLACE INTO ef_settings VALUES('agent',?)",(json.dumps({'mode':'model','query_scope':'database','database_connection_id':self.connection_id}),))
        with patch.object(agent,'execute',return_value={'fixture':True}) as execute:
            self.assertEqual(main.run(main.QueryRequest(question='按账号汇总票据')),{'fixture':True})
        self.assertEqual(execute.call_args.kwargs['database_context']['database_connection_id'],self.connection_id)
        self.assertEqual(execute.call_args.kwargs['database_context']['database_connection_revision'],connections.ready(self.connection_id)['revision'])

    def test_queue_keeps_source_after_config_changes_and_ignores_client_context(self):
        entered,release=threading.Event(),threading.Event();seen=[]
        def executor(question,previous,confirmed,**kwargs):
            seen.append(kwargs.pop('database_context'))
            if len(seen)==1:entered.set();release.wait(4)
            return engine.execute('2024 年净销售额',persist=False)
        manager=conversations.TaskManager(workers=1,executor=executor)
        with s.conn() as db:db.execute("INSERT OR REPLACE INTO ef_settings VALUES('agent',?)",(json.dumps({'mode':'model','query_scope':'database','database_connection_id':self.connection_id}),))
        try:
            first=manager.submit({'question':'一','database_context':{'query_scope':'database','database_connection_id':'client-forged'}})
            self.assertTrue(entered.wait(2))
            second=manager.submit({'question':'二'})
            with s.conn() as db:db.execute("INSERT OR REPLACE INTO ef_settings VALUES('agent',?)",(json.dumps({'mode':'local','query_scope':'sales'}),))
            release.set()
            deadline=time.monotonic()+6
            while manager.get(second['id'])['status'] in conversations.ACTIVE and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(manager.get(first['id'])['status'],'completed')
            self.assertEqual(manager.get(second['id'])['status'],'completed')
            self.assertEqual([v['database_connection_id'] for v in seen],[self.connection_id,self.connection_id])
        finally:release.set();manager.pool.shutdown(wait=True)

    def test_database_recompute_rejected_for_sync_and_session_paths(self):
        spec={'metric':'database','query_scope':'database','database_connection_id':self.connection_id}
        result={'run_id':str(uuid.uuid4()),'question':'按账号汇总票据','query_spec':spec,'status':'completed','data_version':'schema-only'}
        s.snapshot_run(result)
        response=self.client.post('/api/query/recompute',json={'run_id':result['run_id'],'overrides':{'year':2024}})
        self.assertEqual(response.status_code,400);self.assertIn('自然语言追问',response.text)
        sid=self.client.post('/api/projects/default/sessions').json()['id'];mid=str(uuid.uuid4())
        with s.conn() as db:db.execute("INSERT INTO ef_messages(id,session_id,role,text,status,result_json,created_at) VALUES(?,?,'assistant','','completed',?,?)",(mid,sid,json.dumps(result),s.now()))
        response=self.client.post('/api/tasks',json={'session_id':sid,'question':'修改','mode':'recompute','parent_message_id':mid,'confirmed':{'year':2024}})
        self.assertEqual(response.status_code,400);self.assertIn('自然语言追问',response.text)

    def test_generic_saved_snapshots_compare_their_own_schema_or_connection(self):
        snapshot=database_query.inspect(table_names=['orders'])
        result={'query_spec':{'metric':'database','query_scope':'database'},'data_version':snapshot['source']['fingerprint'],
                'database_query':{'source':snapshot['source'],'inspected_tables':['main.orders']}}
        self.assertFalse(conversations.analysis_snapshot_state(result)['stale'])
        with patch.object(s,'data_version',return_value='different-sales-version'):
            state=conversations.analysis_snapshot_state(result)
        self.assertFalse(state['stale']);self.assertEqual(state['stale_scope'],'schema_snapshot');self.assertIn('数据行可能变化',state['stale_boundary'])
        current=connections.ready(self.connection_id)['revision']
        external={'query_spec':{'metric':'database'},'data_version':'unrelated-sales-version',
                  'database_query':{'source':{'connection_id':self.connection_id,'revision':current}}}
        self.assertFalse(conversations.analysis_snapshot_state(external)['stale'])
        external['database_query']['source']['revision']=current-1
        self.assertTrue(conversations.analysis_snapshot_state(external)['stale'])
        missing={'query_spec':{'metric':'database'},'data_version':'old-schema'}
        self.assertIsNone(conversations.analysis_snapshot_state(missing)['stale'])

    def test_health_reports_version_and_query_scope(self):
        health=self.client.get('/api/health').json()
        self.assertEqual(health['version'],'0.6.0');self.assertEqual(health['query_scope'],'sales')
