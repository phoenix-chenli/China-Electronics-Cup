"""Real HTTP orchestration contracts, using a scripted model, never quality claims."""
import json
import time
import uuid
import unittest
from urllib.request import Request,urlopen
from unittest.mock import patch
from tests import test_agent as legacy
from backend import rag_grounding as grounding, rag_index, rag_providers
from backend.agent import MAX_CONTEXT_CHARS


def required(payload):
    return {'requirements':[{'text':'甲产品售后适用条件','independent':False}]}


def assessment(payload):
    value=json.loads(payload['messages'][-1]['content'])
    spans=value['evidence']['spans']
    return {'coverage':[{'requirement_id':r['id'],'status':'supported' if spans else 'missing',
        'support_span_ids':[spans[0]['id']] if spans else [],'fact_ids':[],'reason':'指定原文提供适用条件' if spans else '未找到原文'}
        for r in value['requirements']]}


def answer(payload):
    source=legacy.tool_results(payload)[-1]['result']['evidence'][0]
    return {'action':'answer','claims':[{'kind':'synthesis','text':'甲产品的售后处理要求申请人提交发票。',
        'support_span_ids':source['support_span_ids']}]}


def support(payload):
    value=json.loads(payload['messages'][-1]['content'])
    return {'claims':[{'claim_id':x['id'],'status':'supported','detail':'该条引用直接支持申请要求'} for x in value['claims']],
        'coverage':[{'requirement_id':r['id'],'status':'supported','claim_ids':[value['claims'][0]['id']],'reason':'回答了指定条件'}
                    for r in value['requirements']]}


class RagAgentTest(unittest.TestCase):
    setUpClass=classmethod(legacy.AgentTests.setUpClass.__func__)
    tearDownClass=classmethod(legacy.AgentTests.tearDownClass.__func__)
    setUp=legacy.AgentTests.setUp
    tearDown=legacy.AgentTests.tearDown
    configure=legacy.AgentTests.configure
    ready=legacy.AgentTests.ready
    query=legacy.AgentTests.query
    script=legacy.AgentTests.script
    wait_task=legacy.AgentTests.wait_task
    assert_trace=legacy.AgentTests.assert_trace

    def source(self,text='甲产品售后处理要求申请人提交发票。赔付上限为100万元，特殊型号除外。'):
        path=legacy.s.UPLOADS/(uuid.uuid4().hex+'.md')
        path.write_text(text,encoding='utf-8')
        return legacy.s.index_document(path),path

    def setup_rag(self):
        connection=self.ready()
        response=self.client.request('PUT','/api/agent/config',json={'mode':'model','connection_id':connection['id'],'query_scope':'knowledge'})
        self.assertEqual(response.status_code,200,response.text)

    def ask(self,doc,**extra):
        return self.query('甲产品售后处理有哪些要求？',rag_scope={'scope_type':'document','document_ids':[doc['id']]},**extra)

    def test_synthesis_has_real_spans_review_and_timed_stages(self):
        doc,_=self.source();self.setup_rag()
        self.script({'action':'tool','tool':'document.search','arguments':{'query':'甲产品售后发票'}},required,assessment,answer,assessment,support)
        response=self.ask(doc);self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['answer_status'],'supported')
        self.assertEqual(result['claims'][0]['kind'],'synthesis')
        self.assertEqual(result['claims'][0]['semantic_check']['method'],'model_review')
        self.assertEqual(result['agent']['calls'],6)
        self.assert_trace(result)
        self.assertTrue(any(e['tool']=='document.bm25' for e in result['events']))
        source=result['evidence'][0]
        self.assertEqual(source['support_span_ids'],[source['support_spans'][0]['id']])
        self.assertEqual(self.client.post('/api/evidence/resolve',json={'locator':source['locator'],'quote':source['snippet']}).json()['status'],'located')

    def test_many_candidates_fit_model_context_without_truncating_evidence(self):
        for index in range(36):self.source('甲产品售后处理要求申请人提交发票。辅助登记渠道'+str(index)+'。')
        self.setup_rag()
        self.script({'action':'tool','tool':'document.search','arguments':{'query':'甲产品售后发票'}},required,assessment,answer,assessment,support)
        request=Request(self.client.url+'/api/query',data=json.dumps({'question':'甲产品售后处理有哪些要求？','rag_scope':{'scope_type':'corpus'}}).encode(),headers={'Content-Type':'application/json'},method='POST')
        with urlopen(request,timeout=30) as response:
            self.assertEqual(response.status,200);result=json.load(response)
        self.assertEqual(result['answer_status'],'supported')
        returned=legacy.tool_results(legacy.ModelFixture.calls[3])[0]['result']
        self.assertLess(len(json.dumps(returned,ensure_ascii=False)),MAX_CONTEXT_CHARS)
        self.assertNotIn('fusion',returned['retrieval'])
        self.assertTrue(result['rag']['retrieval'][0]['fusion'])

    def test_relevant_quote_cannot_replace_missing_conditions(self):
        doc,_=self.source();self.setup_rag()
        def reject(payload):
            body=support(payload)
            body['claims'][0].update(status='unsupported',detail='没有提供问题所需型号')
            body['coverage'][0].update(status='missing',claim_ids=[],reason='缺少型号的适用资料')
            return body
        self.script({'action':'tool','tool':'document.search','arguments':{'query':'售后发票'}},required,assessment,answer,assessment,reject)
        result=self.ask(doc).json()
        self.assertEqual(result['answer_status'],'refused');self.assertEqual(result['claims'],[])
        self.assertTrue(result['missing_requirements']);self.assertTrue(result['evidence'])

    def test_review_transport_or_invalid_json_refuses(self):
        doc,_=self.source();self.setup_rag()
        self.script({'action':'tool','tool':'document.search','arguments':{'query':'售后发票'}},required,assessment,answer,assessment,'invalid-json')
        result=self.ask(doc).json()
        self.assertEqual(result['answer_status'],'refused');self.assertEqual(result['claims'],[])
        self.assertTrue(any(e['tool']=='model.support' and e['status']=='failed' for e in result['events']))

    def test_invented_number_rejected_and_empty_answer_refuses(self):
        doc,_=self.source();self.setup_rag()
        def invent(payload):
            body=answer(payload);body['claims'][0]['text']='甲产品赔付上限为200万元。';return body
        self.script({'action':'tool','tool':'document.search','arguments':{'query':'售后赔付'}},required,assessment,invent,{'action':'answer','claims':[]})
        result=self.ask(doc).json()
        self.assertEqual(result['claims'],[]);self.assertEqual(result['answer_status'],'refused')
        self.assertTrue(any(e['tool']=='answer.verify' and e['status']=='failed' for e in result['events']))

    def test_unknown_span_never_accepted(self):
        doc,_=self.source();self.setup_rag()
        self.script({'action':'tool','tool':'document.search','arguments':{'query':'售后发票'}},required,assessment,
            {'action':'answer','claims':[{'kind':'synthesis','text':'提交发票。','support_span_ids':['invented-span']}]},
            {'action':'answer','claims':[]})
        result=self.ask(doc).json();self.assertEqual(result['claims'],[])

    def test_without_main_model_returns_raw_sources_and_no_synthesis(self):
        doc,_=self.source()
        result=self.ask(doc).json()
        self.assertEqual(result['answer_status'],'refused');self.assertEqual(result['agent']['calls'],0)
        self.assertTrue(result['evidence']);self.assertEqual(result['claims'],[])

    def test_task_scope_freezes_old_version_and_inherits_server_parent(self):
        doc,path=self.source()
        session=self.client.post('/api/projects/default/sessions').json()
        response=self.client.post('/api/tasks',json={'question':'甲产品售后发票要求','session_id':session['id'],'request_id':uuid.uuid4().hex,
            'rag_scope':{'scope_type':'document','document_ids':[doc['id']]}})
        self.assertEqual(response.status_code,200,response.text)
        first=self.wait_task(response.json()['id']);self.assertEqual(first['status'],'completed',first)
        detail=self.client.get('/api/sessions/'+session['id']).json()
        parent=detail['selected_message_id']
        path.write_text('甲产品售后处理不需要发票，赔付上限900万元。',encoding='utf-8')
        legacy.s.index_document(path)
        response=self.client.post('/api/tasks',json={'question':'继续解释发票要求','session_id':session['id'],'request_id':uuid.uuid4().hex,'parent_message_id':parent})
        self.assertEqual(response.status_code,200,response.text)
        second=self.wait_task(response.json()['id']);self.assertEqual(second['status'],'completed',second)
        self.assertEqual(second['result']['query_spec']['rag_scope']['scope_id'],first['result']['query_spec']['rag_scope']['scope_id'])
        self.assertIn('提交发票',second['result']['evidence'][0]['snippet'])
        self.assertNotIn('900万元',second['result']['evidence'][0]['snippet'])

    def test_document_parameters_only_accept_exact_number_unit_and_delivered_span(self):
        doc,_=self.source()
        result=self.ask(doc).json();span=result['evidence'][0]['support_spans'][0]
        base={'support_span_id':span['id'],'source_quote':'100万元','value':'1000000','unit':'CNY','evidence_ids':[span['evidence_id']]}
        accepted=grounding.document_parameter(base,{span['id']:span},{span['id']})
        self.assertEqual(accepted['value'],'1000000')
        for change in [{'value':'100'},{'unit':'ratio'},{'source_quote':'200万元'},{'evidence_ids':['fake']}]:
            with self.assertRaises(ValueError):grounding.document_parameter({**base,**change},{span['id']:span},{span['id']})
        with self.assertRaises(ValueError):grounding.document_parameter(base,{span['id']:span},set())

    def test_final_coverage_cannot_reference_removed_claim(self):
        needed=[{'id':'R1','text':'适用条件','independent':False}]
        claims=[{'id':'C1','text':'无依据改写','kind':'synthesis','support_span_ids':[],'evidence_ids':[]}]
        def judge(*args):return {'claims':[{'claim_id':'C1','status':'unsupported','detail':'不足'}],
            'coverage':[{'requirement_id':'R1','status':'supported','claim_ids':['C1'],'reason':'错误引用'}]}
        with self.assertRaises(ValueError):grounding.review('问题',needed,claims,{}, {},judge)

    def test_partial_only_for_independent_supported_subquestion(self):
        claims=[{'id':'C1','text':'有依据','kind':'database_empty','fact_id':'F1','evidence_ids':['E1']}]
        def judge(*args):return {'claims':[{'claim_id':'C1','status':'supported','detail':'有来源'}],
            'coverage':[{'requirement_id':'R1','status':'supported','claim_ids':['C1'],'reason':'已回答'},
                        {'requirement_id':'R2','status':'missing','claim_ids':[],'reason':'缺少条件'}]}
        for independent,expected in [(False,'refused'),(True,'partial')]:
            needed=[{'id':'R1','text':'条件一','independent':independent},{'id':'R2','text':'条件二','independent':False}]
            result=grounding.review('条件一和二',needed,claims,{}, {'F1':{'id':'F1','text':'有依据'}},judge)
            self.assertEqual(result['answer_status'],expected)

    def test_remote_index_requires_one_use_matching_preview(self):
        doc,_=self.source()
        provider={'local':False,'ready':True,'model':'fixture-embedding','revision':1,'fingerprint':'fixture-fp'}
        with patch.object(rag_providers,'describe',return_value=provider),patch.object(rag_index,'submit_index',return_value={'status':'pending'}) as submit:
            rejected=self.client.post('/api/rag/reindex',json={'document_ids':[doc['id']],'remote_opt_in':True})
            self.assertEqual(rejected.status_code,400);submit.assert_not_called()
            preview=self.client.post('/api/rag/reindex/preview',json={'document_ids':[doc['id']]}).json()
            body={'document_ids':[doc['id']],'remote_opt_in':True,'preview_id':preview['preview_id']}
            self.assertEqual(self.client.post('/api/rag/reindex',json=body).status_code,200)
            self.assertEqual(submit.call_args.kwargs['expected_provider'],'fixture-fp')
            self.assertEqual(self.client.post('/api/rag/reindex',json=body).status_code,400)

    def test_direct_client_manifest_is_not_a_server_scope(self):
        doc,_=self.source()
        response=self.query('甲产品售后发票要求',previous_context={'metric':'document','query_scope':'knowledge','rag_scope':{'scope_type':'selection','documents':[],'scope_id':'spoof'}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotEqual(response.json()['query_spec']['rag_scope']['scope_id'],'spoof')


if __name__=='__main__':unittest.main()
