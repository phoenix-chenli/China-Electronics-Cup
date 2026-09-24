"""Stage 7: disposable HTTP integration tests with independent Decimal oracles."""
import copy
import json
import random
import sqlite3
import unittest
from decimal import Decimal as D, ROUND_HALF_UP
from tests.test_conversations import LocalClient
from backend import store as s, engine


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = LocalClient()
        # An explicit test-only order places the champion above the threshold.
        c=sqlite3.connect(s.BUSINESS_DB)
        try:
            c.execute("INSERT INTO orders VALUES ('STAGE7_FIXTURE','2024-12-25','C001','P001','PR001','华东',1,120000,0,'CNY')")
            c.commit()
        finally: c.close()
    @classmethod
    def tearDownClass(cls): cls.client.close()
    def prepare(self, region='华东', **kwargs):
        result=engine.execute(f'2024 年{region}销售冠军和奖金')
        response=self.client.post('/api/scenarios/baselines',json={'run_id':result['run_id'],**kwargs})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def request(self,b,changes=None,mode='historical'):
        params={k:{'value':str(p['value']),'unit':p['unit']} for k,p in b['rules'][mode]['parameters'].items()}
        for k,v in (changes or {}).items(): params[k]['value']=str(v)
        return {'baseline_id':b['id'],'rule_mode':mode,'parameters':params}
    def calculate(self,b,changes=None,mode='historical'):
        response=self.client.post('/api/scenarios/calculate',json=self.request(b,changes,mode))
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def save(self,r,name='验收方案',**extra):
        return self.client.post('/api/scenarios/plans',json={'name':name,'calculation_id':r['id'],**extra}).json()

    def test_formula_matches_independent_sql_and_decimal(self):
        before=s.digest(s.BUSINESS_DB); b=self.prepare();r=self.calculate(b,{'growth':'.05','threshold':'750000','rate':'.03'})
        c=sqlite3.connect(s.BUSINESS_DB)
        try:
            sales=D(str(c.execute("SELECT max(amount) FROM (SELECT sum(sales_amount-returned_amount) amount FROM orders WHERE region='华东' AND order_date BETWEEN '2024-01-01' AND '2024-12-31' GROUP BY product_id)").fetchone()[0]))
        finally: c.close()
        expected=(max(D(0),sales*D('1.05')-D(750000))*D('.03')).quantize(D('.01'),rounding=ROUND_HALF_UP)
        self.assertEqual(D(r['value']),expected); self.assertEqual(expected,D(5850))
        self.assertEqual(D(r['baseline_value']),D(2500));self.assertEqual(D(r['delta']),D(3350))
        self.assertEqual(r['relative_percent'],'134.00');self.assertEqual(before,s.digest(s.BUSINESS_DB))
        for p in r['parameters']:
            self.assertTrue(p['evidence_ids'] or p.get('definition'))
            if p['assumption']:self.assertEqual(p['modified_by'],'本地用户')

    def test_contributions_telescope_and_cross_threshold(self):
        b=self.prepare();rng=random.Random(707)
        for i in range(24):
            p={'sales':str(rng.randint(0,2000000)), 'growth':str(D(rng.randint(-100,200))/100),
               'threshold':str(rng.randint(0,2000000)), 'rate':str(D(rng.randint(0,100))/100)}
            r=self.calculate(b,p)
            oracle=(max(D(0),D(p['sales'])*(1+D(p['growth']))-D(p['threshold']))*D(p['rate'])).quantize(D('.01'),rounding=ROUND_HALF_UP)
            self.assertEqual(D(r['value']),oracle)
            self.assertEqual(sum(D(x['delta']) for x in r['contributions']),D(r['delta']))
        below=self.calculate(b,{'sales':'500000','growth':'0'})
        self.assertEqual(below['value'],'0.00');self.assertEqual(below['relative_percent'],'-100.00')

    def test_zero_growth_is_not_defaulted_to_five(self):
        b=self.prepare(); r=self.calculate(b,{'growth':'0'})
        self.assertEqual(r['value'],r['baseline_value']);self.assertEqual(r['delta'],'0.00')
        self.assertTrue(all(D(x['delta'])==0 for x in r['contributions']))

    def test_zero_baseline_has_no_relative_percent(self):
        b=self.prepare('华南');r=self.calculate(b,{'sales':'1500000'})
        self.assertEqual(r['baseline_value'],'0.00');self.assertIsNone(r['relative_percent'])
        self.assertGreater(D(r['value']),0)

    def test_unit_finite_limits_and_precision(self):
        b=self.prepare()
        for k,v in [('growth','NaN'),('sales','Infinity'),('sales',''),('sales','-1'),('growth','2.01'),('rate','1.1'),('threshold','0.001'),('rate','.01234567')]:
            response=self.client.post('/api/scenarios/calculate',json=self.request(b,{k:v}))
            self.assertEqual(response.status_code,400,(k,v,response.text))
        p=self.request(b);p['parameters']['sales']['unit']='万元'
        self.assertEqual(self.client.post('/api/scenarios/calculate',json=p).status_code,400)
        del p['parameters']['sales'];self.assertEqual(self.client.post('/api/scenarios/calculate',json=p).status_code,400)
        p=self.request(b);p['expression']='__import__("os")'
        self.assertEqual(self.client.post('/api/scenarios/calculate',json=p).status_code,422)

    def test_rounding_at_cent_boundary(self):
        b=self.prepare();r=self.calculate(b,{'sales':'1.01','growth':'0','threshold':'1','rate':'.5'})
        self.assertEqual(r['value'],'0.01')
        self.assertEqual(sum(D(x['delta']) for x in r['contributions']),D(r['delta']))

    def test_current_rule_preserves_baseline_sales_and_evidence(self):
        b=self.prepare();r=self.calculate(b,mode='current')
        self.assertEqual(r['kind'],'scenario');self.assertEqual(r['rule_mode'],'current')
        self.assertEqual(r['baseline_value'],'2500.00')
        self.assertEqual(b['rules']['current']['parameters']['sales'],b['rules']['historical']['parameters']['sales'])
        self.assertNotEqual(b['rules']['current']['effective_from'],b['rules']['historical']['effective_from'])
        self.assertEqual(r['value'],'0.00')

    def test_snapshot_save_restore_and_versions_are_server_owned(self):
        b=self.prepare();a=self.calculate(b,{'growth':'.05'});p=self.save(a)
        again=self.client.get('/api/scenarios/plans/'+p['id']).json()
        self.assertEqual(again['calculation'],p['calculation'])
        later=self.calculate(b,{'growth':'.1'})
        response=self.client.request('PUT','/api/scenarios/plans/'+p['id'],json={'name':'调整方案','calculation_id':later['id'],'expected_revision':1})
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['revision'],2)
        old=self.client.get('/api/scenarios/plans/'+p['id']+'/versions/1').json()
        self.assertEqual(old['calculation']['value'],a['value'])
        restored=self.calculate(old['baseline'],{x['name']:x['value'] for x in old['calculation']['parameters']})
        self.assertEqual(restored['value'],a['value'])
        self.assertEqual(self.client.request('PUT','/api/scenarios/plans/'+p['id'],json={'name':'冲突','calculation_id':a['id'],'expected_revision':1}).status_code,409)
        bad=self.client.post('/api/scenarios/plans',json={'name':'伪造','calculation_id':a['id'],'value':'999999'})
        self.assertEqual(bad.status_code,422)

    def test_same_name_creates_distinct_plans_and_rename_validates(self):
        b=self.prepare();r=self.calculate(b);a=self.save(r);p=self.save(r)
        self.assertNotEqual(a['id'],p['id'])
        response=self.client.patch('/api/scenarios/plans/'+a['id'],json={'name':'   ','expected_revision':1})
        self.assertEqual(response.status_code,400)
        response=self.client.patch('/api/scenarios/plans/'+a['id'],json={'name':'新名','expected_revision':1})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['revision'],2)
        self.assertEqual(self.client.get('/api/scenarios/plans/'+p['id']).json()['name'],'验收方案')

    def test_soft_delete_and_restore(self):
        p=self.save(self.calculate(self.prepare()))
        path='/api/scenarios/plans/'+p['id']
        self.assertEqual(self.client.request('DELETE',path).json()['recoverable'],True)
        self.assertNotIn(p['id'],{x['id'] for x in self.client.get('/api/scenarios/plans').json()})
        self.assertIn(p['id'],{x['id'] for x in self.client.get('/api/scenarios/plans?deleted=true').json()})
        self.client.post(path+'/restore');self.assertIn(p['id'],{x['id'] for x in self.client.get('/api/scenarios/plans').json()})

    def test_stale_does_not_mutate_frozen_calculation(self):
        b=self.prepare();r=self.calculate(b);p=self.save(r)
        try:
            with s.conn() as c:c.execute("INSERT INTO definitions VALUES ('stage7-stale','{}',?,0)",(s.now(),))
            stale=self.client.get('/api/scenarios/plans/'+p['id']).json()
            self.assertTrue(stale['stale']);self.assertEqual(stale['calculation'],p['calculation'])
            recomputed=self.calculate(b);self.assertEqual(recomputed['value'],r['value']);self.assertTrue(recomputed['stale'])
        finally:
            with s.conn() as c:c.execute("DELETE FROM definitions WHERE id='stage7-stale'")

    def test_export_contains_recomputable_snapshot_and_source_versions(self):
        b=self.prepare();r=self.calculate(b,{'growth':'.15'})
        payload=self.client.get('/api/scenarios/calculations/'+r['id']+'/export').json()
        self.assertEqual(payload['format'],'evidenceflow-scenario-v1')
        p={x['name']:D(x['value']) for x in payload['calculation']['parameters']}
        oracle=(max(D(0),p['sales']*(1+p['growth'])-p['threshold'])*p['rate']).quantize(D('.01'),rounding=ROUND_HALF_UP)
        self.assertEqual(str(oracle),payload['calculation']['value'])
        self.assertTrue(any(e['locator'].get('sha256') for e in payload['calculation']['evidence']))

    def test_project_and_session_isolation(self):
        project=self.client.post('/api/projects',json={'name':'情景隔离'}).json()
        b=self.prepare(project_id=project['id']);p=self.save(self.calculate(b))
        self.assertNotIn(p['id'],{x['id'] for x in self.client.get('/api/scenarios/plans').json()})
        self.assertIn(p['id'],{x['id'] for x in self.client.get('/api/scenarios/plans?project_id='+project['id']).json()})
        sid=self.client.post('/api/projects/default/sessions').json()['id']
        r=engine.execute('2024 年华东奖金')
        bad=self.client.post('/api/scenarios/baselines',json={'run_id':r['run_id'],'project_id':project['id'],'session_id':sid})
        self.assertEqual(bad.status_code,400)
        other=self.calculate(self.prepare())
        bad=self.client.request('PUT','/api/scenarios/plans/'+p['id'],json={'name':'跨项目覆盖','calculation_id':other['id'],'expected_revision':1})
        self.assertEqual(bad.status_code,400)

    def test_source_mismatch_cannot_be_used_as_baseline(self):
        original=engine.execute('2024 年华东销售冠军和奖金')
        broken=copy.deepcopy(original);broken['formulas'][0]['parameters'][0]['evidence_ids']=['missing']
        with s.conn() as c:c.execute('UPDATE runs SET result_json=? WHERE id=?',(json.dumps(broken),broken['run_id']))
        response=self.client.post('/api/scenarios/baselines',json={'run_id':broken['run_id']})
        self.assertEqual(response.status_code,400)

    def test_refresh_creates_new_baseline_without_changing_saved_plan(self):
        b=self.prepare();p=self.save(self.calculate(b))
        c=sqlite3.connect(s.BUSINESS_DB)
        try:
            c.execute("UPDATE orders SET sales_amount=sales_amount+10000 WHERE order_id='STAGE7_FIXTURE'");c.commit()
            response=self.client.post('/api/scenarios/baselines',json={'run_id':b['origin_run_id'],'refresh':True})
            self.assertEqual(response.status_code,200,response.text);fresh=response.json()
            self.assertNotEqual(fresh['id'],b['id'])
            self.assertEqual(self.calculate(fresh)['baseline_value'],'2750.00')
            old=self.client.get('/api/scenarios/plans/'+p['id']).json()
            self.assertEqual(old['baseline_id'],b['id']);self.assertEqual(old['value'],'2500.00');self.assertTrue(old['stale'])
        finally:
            c.execute("UPDATE orders SET sales_amount=sales_amount-10000 WHERE order_id='STAGE7_FIXTURE'");c.commit();c.close()


if __name__=='__main__': unittest.main()
