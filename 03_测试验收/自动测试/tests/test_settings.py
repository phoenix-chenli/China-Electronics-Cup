"""Stage 8: actual local HTTP, definition isolation, and RAM-only privacy checks."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch
from tests.test_conversations import LocalClient
from backend import store as s, engine, settings, conversations

class SettingsTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls): cls.client=LocalClient()
 @classmethod
 def tearDownClass(cls): cls.client.close()
 def counts(self):
  with s.conn() as c:return {t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['runs','ef_tasks','ef_messages','ef_sessions','saved_analyses','documents']}
 def query(self,token,q,**kwargs):return self.client.post('/api/temporary/'+token+'/query',json={'question':q,**kwargs})
 def start(self):return self.client.post('/api/temporary/sessions').json()['id']
 def define(self,**kwargs):return self.client.post('/api/definitions',json={'kind':'metric','name':'净营收测试','aliases':['NTR_TEST'],'target':'net_sales',**kwargs})
 def test_local_policy_defaults_and_explicit_change(self):
  r=self.client.get('/api/settings').json();self.assertTrue(r['local_only']);self.assertTrue(r['can_disable_local_only']);self.assertIsInstance(r['external_services'],list)
  self.assertEqual(self.client.patch('/api/settings/privacy',json={'local_only':False}).status_code,200)
  self.client.patch('/api/settings/privacy',json={'local_only':True})
  self.assertEqual(self.client.patch('/api/settings/privacy',json={'local_only':True,'key':'secret'}).status_code,422)
 def test_temporary_no_durable_rows_or_files(self):
  before=self.counts();files={str(p):s.digest(p) for p in s.UPLOADS.rglob('*') if p.is_file()};business=s.digest(s.BUSINESS_DB)
  token=self.start();r=self.query(token,'2024 年华东净销售额是多少？');self.assertEqual(r.status_code,200,r.text)
  result=r.json();self.assertTrue(result['temporary']);self.assertEqual(result['actions'],[]);self.assertEqual(self.counts(),before)
  self.assertEqual(files,{str(p):s.digest(p) for p in s.UPLOADS.rglob('*') if p.is_file()});self.assertEqual(s.digest(s.BUSINESS_DB),business)
  with self.assertRaises(ValueError):s.get_run(result['run_id'])
  self.client.post('/api/temporary/'+token+'/close');self.assertEqual(self.query(token,'它的奖金？').status_code,410)
 def test_temporary_context_isolated_and_opt_out(self):
  a,b=self.start(),self.start();self.query(a,'2024 年华东销售冠军是谁？')
  r=self.query(a,'它的奖金是多少？').json();self.assertEqual(r['query_spec']['region'],'华东');self.assertEqual(r['query_spec']['product'],'云安全平台')
  self.assertEqual(self.query(b,'它的奖金是多少？').json()['status'],'needs_clarification')
  self.assertEqual(self.query(a,'它的奖金是多少？',inherit_context=False).json()['status'],'needs_clarification')
  for token in [a,b]:self.client.post('/api/temporary/'+token+'/close')
 def test_temporary_clarification_and_independent_sql(self):
  t=self.start();r=self.query(t,'华北净销售额是多少？').json();self.assertEqual(r['status'],'needs_clarification')
  r=self.query(t,r['question'],confirmed={'year':2024}).json();oracle=s.read("SELECT SUM(sales_amount-returned_amount) amount FROM orders WHERE region='华北' AND order_date BETWEEN '2024-01-01' AND '2024-12-31'")[0][0]['amount']
  self.assertEqual(r['rows'][0]['net_sales'],oracle);self.client.post('/api/temporary/'+t+'/close')
 def test_temporary_cannot_upload_or_inject_persistent_context(self):
  t=self.start();self.assertEqual(self.query(t,'测试',run_id='unknown').status_code,422)
  self.assertEqual(self.client.post('/api/temporary/'+t+'/upload',json={'contents':'test'}).status_code,405)
  self.client.post('/api/temporary/'+t+'/close')
 def test_expired_temporary_session_is_not_recoverable(self):
  t=self.start();settings.temporary.sessions[t]['expires']=time.monotonic()-1
  self.assertEqual(self.query(t,'2024年净销售额').status_code,410);self.assertNotIn(t,settings.temporary.sessions)
 def test_close_wins_against_late_temporary_result(self):
  store=settings.TemporaryStore();token=store.create();started=threading.Event();finish=threading.Event();errors=[];before=self.counts()
  original=engine.execute
  def slow(*args,**kwargs):started.set();finish.wait(3);return original(*args,**kwargs)
  def work():
   try:store.query(token,settings.TemporaryRequest(question='2024年华东净销售额'))
   except Exception as exc:errors.append(exc)
  with patch.object(engine,'execute',slow):
   th=threading.Thread(target=work);th.start();self.assertTrue(started.wait(2));store.close(token);finish.set();th.join(3)
  self.assertEqual(errors[0].status_code,410);self.assertNotIn(token,store.sessions);self.assertEqual(self.counts(),before)
 def test_help_tone_does_not_change_evidence(self):
  a=self.client.post('/api/assistant/help',json={'question':'如何查看来源？'}).json();b=self.client.post('/api/assistant/help',json={'question':'如何查看来源？','tone':'friendly'}).json()
  self.assertTrue(b['text'].endswith(a['text']));self.assertEqual(a['evidence'],b['evidence']);self.assertEqual(a['action'],b['action'])
 def test_help_settings_describes_actual_supplied_preference(self):
  r=self.client.post('/api/assistant/help',json={'question':'当前设置','preferences':{'theme':'dark','fontSize':18}}).json()
  self.assertIn('深色',r['text']);self.assertIn('18px',r['text']);self.assertEqual(r['mode'],'local-help')
 def test_help_unknown_routes_to_query_not_invented_answer(self):
  r=self.client.post('/api/assistant/help',json={'question':'2024年华北销售冠军和奖金'}).json();self.assertEqual(r['kind'],'query');self.assertNotIn('元',r['text'])
 def test_help_context_must_belong_to_project_session(self):
  sid=self.client.post('/api/projects/default/sessions').json()['id'];other=self.client.post('/api/projects',json={'name':'设置隔离'}).json()['id']
  for body in [{'project_id':other,'session_id':sid},{'session_id':sid,'run_id':'not-there'}]:
   self.assertEqual(self.client.post('/api/assistant/help',json={'question':'当前结果',**body}).status_code,400)
 def test_scenario_help_uses_server_calculation_and_rejects_wrong_project(self):
  result=engine.execute('2024年华东销售冠军与奖金')
  b=self.client.post('/api/scenarios/baselines',json={'run_id':result['run_id']}).json()
  params={k:{'value':str(p['value']),'unit':p['unit']} for k,p in b['rules']['historical']['parameters'].items()}
  params['sales']['value']='900000';params['growth']['value']='0.05'
  calc=self.client.post('/api/scenarios/calculate',json={'baseline_id':b['id'],'rule_mode':'historical','parameters':params}).json()
  response=self.client.post('/api/assistant/help',json={'question':'解释情景参数','page':'scenario','scenario_id':calc['id']})
  self.assertEqual(response.status_code,200,response.text);answer=response.json()
  self.assertIn('3,625.00 元',answer['text']);self.assertIn('用户假设',answer['text']);self.assertEqual({e['id'] for e in answer['evidence']},{eid for p in calc['parameters'] for eid in p.get('evidence_ids',[])});self.assertEqual(answer['state'],'explain_left')
  other=self.client.post('/api/projects',json={'name':'其他情景项目'}).json()['id']
  self.assertEqual(self.client.post('/api/assistant/help',json={'question':'解释情景参数','scenario_id':calc['id'],'project_id':other}).status_code,400)
 def test_definition_aliases_versions_and_old_snapshot(self):
  r=self.define(name='净营收测试1',aliases=['NTR_STAGE8']).json();before=engine.execute('2024年华东 NTR_STAGE8 合计')
  self.assertEqual(before['query_spec']['metric'],'net_sales');self.assertEqual(before['query_spec']['definitions_snapshot'][0]['id'],r['id'])
  edited=self.define(id=r['id'],name='净营收测试1',aliases=['GTR_STAGE8'],target='gross_sales');self.assertEqual(edited.status_code,200,edited.text)
  after=engine.execute('2024年华东 GTR_STAGE8 合计');self.assertEqual(after['query_spec']['metric'],'gross_sales')
  self.assertEqual(s.get_run(before['run_id'])['query_spec']['definitions_snapshot'][0]['target'],'net_sales')
  with s.conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM definition_versions WHERE definition_id=?',(r['id'],)).fetchone()[0],2)
 def test_conflict_across_names_aliases_and_reserved_terms(self):
  self.define(name='设置冲突A',aliases=['Revenue_STAGE8'])
  self.assertEqual(self.define(name='设置冲突B',aliases=['revenue_stage8']).status_code,400)
  self.assertEqual(self.define(name='设置冲突A',aliases=[]).status_code,400)
  self.assertEqual(self.define(name='华东',aliases=[],kind='alias',field='region',target='华北').status_code,400)
 def test_recover_definition_does_not_overwrite_conflict(self):
  a=self.define(name='恢复口径8',aliases=['Restore_STAGE8']).json();self.client.request('DELETE','/api/definitions/'+a['id'])
  self.assertTrue(any(x['id']==a['id'] for x in self.client.get('/api/definitions/archived').json()))
  b=self.define(name='另一恢复口径8',aliases=['Restore_STAGE8']).json()
  self.assertEqual(self.client.post('/api/definitions/'+a['id']+'/restore').status_code,400)
  self.client.request('DELETE','/api/definitions/'+b['id']);self.assertEqual(self.client.post('/api/definitions/'+a['id']+'/restore').status_code,200)
 def test_restore_preferences_endpoint_never_clears_history(self):
  before=self.counts();r=self.client.patch('/api/settings/privacy',json={'local_only':True});self.assertEqual(r.status_code,200);self.assertEqual(self.counts(),before)

if __name__=='__main__':unittest.main()
