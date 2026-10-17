"""Large catalogs must not consume the planner's entire context budget."""
import copy
import json
import unittest
from unittest.mock import patch
from tests import test_agent as legacy
from tests import test_rag_agent as rag
from backend.agent import _rag_scope_preview,MAX_CONTEXT_CHARS
from backend import store as s


class LargeCorpusAgentHTTP(unittest.TestCase):
    setUpClass=classmethod(legacy.AgentTests.setUpClass.__func__)
    tearDownClass=classmethod(legacy.AgentTests.tearDownClass.__func__)
    setUp=legacy.AgentTests.setUp
    tearDown=legacy.AgentTests.tearDown
    configure=legacy.AgentTests.configure
    ready=legacy.AgentTests.ready
    query=legacy.AgentTests.query
    script=legacy.AgentTests.script
    source=rag.RagAgentTest.source
    setup_rag=rag.RagAgentTest.setup_rag
    assert_trace=legacy.AgentTests.assert_trace

    def test_two_hundred_documents_remain_searchable_beyond_prompt_preview(self):
        entries=[]
        for i in range(200):
            doc,_=self.source(f'entry{i:04d}。甲产品售后处理要求申请人提交发票。')
            entries.append((doc,i))
        target,number=sorted(entries,key=lambda pair:pair[0]['id'])[-1]
        self.setup_rag()
        def start(payload):
            view=json.loads(payload['messages'][1]['content'])
            self.assertEqual(view['rag_scope']['document_count'],200)
            self.assertTrue(view['rag_scope']['catalog_preview_only'])
            self.assertEqual(len(view['document_catalog']),8)
            self.assertNotIn(target['id'],[d['document_id'] for d in view['rag_scope']['documents']])
            self.assertLess(sum(len(m['content']) for m in payload['messages']),MAX_CONTEXT_CHARS)
            return {'action':'tool','tool':'document.search','arguments':{'query':f'entry{number:04d} 售后 发票'}}
        def answer(payload):
            sources=legacy.tool_results(payload)[-1]['result']['evidence']
            source=next(e for e in sources if e['document_id']==target['id'])
            return {'action':'answer','claims':[{'kind':'synthesis','text':'甲产品售后处理要求申请人提交发票。','support_span_ids':source['support_span_ids']}]}
        self.script(start,rag.required,rag.assessment,answer,rag.support)
        response=self.query(f'entry{number:04d}中的甲产品售后处理要求是什么？',rag_scope={'scope_type':'corpus','document_ids':[d['id'] for d,_ in entries]})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertEqual(result['answer_status'],'supported');self.assert_trace(result)
        self.assertEqual(len(result['rag']['scope']['documents']),200)
        self.assertEqual(result['agent']['scope_preview']['preview_count'],8)
        self.assertTrue(result['agent']['scope_preview']['server_scope_unchanged'])
        self.assertEqual(result['agent']['calls'],5)
        self.assertEqual(result['rag']['retrieval'][0]['candidate_count'],200)
        self.assertTrue(any(e['locator']['document_id']==target['id'] for e in result['evidence']))


class ScopePreviewContracts(unittest.TestCase):
    def test_thousand_source_metadata_is_bounded_without_mutating_scope(self):
        scope={'scope_type':'corpus','documents':[{'document_id':str(i),'sha256':'a'*64,'parse_id':'p'*32,'generation_id':'g'*64} for i in range(1000)]}
        original=copy.deepcopy(scope)
        with patch.object(s,'doc_get',side_effect=lambda did:{'name':did+'.pdf'}) as names:
            view,catalog=_rag_scope_preview(scope)
        self.assertEqual(names.call_count,8);self.assertEqual(len(catalog),8)
        self.assertEqual(view['document_count'],1000);self.assertTrue(view['catalog_preview_only'])
        self.assertLess(len(json.dumps([view,catalog])),6000)
        view['documents'][0]['parse_id']='changed preview'
        self.assertEqual(scope,original)

    def test_small_scope_keeps_its_whole_catalog(self):
        source={'document_id':'d','sha256':'a'*64,'parse_id':'p'}
        with patch.object(s,'doc_get',return_value={'name':'sample.pdf'}):view,catalog=_rag_scope_preview({'scope_type':'document','documents':[source]})
        self.assertFalse(view['catalog_preview_only']);self.assertEqual(view['documents'],[source])
        self.assertEqual(catalog[0]['sha256'],source['sha256'])

    def test_non_document_query_has_no_synthetic_scope(self):
        self.assertEqual(_rag_scope_preview(None),({},[]))
