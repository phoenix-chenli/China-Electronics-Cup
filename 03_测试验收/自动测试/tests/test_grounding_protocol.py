import copy
import unittest
from backend import rag_grounding as g


class GroundingProtocolTests(unittest.TestCase):
    def setUp(self):
        self.needed=[{'id':'R1','text':'金额及增速','independent':True,'answer_kind':'fact_value'}]
        self.spans={
            'a':{'id':'a','evidence_id':'ea','source':'报告.pdf','text':'年末余额100亿元，增长\n— 4 —',
                 'locator':{'document_id':'doc','sha256':'v','parse_id':'p','page':4,'text_range':{'start':20}},
                 'quality':{'source_methods':['pdf_native'],'derived_view':False}},
            'b':{'id':'b','evidence_id':'eb','source':'报告.pdf','text':'8%；其中居民存款50亿元。',
                 'locator':{'document_id':'doc','sha256':'v','parse_id':'p','page':5,'text_range':{'start':0}},
                 'quality':{'source_methods':['pdf_native'],'derived_view':False}}}
        self.claims=[{'id':'claim-1','kind':'excerpt','text':'年末余额100亿元，增长','support_span_ids':['a']}]

    def test_reading_view_preserves_separate_originals_and_protects_next_page(self):
        spans=copy.deepcopy(self.spans)
        spans.update({str(i):{'id':str(i),'text':'其他内容','locator':{'document_id':'other','sha256':'v','parse_id':'p'}} for i in range(10)})
        selected=g.select_span_ids(spans,['a']+[str(i) for i in range(10)]+['b'],limit=4)
        self.assertIn('b',selected)
        self.assertIn('0',selected)
        pairs=g.page_continuation_candidates(list(self.spans.values()))
        self.assertIn('增长 8%',pairs[0]['reading_excerpt'])
        self.assertEqual(self.spans['a']['text'],'年末余额100亿元，增长\n— 4 —')
        table_view=copy.deepcopy(self.spans)
        table_view['a']['text']+='\n\n原生 PDF 表格 · 第1行\n其他投资 | 5.2'
        self.assertIn('增长 8%',g.page_continuation_candidates(list(table_view.values()))[0]['reading_excerpt'])
        changed=copy.deepcopy(self.spans);changed['a']['text']='总额100亿元。'
        self.assertEqual(g.page_continuation_candidates(list(changed.values())),[])

    def test_invalid_coverage_reference_can_be_repaired_once_without_waiving_gap(self):
        calls=[]
        def judge(tool,label,system,payload):
            calls.append(tool)
            return {'claims':[{'claim_id':'claim-1','status':'supported','detail':'忠实摘录'}],
                    'coverage':[{'requirement_id':'R1','status':'missing','claim_ids':['a'] if len(calls)==1 else [],'reason':'增速未包含在结论'}]}
        result=g.review('金额与增速',self.needed,self.claims,self.spans,{},judge)
        self.assertEqual(calls,['model.support','model.support.repair'])
        self.assertEqual(result['answer_status'],'refused')
        self.assertEqual(result['protocol_repair']['attempts'],1)

    def test_second_invalid_response_stops_and_transport_error_is_not_replayed(self):
        calls=[]
        def bad(*args):
            calls.append(1);return {'claims':[],'coverage':[]}
        with self.assertRaises(ValueError):g.review('金额与增速',self.needed,self.claims,self.spans,{},bad)
        self.assertEqual(len(calls),2)
        calls.clear()
        def network(*args):
            calls.append(1);raise ValueError('HTTP 429')
        with self.assertRaises(ValueError):g.review('金额与增速',self.needed,self.claims,self.spans,{},network)
        self.assertEqual(len(calls),1)

    def test_assessor_repair_cannot_accept_unknown_source(self):
        calls=[]
        def judge(*args):
            calls.append(1)
            return {'coverage':[{'requirement_id':'R1','status':'supported','support_span_ids':['invented'],'fact_ids':[],'reason':'无有效来源'}]}
        with self.assertRaises(ValueError):g.assess('金额与增速',self.needed,g.packet(self.spans),judge)
        self.assertEqual(len(calls),2)

    def test_missing_coverage_may_explain_rejected_claim_but_supported_cannot_use_it(self):
        all_claims={'claim-1':self.claims[0]}
        item={'requirement_id':'R1','status':'missing','claim_ids':['claim-1'],'reason':'只有金额，未给出增速'}
        out=g._coverage([item],{'R1':self.needed[0]},self.spans,{},final=True,
                        accepted_claims={},reviewed_claims=all_claims)
        self.assertEqual(out[0]['status'],'missing')
        with self.assertRaisesRegex(ValueError,'未通过'):
            g._coverage([{**item,'status':'supported'}],{'R1':self.needed[0]},self.spans,{},final=True,
                        accepted_claims={},reviewed_claims=all_claims)

    def test_negative_rate_can_be_read_as_decline_magnitude_without_approving_units(self):
        facts={'f':{'id':'f','kind':'database_value','value':-3.1,'evidence_ids':['query-evidence']}}
        claim=g.synthesis({'text':'该量下降3.1%。','support_fact_ids':['f']},{},set(),facts,question='该量的变化')
        self.assertEqual(claim['literal_sign_bindings'][0]['source_value'],'-3.1')
        self.assertIn('semantic_review',claim['literal_sign_bindings'][0]['status'])
        for text in ['该量增长3.1%。','该量下降31%。']:
            with self.assertRaises(ValueError):g.synthesis({'text':text,'support_fact_ids':['f']},{},set(),facts,question='该量的变化')

    def test_registered_numeric_identifier_does_not_supply_an_extra_measurement(self):
        facts={'f':{'id':'f','kind':'database_value','value':31,'evidence_ids':['query-evidence'],
            'query_provenance':{'source_label_registry':[{'labels':{'metric_name':'细颗粒物PM2.5浓度'}}]}}}
        claim=g.synthesis({'text':'PM2.5浓度为31微克/立方米。','support_fact_ids':['f']},{},set(),facts)
        self.assertEqual(claim['question_scope_mentions'][0]['kind'],'source_registered_identifier')
        for text in ['PM3.0浓度为31微克/立方米。','PM2.5浓度为2.5微克/立方米。']:
            with self.assertRaises(ValueError):g.synthesis({'text':text,'support_fact_ids':['f']},{},set(),facts)

    def test_original_source_coverage_repairs_missing_document_claim_reference(self):
        needed=[{**self.needed[0],'answer_kind':'original_source_check'}]
        claims=[{'id':'claim-1','kind':'database_row','fact_id':'f','text':'金额100','support_span_ids':[]},
                {'id':'claim-2','kind':'excerpt','text':self.spans['a']['text'],'support_span_ids':['a']}]
        calls=[]
        def judge(tool,label,system,payload):
            calls.append(tool)
            return {'claims':[{'claim_id':c['id'],'status':'supported','detail':'对应来源'} for c in claims],
                    'coverage':[{'requirement_id':'R1','status':'supported','claim_ids':['claim-1'] if len(calls)==1 else ['claim-2'],'reason':'原句核对'}]}
        result=g.review('核对金额原句',needed,claims,self.spans,{'f':{'id':'f','value':100}},judge)
        self.assertEqual(calls,['model.support','model.support.repair'])
        self.assertEqual(result['coverage'][0]['claim_ids'],['claim-2'])


if __name__=='__main__':unittest.main()
