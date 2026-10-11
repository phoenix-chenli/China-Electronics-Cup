"""Stage 9: real local HTTP and SQLite. Provider fixtures prove contracts only."""
import base64, hashlib, io, json, os, re, sqlite3, stat, threading, time, unittest, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from tests.test_conversations import LocalClient
from backend import connections as c, extensions, sharing, store as s, engine

class Provider(BaseHTTPRequestHandler):
 calls=[];mode='ok'
 def log_message(self,*args):pass
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));self.calls.append((self.path,dict(self.headers),data))
  if self.mode=='auth':self.send_response(401);self.end_headers();self.wfile.write(b'{"secret":"provider-should-not-leak"}');return
  if self.mode=='unavailable':self.send_response(503);self.end_headers();self.wfile.write(b'{"secret":"provider-should-not-leak"}');return
  if self.mode=='disconnect':self.close_connection=True;return
  if self.mode=='redirect':self.send_response(307);self.send_header('Location','https://example.com');self.end_headers();return
  msg=data['messages'][-1]['content']
  if isinstance(msg,list):
   from PIL import Image
   im=Image.open(io.BytesIO(base64.b64decode(msg[1]['image_url']['url'].split(',')[1])))
   colors={(227,32,32):'RED',(23,76,237):'BLUE',(21,156,56):'GREEN'}
   text=','.join(colors.get(im.getpixel((60+i*120,80)),'UNKNOWN') for i in range(3))
  else:
   match=re.search(r'计算 (\d+)\+(\d+)',msg)
   text=str(int(match[1])+int(match[2])) if match else '这是一条协议测试回复，不是业务核验。'
  if self.mode=='wrong':text='WRONG'
  if self.mode=='empty':text=''
  result={'model':'fixture-only','choices':[{'message':{'content':text}}],'usage':{'total_tokens':10}}
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())

class ConnectionsTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.client=LocalClient();cls.provider=ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=cls.provider.serve_forever,daemon=True).start()
  cls.endpoint='http://127.0.0.1:'+str(cls.provider.server_port)+'/v1'
  cls.source=s.DATA/'readonly-source.sqlite'
  with sqlite3.connect(cls.source,factory=s.ClosingConnection) as db:
   db.execute('CREATE TABLE "销售 明细" (id INTEGER, amount REAL, note TEXT)');db.executemany('INSERT INTO "销售 明细" VALUES (?,?,?)',[(1,12.5,'中文'),(2,24.5,'<script>alert(1)</script>')])
   db.execute('CREATE TABLE "empty" (id INTEGER)')
 @classmethod
 def tearDownClass(cls):cls.provider.shutdown();cls.provider.server_close();cls.client.close()
 def setUp(self):Provider.mode='ok';Provider.calls.clear();self.client.patch('/api/settings/privacy',json={'local_only':True})
 def create(self,kind='text',**kw):
  defaults={'name':'连接测试','kind':kind,'endpoint':self.endpoint,'model':'contract-fixture','path':str(self.source)}
  r=self.client.post('/api/connections',json={**defaults,**kw});self.assertEqual(r.status_code,200,r.text);return r.json()
 def ready_connection(self,kind='text',**kw):
  d=self.create(kind,**kw);r=self.client.post('/api/connections/'+d['id']+'/test').json();self.assertTrue(r['last_test']['ok'],r)
  return self.client.patch('/api/connections/'+d['id']+'/enabled',json={'enabled':True}).json()
 def prepare(self,d,**kw):
  r=self.client.post('/api/extensions/prepare',json={'connection_id':d['id'],'question':'补充说明','run_id':None,**kw});self.assertEqual(r.status_code,200,r.text);return r.json()
 def execute(self,d):return self.client.post('/api/extensions/'+d['id']+'/execute',json={'accepted':True})
 def test_secret_encrypted_redacted_and_permissions(self):
  secret='test-only-key-'+os.urandom(12).hex();d=self.create(secret=secret)
  self.assertTrue(d['has_secret']);self.assertNotIn(secret,json.dumps(self.client.get('/api/connections').json()))
  with s.conn() as db:
   row=db.execute('SELECT body,secret FROM ef_connections WHERE id=?',(d['id'],)).fetchone();self.assertNotIn(secret,row[0]);self.assertNotIn(secret,row[1])
  self.assertEqual(c.get(d['id'],True)['_secret'],secret)
  if os.name=='nt':
   from backend.vault import PREFIX
   self.assertTrue((s.DATA/'.private/connections.key').read_bytes().startswith(PREFIX))
  else:
   self.assertEqual(stat.S_IMODE((s.DATA/'.private/connections.key').stat().st_mode),0o600)
   self.assertEqual(stat.S_IMODE((s.DATA/'.private').stat().st_mode),0o700)
 def test_update_revision_retains_and_clears_secret(self):
  d=self.create(secret='test-key');body={'name':d['name'],'kind':'text','endpoint':self.endpoint,'model':'contract-fixture','expected_revision':d['revision']}
  r=self.client.request('PUT','/api/connections/'+d['id'],json=body);self.assertEqual(r.status_code,200);self.assertTrue(r.json()['has_secret'])
  self.assertEqual(self.client.request('PUT','/api/connections/'+d['id'],json=body).status_code,409)
  body.update(expected_revision=r.json()['revision'],secret='');r=self.client.request('PUT','/api/connections/'+d['id'],json=body);self.assertFalse(r.json()['has_secret'])
 def test_credentials_not_reused_for_changed_destination(self):
  d=self.create(secret='fixture-only');r=self.client.request('PUT','/api/connections/'+d['id'],json={'name':d['name'],'kind':'text','model':'x','endpoint':'https://example.com/v1','expected_revision':d['revision']});self.assertEqual(r.status_code,400);self.assertIn('重新填写',r.text)
 def test_enable_requires_real_test(self):
  d=self.create();r=self.client.patch('/api/connections/'+d['id']+'/enabled',json={'enabled':True});self.assertEqual(r.status_code,400)
 def test_text_probe_and_token_limit(self):
  d=self.ready_connection();self.assertEqual(len(Provider.calls),1);self.assertEqual(Provider.calls[0][2]['max_completion_tokens'],1200);self.assertTrue(c.capabilities()['text'])
 def test_slow_model_timeout_can_be_configured_without_unbounded_wait(self):
  self.assertEqual(self.create(timeout=120)['timeout'],120)
  self.assertEqual(self.create(timeout=300)['timeout'],300)
  r=self.client.post('/api/connections',json={'name':'超时上限','kind':'text','endpoint':self.endpoint,'model':'fixture','timeout':301})
  self.assertEqual(r.status_code,422)
 def test_tokendance_planner_uses_json_without_hidden_thinking(self):
  d={**c.get(self.create()['id'],True),'endpoint':'https://tokendance.space/gateway/v1','model':'qwen3.8-max'}
  response={'choices':[{'message':{'content':'{"action":"answer","claims":[]}'}}]}
  with patch.object(c,'request_json',return_value=response) as call:
   c.chat(d,[{'role':'user','content':'只返回 JSON'}],json_mode=True)
   body=call.call_args.args[2]
   self.assertEqual(body['response_format'],{'type':'json_object'});self.assertEqual(body['temperature'],0);self.assertFalse(body['enable_thinking'])
   c.chat(d,[{'role':'user','content':'普通文字补充'}])
   self.assertNotIn('response_format',call.call_args.args[2]);self.assertNotIn('enable_thinking',call.call_args.args[2])
 def test_other_compatible_providers_keep_the_existing_contract(self):
  d=c.get(self.create()['id'],True)
  with patch.object(c,'request_json',return_value={'choices':[{'message':{'content':'{}'}}]}) as call:
   c.chat(d,[{'role':'user','content':'JSON'}],json_mode=True)
   self.assertNotIn('response_format',call.call_args.args[2]);self.assertNotIn('enable_thinking',call.call_args.args[2])
 def test_tokendance_deepseek_keeps_visible_answer_budget_for_probe_and_json(self):
  d={**c.get(self.create()['id'],True),'endpoint':'https://tokendance.space/gateway/v1','model':'deepseek-v4.1-flash'}
  response={'model':d['model'],'choices':[{'finish_reason':'stop','message':{'content':'{"answer":8}'}}]}
  with patch.object(c,'request_json',return_value=response) as call:
   for json_mode in [False,True]:
    result=c.chat(d,[{'role':'user','content':'返回 JSON: 3+5'}],max_tokens=1800,json_mode=json_mode)
    body=call.call_args.args[2]
    self.assertEqual(body['max_tokens'],1800);self.assertNotIn('max_completion_tokens',body)
    self.assertEqual(body['thinking'],{'type':'disabled'});self.assertNotIn('enable_thinking',body)
    self.assertEqual('response_format' in body,json_mode);self.assertEqual(result['finish_reason'],'stop')
 def test_connection_setup_retries_before_one_post(self):
  import httpcore
  from httpcore._backends.sync import SyncBackend
  original=SyncBackend.connect_tcp;attempts=[]
  def connect(backend,*args,**kwargs):
   attempts.append(1)
   if len(attempts)==1:raise httpcore.ConnectError('fixture transient connect error')
   return original(backend,*args,**kwargs)
  d=self.create()
  with patch.object(SyncBackend,'connect_tcp',autospec=True,side_effect=connect):tested=c.test(d['id'])
  self.assertTrue(tested['last_test']['ok'],tested);self.assertEqual(len(attempts),2);self.assertEqual(len(Provider.calls),1)
 def test_submitted_requests_are_not_automatically_replayed(self):
  for mode in ['unavailable','disconnect']:
   with self.subTest(mode=mode):
    d=self.create();Provider.mode=mode;Provider.calls.clear();tested=c.test(d['id'])
    self.assertFalse(tested['last_test']['ok']);self.assertEqual(len(Provider.calls),1)
    self.assertNotIn('provider-should-not-leak',json.dumps(tested))
 def test_total_deadline_also_applies_without_cancel_event(self):
  d={**c.get(self.create()['id'],True),'timeout':.15};release=threading.Event();entered=threading.Event()
  def stalled(*args):entered.set();release.wait(2);return {}
  started=time.monotonic()
  try:
   with patch.object(c,'_request_json_transport',side_effect=stalled):
    with self.assertRaisesRegex(ValueError,'总时限'):c.request_json(d,self.endpoint+'/chat/completions',{})
   self.assertTrue(entered.is_set());self.assertLess(time.monotonic()-started,.6)
  finally:release.set()
 def test_transport_slots_bound_calls_without_cancel_event(self):
  d=c.get(self.create()['id'],True);release=threading.Event();entered=threading.Barrier(5);errors=[]
  def stalled(*args):entered.wait(timeout=2);release.wait(2);return {}
  def invoke():
   try:c.request_json(d,self.endpoint+'/chat/completions',{})
   except BaseException as exc:errors.append(exc)
  workers=[threading.Thread(target=invoke) for _ in range(4)]
  with patch.object(c,'_request_json_transport',side_effect=stalled) as transport:
   try:
    for worker in workers:worker.start()
    entered.wait(timeout=2)
    with self.assertRaisesRegex(ValueError,'繁忙'):c.request_json({**d,'timeout':.15},self.endpoint+'/chat/completions',{})
    self.assertEqual(transport.call_count,4)
   finally:
    release.set()
    for worker in workers:worker.join(2)
  self.assertEqual(errors,[])
 def test_bad_probe_does_not_grant_capability(self):
  d=self.create();Provider.mode='wrong';r=self.client.post('/api/connections/'+d['id']+'/test').json();self.assertFalse(r['last_test']['ok']);self.assertFalse(r['enabled'])
 def test_vision_separate_image_probe(self):
  d=self.ready_connection('vision');payload=Provider.calls[-1][2];self.assertEqual(payload['messages'][0]['content'][1]['type'],'image_url');self.assertTrue(d['last_test']['ok'])
 def test_test_failure_disables_previous_success(self):
  d=self.ready_connection();Provider.mode='auth';r=self.client.post('/api/connections/'+d['id']+'/test').json();self.assertFalse(r['enabled']);self.assertIn('HTTP 401',r['last_test']['detail']);self.assertNotIn('provider-should-not-leak',json.dumps(r))
 def test_remote_gate_no_request_when_local_only(self):
  d=self.create(endpoint='https://example.com/v1')
  with patch.object(c.httpx.Client,'stream',side_effect=AssertionError('must not send')):
   r=self.client.post('/api/connections/'+d['id']+'/test').json();self.assertFalse(r['last_test']['ok']);self.assertIn('仅本机',r['last_test']['detail'])
 def test_redirect_not_followed(self):
  d=self.create(secret='never-forward');Provider.mode='redirect';r=self.client.post('/api/connections/'+d['id']+'/test').json();self.assertFalse(r['last_test']['ok']);self.assertEqual(len(Provider.calls),1);self.assertIn('重定向',r['last_test']['detail'])
 def test_unsafe_endpoint_rejected(self):
  for endpoint in ['http://example.com/v1','https://user:password@example.com/v1','https://example.com/?key=secret']:
   r=self.client.post('/api/connections',json={'name':'无效','kind':'text','endpoint':endpoint,'model':'x'});self.assertEqual(r.status_code,400)
 def test_sqlite_test_preview_import_readonly_and_provenance(self):
  before=s.digest(self.source);d=self.ready_connection('sqlite');tables=self.client.get('/api/connections/'+d['id']+'/tables').json()['items'];self.assertIn({'schema':'main','name':'销售 明细'},tables)
  payload={'schema_name':'main','name':'销售 明细'};preview=self.client.post('/api/connections/'+d['id']+'/preview',json=payload).json();self.assertEqual(preview['rows'][0]['amount'],12.5)
  imported=self.client.post('/api/connections/'+d['id']+'/import',json={'tables':[payload]}).json();self.assertEqual(imported['provenance'][0]['row_count'],2)
  values,_=s.read('SELECT SUM(amount) AS amount FROM '+s.quoted(imported['tables'][0]));self.assertEqual(values[0]['amount'],37)
  with s.conn() as db:self.assertIsNotNone(db.execute('SELECT * FROM ef_import_provenance WHERE table_name=?',(imported['tables'][0],)).fetchone())
  self.assertEqual(before,s.digest(self.source))
 def test_database_transport_rejects_write(self):
  d=self.create('sqlite');config=c.get(d['id'],True)
  with c.database(config) as db:
   with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM empty')
 def test_preview_injection_unknown_table_rejected(self):
  d=self.ready_connection('sqlite');r=self.client.post('/api/connections/'+d['id']+'/preview',json={'name':'empty; DROP TABLE empty;--'});self.assertEqual(r.status_code,400)
 def test_import_atomic_when_later_table_invalid(self):
  d=self.ready_connection('sqlite');before=s.digest(s.BUSINESS_DB)
  r=self.client.post('/api/connections/'+d['id']+'/import',json={'tables':[{'name':'empty'},{'name':'missing'}]});self.assertEqual(r.status_code,400);self.assertEqual(before,s.digest(s.BUSINESS_DB))
 def test_deleted_connection_keeps_import_and_history(self):
  d=self.ready_connection('sqlite');r=self.client.request('DELETE','/api/connections/'+d['id'],json={});self.assertEqual(r.status_code,200)
  with s.conn() as db:self.assertGreater(db.execute('SELECT COUNT(*) FROM ef_connection_events WHERE connection_id=?',(d['id'],)).fetchone()[0],0)
 def test_outbound_preview_is_exact_and_idempotent(self):
  d=self.ready_connection();p=self.prepare(d);self.assertNotIn('messages',p);before=len(Provider.calls)
  r=self.execute(p).json();self.assertEqual(r['status'],'completed');self.assertFalse(r['verified']);self.assertEqual(Provider.calls[-1][2]['messages'][-1]['content'],p['outbound'])
  self.assertEqual(self.execute(p).json()['id'],r['id']);self.assertEqual(len(Provider.calls),before+1)
 def test_context_opt_in_and_original_result_unchanged(self):
  result=engine.execute('2024年华东销售冠军是谁？');d=self.ready_connection();p=self.prepare(d,run_id=result['run_id']);self.assertNotIn(result['answer'],p['outbound'])
  p=self.prepare(d,run_id=result['run_id'],include_context=True);self.assertIn(result['answer'],p['outbound']);self.execute(p)
  self.assertEqual(s.get_run(result['run_id'])['claims'],result['claims']);self.assertEqual(s.get_run(result['run_id'])['coverage'],result['coverage'])
 def test_no_execute_without_acceptance(self):
  p=self.prepare(self.ready_connection());before=len(Provider.calls);self.assertEqual(self.client.post('/api/extensions/'+p['id']+'/execute',json={'accepted':False}).status_code,400);self.assertEqual(before,len(Provider.calls))
 def test_config_change_invalidates_prepared_request(self):
  d=self.ready_connection();p=self.prepare(d);self.client.patch('/api/connections/'+d['id']+'/enabled',json={'enabled':True});self.assertEqual(self.execute(p).status_code,409)
 def test_expired_preview_not_sent(self):
  p=self.prepare(self.ready_connection());v=extensions.get(p['id']);v['created_at']='2001-01-01T00:00:00+00:00';extensions.put(v);before=len(Provider.calls);self.assertEqual(self.execute(p).status_code,410);self.assertEqual(before,len(Provider.calls))
 def test_provider_failure_stored_and_not_auto_retried(self):
  p=self.prepare(self.ready_connection());Provider.mode='empty';r=self.execute(p).json();self.assertEqual(r['status'],'failed');before=len(Provider.calls);self.execute(p);self.assertEqual(before,len(Provider.calls))
 def test_search_protocol_and_inert_urls(self):
  d=self.create('wikipedia');self.client.patch('/api/settings/privacy',json={'local_only':False})
  # Contract fixture only. Real public search is verified separately in artifacts.
  raw={'query':{'search':[{'pageid':123,'title':'SQLite','snippet':'<b>数据库</b>'}]}}
  with patch.object(c,'request_json',return_value=raw):
   tested=self.client.post('/api/connections/'+d['id']+'/test').json();self.assertTrue(tested['last_test']['ok'])
   self.client.patch('/api/connections/'+d['id']+'/enabled',json={'enabled':True});p=self.prepare(d,question='SQLite');r=self.execute(p).json();self.assertEqual(r['result']['items'][0]['snippet'],'数据库');self.assertEqual(r['result']['items'][0]['url'],'https://zh.wikipedia.org/?curid=123')
 def test_privacy_reenabled_blocks_prepared_remote(self):
  d=self.create('wikipedia');self.client.patch('/api/settings/privacy',json={'local_only':False})
  with patch.object(c,'search',return_value=[{'url':'https://example.org'}]):
   self.client.post('/api/connections/'+d['id']+'/test');self.client.patch('/api/connections/'+d['id']+'/enabled',json={'enabled':True});p=self.prepare(d)
  self.client.patch('/api/settings/privacy',json={'local_only':True});self.assertEqual(self.execute(p).status_code,403)
 def test_offline_share_hashes_citations_no_originals_default(self):
  result=engine.execute('2024年华东销售冠军是谁？结合项目复盘说明方法和奖金。')
  meta=self.client.post('/api/sharing/preview',json={'id':result['run_id']}).json();self.assertGreater(meta['evidence_count'],0)
  req=urllib.request.Request(self.client.url+'/api/sharing/download',data=json.dumps({'id':result['run_id']}).encode(),headers={'Content-Type':'application/json'})
  data=urllib.request.urlopen(req).read()
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   self.assertEqual(set(z.namelist()),{'index.html','snapshot.json','manifest.json'});manifest=json.loads(z.read('manifest.json'))
   for f in manifest['files']:self.assertEqual(hashlib.sha256(z.read(f['path'])).hexdigest(),f['sha256'])
   page=z.read('index.html').decode();self.assertNotIn('<script',page);self.assertNotIn('127.0.0.1',page);self.assertIn('source-',page)
   self.assertEqual(json.loads(z.read('snapshot.json'))['claims'],result['claims'])
 def test_share_only_selected_versioned_original(self):
  result=engine.execute('2024年华东销售冠军的奖金是多少？');req=sharing.Share(id=result['run_id']);p=sharing.preview(req);chosen=p['originals'][0]
  response=sharing.download(sharing.Share(id=result['run_id'],originals=[chosen['id']]))
  with zipfile.ZipFile(io.BytesIO(response.body)) as z:
   files=[x for x in z.namelist() if x.startswith('originals/')];self.assertEqual(len(files),1);self.assertEqual(hashlib.sha256(z.read(files[0])).hexdigest(),chosen['version'])
  self.assertEqual(self.client.post('/api/sharing/download',json={'id':result['run_id'],'originals':['../../secret']}).status_code,400)
 def test_vision_previews_actual_pinned_page(self):
  png,_=c.vision_probe();path=s.UPLOADS/'vision-test.png';path.write_bytes(png);doc=s.index_document(path);d=self.ready_connection('vision')
  p=self.prepare(d,document_id=doc['id'],version=doc['sha256'],page=1)
  self.assertEqual(p['image']['version'],doc['sha256']);self.assertEqual(p['image']['page'],1);self.assertNotIn('image_data',p)
  response=self.execute(p).json();self.assertEqual(response['status'],'completed');self.assertFalse(response['verified'])
  self.assertFalse(s.doc_get(doc['id'])['status']=='verified')
 def test_invalid_vision_version_is_never_sent(self):
  d=self.ready_connection('vision');before=len(Provider.calls)
  r=self.client.post('/api/extensions/prepare',json={'connection_id':d['id'],'question':'读取','document_id':'missing','version':'a'*64});self.assertEqual(r.status_code,400);self.assertEqual(before,len(Provider.calls))
 def test_brave_adapter_extracts_source_and_drops_unsafe_links(self):
  d=self.create('brave',secret='fixture-brave-key');config=c.get(d['id'],True)
  raw={'web':{'results':[{'title':'数据库','url':'https://example.org/sql','description':'<b>SQL</b>'},{'title':'bad','url':'javascript:alert(1)'}]}}
  with patch.object(c,'request_json',return_value=raw) as call:
   rows=c.search(config,'SQLite');self.assertEqual(len(rows),1);self.assertEqual(rows[0]['snippet'],'SQL');self.assertEqual(call.call_args.kwargs['headers']['X-Subscription-Token'],'fixture-brave-key')
 def test_scenario_share_preserves_formula_and_parameters(self):
  from backend import scenarios
  result=engine.execute('2024 年华东销售冠军的奖金是多少？')
  base=scenarios.prepare(scenarios.Prepare(run_id=result['run_id']))
  parameters={k:{'value':p['value'],'unit':p['unit']} for k,p in base['rules']['historical']['parameters'].items()}
  parameters['growth']['value']='0.1'
  calc=scenarios.compute(scenarios.Compute(baseline_id=base['id'],parameters=parameters))
  package=sharing.download(sharing.Share(kind='scenario',id=calc['id']))
  with zipfile.ZipFile(io.BytesIO(package.body)) as z:
   exported=json.loads(z.read('snapshot.json'));self.assertEqual(exported['value'],calc['value']);self.assertEqual(exported['parameters'],calc['parameters']);self.assertIn('情景是假设计算',z.read('index.html').decode())
 def test_restart_stops_pending_requests_without_resubmission(self):
  p=self.prepare(self.ready_connection());before=len(Provider.calls);c.initialize();r=self.execute(p).json();self.assertEqual(r['status'],'failed');self.assertIn('重启',r['error']);self.assertEqual(before,len(Provider.calls))
 def test_invalid_body_does_not_echo_secret(self):
  r=self.client.post('/api/connections',json={'name':'x','kind':'text','secret':'s'*4100});self.assertEqual(r.status_code,422);self.assertNotIn('s'*50,r.text)

if __name__=='__main__':unittest.main(verbosity=2)
