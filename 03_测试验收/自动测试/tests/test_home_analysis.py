"""Stage 5 integration checks with isolated databases and a real HTTP server."""
import io
import sqlite3
from contextlib import contextmanager
import json
import time
import unittest
import uuid
import urllib.request
import urllib.error
from tests.test_conversations import LocalClient, TEMP, conv, s, engine

@contextmanager
def write_business():
    connection=sqlite3.connect(s.BUSINESS_DB)
    try:
        with connection: yield connection
    finally: connection.close()

class HomeAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.client=LocalClient()
    @classmethod
    def tearDownClass(cls):
        conv.manager.pool.shutdown(wait=True);cls.client.close();TEMP.cleanup()
    def project(self):
        return self.client.post('/api/projects',json={'name':'阶段5独立验收'}).json()['id']
    def session(self,pid='default'):
        return self.client.post('/api/projects/'+pid+'/sessions').json()['id']
    def query(self,sid,question,**extra):
        r=self.client.post('/api/tasks',json={'session_id':sid,'question':question,'request_id':str(uuid.uuid4()),**extra})
        self.assertEqual(r.status_code,200,r.text)
        for _ in range(800):
            task=self.client.get('/api/tasks/'+r.json()['id']).json()
            if task['status'] not in conv.ACTIVE:
                self.assertEqual(task['status'],'completed',task)
                return task['result']
            time.sleep(.01)
        self.fail('Task timed out')
    def detail(self,sid):return self.client.get('/api/sessions/'+sid).json()
    def upload(self,filename,content):
        boundary='stage5-'+uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+content+f'\r\n--{boundary}--\r\n'.encode()
        req=urllib.request.Request(self.client.url+'/api/import',data=body,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        try:r=urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:r=e
        return r.status,json.loads(r.read())
    def test_dashboard_is_project_scoped_and_excludes_checks(self):
        pid=self.project();sid=self.session(pid)
        self.query(sid,'2024 年华东净销售额是多少？')
        before=self.client.get('/api/dashboard?project_id='+pid).json()
        engine.execute('2024 年华东净销售额是多少？')
        self.query(self.session(self.project()),'2024 年华北有多少客户？')
        after=self.client.get('/api/dashboard?project_id='+pid).json()
        self.assertEqual(before,after)
        self.assertEqual(after['query_count'],1);self.assertEqual(after['today_count'],1)
        self.assertEqual(sum(x['count'] for x in after['trend']),1);self.assertEqual(len(after['trend']),30)
        recent=after['recent'][0];self.assertEqual(recent['session_id'],sid)
        self.assertEqual(recent['result_message_id'],self.detail(sid)['selected_message_id'])
        self.assertEqual(self.client.get('/api/dashboard?project_id=missing').status_code,404)
    def test_restore_history_does_not_execute_again(self):
        pid=self.project();sid=self.session(pid)
        original=self.query(sid,'2024 年华东净销售额是多少？');first=self.detail(sid)['selected_message_id']
        self.query(sid,'2024 年华北净销售额是多少？')
        before=self.client.get('/api/dashboard?project_id='+pid).json()['query_count']
        restored=self.client.patch('/api/sessions/'+sid,json={'selected_message_id':first}).json()
        self.assertEqual(next(m['result'] for m in restored['messages'] if m['id']==first),original)
        self.assertEqual(self.client.get('/api/dashboard?project_id='+pid).json()['query_count'],before)
    def test_recompute_undo_and_bound_evidence(self):
        sid=self.session();before=self.query(sid,'2024 年华东销售冠军是谁？结合项目复盘说明方法和奖金。')
        mid=self.detail(sid)['selected_message_id']
        after=self.query(sid,before['question'],mode='recompute',parent_message_id=mid,confirmed={'region':'华北'},inherit_context=False)
        self.assertEqual(after['parent_run_id'],before['run_id']);self.assertEqual(after['query_spec']['region'],'华北')
        states={c['key']:c['state'] for c in after['conditions']}
        self.assertEqual(states['region'],'confirmed');self.assertEqual(states['rule_mode'],'default')
        self.assertEqual(states['year'],'explicit')
        self.assertTrue(any(c['status']=='needs_evidence' and not c['evidence_ids'] for c in after['claims']))
        for result in [before,after]:
            ids={e['id'] for e in result['evidence']}
            self.assertTrue(all(set(c['evidence_ids'])<=ids for c in result['claims']))
            sql=next(e for e in result['evidence'] if e['type']=='sql_result')
            self.assertEqual(sql['locator']['rows'],result['rows'])
            self.assertEqual(s.read(sql['locator']['sql'],sql['locator']['params'])[0],result['rows'])
        self.client.patch('/api/sessions/'+sid,json={'selected_message_id':mid})
        follow=self.query(sid,'它的奖金是多少？',parent_message_id=mid)
        self.assertEqual(follow['query_spec']['region'],'华东')
        self.assertEqual(follow['formulas'][0]['value'],before['formulas'][0]['value'])
    def test_gross_ranking_keeps_bonus_on_net(self):
        with write_business() as c:
            c.execute("UPDATE orders SET sales_amount=sales_amount+900000 WHERE order_id=(SELECT MIN(order_id) FROM orders WHERE region='华东')")
        sid=self.session();r=self.query(sid,'2024 年华东销售冠军的奖金是多少？',confirmed={'metric':'gross_sales'})
        first=r['rows'][0]
        self.assertIn('gross_sales',first);self.assertNotIn('net_sales',first)
        self.assertIn('销售额（未扣退货）',r['headline'])
        expected=s.read("SELECT SUM(sales_amount-returned_amount) AS net FROM orders WHERE region='华东' AND strftime('%Y',order_date)='2024' AND product_id=?",[first['product_id']])[0][0]['net']
        f=r['formulas'][0];self.assertEqual(f['actual_sales'],expected)
        rule=s.read("SELECT threshold_amount,bonus_rate FROM business_rules WHERE effective_from <= '2024-01-01' AND (effective_to IS NULL OR effective_to >= '2024-12-31')")[0][0]
        self.assertEqual(float(f['value']),round(max(0,expected-rule['threshold_amount'])*rule['bonus_rate'],2))
        self.assertGreater(float(f['value']),0)
        param=f['parameters'][0];e=next(e for e in r['evidence'] if e['id']==param['evidence_ids'][0])
        self.assertEqual(e['locator']['rows'][0]['net_sales'],expected)
    def test_gross_total_label_and_missing_period(self):
        r=self.query(self.session(),'2024 年华北销售额是多少？')
        expected=s.read("SELECT SUM(sales_amount) AS x FROM orders WHERE region='华北' AND strftime('%Y',order_date)='2024'")[0][0]['x']
        self.assertEqual(r['rows'][0]['gross_sales'],expected)
        r=self.query(self.session(),'2024 年华东净销售额比 2023 年增长多少？')
        self.assertIsNone(r['rows'][0]['compare_net_sales']);self.assertEqual(r['metrics'],[])
        self.assertIn('数据缺失',r['answer'])
    def test_comparison_checks_both_periods_and_rejects_same_year(self):
        sid=self.session()
        r=self.query(sid,'2024 年华东净销售额增长率是多少？',confirmed={'compare_year':2024})
        self.assertEqual(r['status'],'needs_clarification');self.assertEqual(r['clarification']['field'],'compare_year')
        with write_business() as c:
            c.execute("UPDATE orders SET currency='USD' WHERE order_date LIKE '2025-%' AND region='华东'")
        try:
            r=self.query(sid,'2024 年华东净销售额比 2025 年增长多少？')
            self.assertEqual(r['metrics'],[]);self.assertEqual(r['rows'],[])
            self.assertTrue(any(c['name']=='币种' and c['status']=='failed' for c in r['checks']))
        finally:
            with write_business() as c:c.execute("UPDATE orders SET currency='CNY' WHERE currency='USD'")
    def test_personnel_not_invented(self):
        r=self.query(self.session(),'2024 年华东销售人员冠军是谁？')
        self.assertEqual(r['rows'],[]);self.assertEqual(r['evidence'],[])
        self.assertIn('没有销售人员归属',r['answer'])
    def test_real_csv_import_preview_archive(self):
        code,r=self.upload('stage5.csv','产品,金额,备注\n产品甲,125.5,第一行\n产品乙,240,第二行\n'.encode())
        self.assertEqual(code,200);self.assertTrue(r['ok'])
        table=r['tables'][0];rows=self.client.get('/api/tables/'+table+'/rows').json()
        self.assertEqual(rows['total'],2);self.assertEqual(rows['rows'][0]['金额'],125.5)
        self.client.post('/api/tables/'+table+'/archive')
        self.assertNotIn(table,[x['name'] for x in self.client.get('/api/tables').json()])
        self.client.post('/api/tables/'+table+'/restore')
        self.assertIn(table,[x['name'] for x in self.client.get('/api/tables').json()])
    def test_failed_sheet_cannot_leave_partial_import(self):
        import pandas as pd
        out=io.BytesIO()
        with pd.ExcelWriter(out,engine='openpyxl') as writer:
            pd.DataFrame({'good':[1]}).to_excel(writer,sheet_name='first',index=False)
            pd.DataFrame({'A':[1],'a':[2]}).to_excel(writer,sheet_name='invalid',index=False)
        before=[t['name'] for t in s.tables()]
        code,r=self.upload('bad.xlsx',out.getvalue())
        self.assertEqual(code,400);self.assertIn('重复',r['detail'])
        self.assertEqual(before,[t['name'] for t in s.tables()])
        code,_=self.upload('empty.csv',b'');self.assertEqual(code,400)
    def test_broken_document_retains_original(self):
        code,r=self.upload('broken.pdf',b'not a pdf')
        self.assertEqual(code,200);self.assertFalse(r['ok']);self.assertEqual(r['document']['status'],'failed')
        self.assertTrue(r['document']['quality']['errors'])

if __name__=='__main__':unittest.main()
