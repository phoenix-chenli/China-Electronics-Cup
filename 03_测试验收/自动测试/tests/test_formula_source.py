"""Literal equation, source revision and unit protections, with independent Decimal results."""
import unittest
import uuid
from decimal import Decimal
from tests.test_conversations import LocalClient,TEMP,conv,s
from backend import documents,formula_source,engine,rag_grounding


class FormulaSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.client=LocalClient()
    @classmethod
    def tearDownClass(cls):conv.manager.pool.shutdown(wait=True);cls.client.close();TEMP.cleanup()
    def setup_formula(self):
        text='适用条件：2025年可计费检修。\nfee [元] = base + max(0, minutes - included) * price\nbase = 300元；minutes = 90分钟；included = 60分钟；price = 2元/分钟。'
        path=s.UPLOADS/(uuid.uuid4().hex+'.md');path.write_text(text);d=s.index_document(path)
        bound=documents.resolve({'document_id':d['id'],'sha256':d['sha256']},text)
        span={'text':text,'evidence_id':'doc-evidence','locator':bound['locator']}
        reference={'support_span_id':'span1','source_quote':text,'result_unit':'CNY'}
        params=[{'name':'base','value':'300','unit':'CNY'},{'name':'minutes','value':'90','unit':'min'},
                {'name':'included','value':'60','unit':'min'},{'name':'price','value':'2','unit':'CNY/min'}]
        return params,reference,{'span1':span}
    def test_source_equation_and_dimension_produce_independent_value(self):
        params,ref,spans=self.setup_formula();expression='base + max(0, minutes - included) * price'
        binding=formula_source.bind(expression,params,ref,spans,{'span1'})
        self.assertEqual(binding['unit_check'],'passed')
        self.assertEqual(engine.calculate(expression,{p['name']:p['value'] for p in params}),Decimal(360))
        for p,quote in zip(params,['300元','90分钟','60分钟','2元/分钟']):
            source=rag_grounding.document_parameter({**p,'support_span_id':'span1','source_quote':quote,'evidence_ids':['doc-evidence']},spans,{'span1'})
            self.assertEqual(source['unit'],p['unit'])
    def test_altered_formula_or_unbound_source_cannot_become_business_rule(self):
        params,ref,spans=self.setup_formula()
        with self.assertRaisesRegex(ValueError,'原文明确公式'):
            formula_source.bind('base + minutes * price',params,ref,spans,{'span1'})
        with self.assertRaisesRegex(ValueError,'已发送'):
            formula_source.bind('base + max(0, minutes - included) * price',params,ref,spans,set())
    def test_wrong_parameter_and_result_units_are_rejected(self):
        params,ref,spans=self.setup_formula();expression='base + max(0, minutes - included) * price'
        params[-1]['unit']='ratio'
        with self.assertRaisesRegex(ValueError,'单位不一致'):formula_source.bind(expression,params,ref,spans,{'span1'})
        params[-1]['unit']='CNY/min';ref['result_unit']='min'
        with self.assertRaisesRegex(ValueError,'结果单位'):formula_source.bind(expression,params,ref,spans,{'span1'})

    def test_chinese_literal_equation_binds_database_units_and_rate_quote(self):
        text='基础现场服务费为300元；可计费工时超过60分钟的部分每分钟收取2元。服务费=max(0, 可计费分钟-60)×2+300。minutes 的单位为分钟。'
        path=s.UPLOADS/(uuid.uuid4().hex+'.md');path.write_text(text);d=s.index_document(path)
        located=documents.resolve({'document_id':d['id'],'sha256':d['sha256']},text)
        spans={'s':{'text':text,'evidence_id':'doc','locator':located['locator']}}
        numeric={'name':'minutes','value':'90','unit':'min','evidence_ids':['db','doc'],'source_name':'可计费分钟',
                 'unit_source':{'support_span_id':'s','source_quote':'minutes 的单位为分钟'}}
        facts=[{'value':'90','unit':'untyped','unit_origin':'main.labor.minutes','evidence_ids':['db'],'query_id':'q1'}]
        bound=formula_source.database_parameter(numeric,facts,spans,{'s'})
        self.assertEqual(bound['unit'],'min')
        params=[{**bound,'name':'minutes','source_name':'可计费分钟'}]
        for name,quote,value,unit in [('included','60分钟','60','min'),('price','每分钟收取2元','2','CNY/min'),('base','300元','300','CNY')]:
            param=rag_grounding.document_parameter({'value':value,'unit':unit,'source_quote':quote,'support_span_id':'s','evidence_ids':['doc']},spans,{'s'})
            params.append({**param,'name':name})
        expression='max(0, minutes - included) * price + base'
        ref={'support_span_id':'s','source_quote':'服务费=max(0, 可计费分钟-60)×2+300。','result_unit':'CNY'}
        self.assertEqual(formula_source.bind(expression,params,ref,spans,{'s'})['unit_check'],'passed')
        self.assertEqual(engine.calculate(expression,{p['name']:p['value'] for p in params}),Decimal('360'))
        with self.assertRaisesRegex(ValueError,'同字段'):
            formula_source.database_parameter(numeric,[{**facts[0],'unit_origin':'main.labor.some_count'}],spans,{'s'})


if __name__=='__main__':unittest.main()
