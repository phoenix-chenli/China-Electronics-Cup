"""Coverage contracts and real HTTP orchestration with a scripted model."""
import copy
import json
import unittest
from unittest.mock import patch
from tests import test_agent as legacy
from tests import test_rag_agent as rag_tests
from backend import rag_completeness as complete, rag_grounding as grounding

QUESTION='甲产品提交什么材料？赔付有哪些例外？'
PLAN=[{'text':'提交材料','independent':True,'question_part_ids':['Q1']},
      {'text':'赔付例外','independent':True,'question_part_ids':['Q2']}]


class CompletenessContracts(unittest.TestCase):
    def test_explicit_clauses_keep_quoted_delimiters_and_premises(self):
        question='仅按2024年条款；给出“什么；条件？”的原句？注明例外。'
        parts=complete.question_parts(question)
        self.assertEqual([p['text'] for p in parts],['仅按2024年条款；给出“什么；条件？”的原句？','注明例外。'])
        self.assertEqual(''.join(p['text'] for p in parts),question)

    def test_planning_requires_every_question_part(self):
        parts=complete.question_parts(QUESTION)
        for bad in [[PLAN[0]],[{**PLAN[0],'question_part_ids':['Q3']},PLAN[1]],
                    [{**PLAN[0],'question_part_ids':[]},PLAN[1]],
                    [{**PLAN[0],'question_part_ids':['Q1','Q1']},PLAN[1]],
                    [PLAN[0],{**PLAN[1],'text':PLAN[0]['text']}]]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                complete.validate_plan(bad,parts,grounding.ANSWER_KIND_CONTRACT)

    def test_english_contraction_does_not_hide_following_question(self):
        self.assertEqual(len(complete.question_parts("What's required? What exceptions apply?")),2)

    def test_over_budget_questions_are_not_silently_dropped(self):
        parts=complete.question_parts(''.join('给出第'+str(i)+'项？' for i in range(9)))
        with self.assertRaisesRegex(ValueError,'超过8项'):
            complete.validate_plan(PLAN,parts,grounding.ANSWER_KIND_CONTRACT)

    def test_mapping_repairs_once_with_unchanged_question(self):
        calls=[]
        def judge(tool,label,system,payload):
            calls.append(tool)
            self.assertEqual(payload['question'],QUESTION)
            return {'requirements':[PLAN[0]] if len(calls)==1 else PLAN}
        self.assertEqual(len(grounding.requirements(QUESTION,judge)),2)
        self.assertEqual(calls,['model.requirements','model.requirements.repair'])

    def test_repeated_incomplete_mapping_is_visible_failure(self):
        calls=[]
        def judge(*args):calls.append(args[0]);return {'requirements':[PLAN[0]]}
        with self.assertRaisesRegex(ValueError,'遗漏'):
            grounding.requirements(QUESTION,judge)
        self.assertEqual(len(calls),2)

    def test_plan_transport_failure_is_not_relabelled_as_missing_evidence(self):
        with self.assertRaisesRegex(ValueError,'HTTP 503'):
            grounding.requirements(QUESTION,lambda *args:(_ for _ in ()).throw(ValueError('HTTP 503')))

    def data(self):
        needed=complete.validate_plan(PLAN,complete.question_parts(QUESTION),grounding.ANSWER_KIND_CONTRACT)
        assessment={'coverage':[{'requirement_id':'R1','status':'supported','support_span_ids':['s1']},
                                {'requirement_id':'R2','status':'supported','support_span_ids':['s2']}]}
        verification={'claims':[{'id':'c1','semantic_check':{'status':'supported'}}],
                      'coverage':[{'requirement_id':'R1','status':'supported','claim_ids':['c1']},
                                  {'requirement_id':'R2','status':'missing','claim_ids':[]}]}
        return needed,assessment,verification

    def test_one_cited_claim_is_half_question_coverage(self):
        needed,assessment,verification=self.data()
        value=complete.ledger(QUESTION,needed,assessment,verification)
        self.assertEqual((value['answered'],value['total'],value['percent'],value['complete']),(1,2,50,False))
        self.assertEqual([p['status'] for p in value['question_parts']],['answered','unanswered'])

    def test_removed_or_unchecked_claim_does_not_count_as_answered(self):
        needed,assessment,verification=self.data()
        for claims in [[],[{'id':'c1'}],[{'id':'c1','semantic_check':{'status':'unknown'}}]]:
            with self.subTest(claims=claims):
                self.assertEqual(complete.ledger(QUESTION,needed,assessment,{**verification,'claims':claims})['answered'],0)

    def test_supported_but_omitted_answer_uses_existing_evidence(self):
        coverage=complete.ledger(QUESTION,*self.data())
        result=complete.recovery(coverage,[{'status':'supported'}],searches=1,calls_left=4,tools_left=0)
        self.assertEqual(result['action'],'rewrite')
        self.assertEqual(result['requirement_ids'],['R2'])

    def test_unsupported_claim_does_not_trigger_generation_retry(self):
        coverage=complete.ledger(QUESTION,*self.data())
        self.assertEqual(complete.recovery(coverage,[{'status':'unsupported'}],searches=1,calls_left=9,tools_left=4)['action'],'stop')

    def test_source_gap_uses_only_remaining_search_budget(self):
        needed,assessment,verification=self.data()
        assessment['coverage'][1]['status']='missing'
        coverage=complete.ledger(QUESTION,needed,assessment,verification)
        args=dict(searches=1,calls_left=5,tools_left=1)
        self.assertEqual(complete.recovery(coverage,[{'status':'supported'}],**args)['action'],'retrieve')
        for changes in [{'searches':2},{'calls_left':4},{'tools_left':0},{'attempted':True}]:
            self.assertEqual(complete.recovery(coverage,[],**{**args,**changes})['action'],'stop')

    def test_conflicts_never_become_complete_by_retrying(self):
        needed,assessment,verification=self.data()
        assessment['coverage'][1]['status']='conflicted'
        coverage=complete.ledger(QUESTION,needed,assessment,verification)
        self.assertEqual(complete.recovery(coverage,[],searches=0,calls_left=12,tools_left=8)['action'],'stop')

    def test_focused_query_keeps_full_question_and_refuses_truncation(self):
        needed,_,_=self.data()
        query,ids=complete.focused_query(QUESTION,needed,['R2'])
        self.assertTrue(query.startswith(QUESTION+'\n'));self.assertEqual(ids,['R2'])
        self.assertEqual(complete.focused_query('问'*2000,needed,['R2']),(None,[]))

    def test_coverage_packing_reserves_each_supported_goal_and_required_citation(self):
        spans={sid:{'locator':{'document_id':'one'}} for sid in ['required','oldA','oldB']+['new'+str(i) for i in range(10)]}
        selected=grounding.select_span_ids(spans,['new'+str(i) for i in range(10)],['required'],
            coverage_groups=[['oldA'],['oldB']])
        self.assertEqual(len(selected),8)
        self.assertTrue({'required','oldA','oldB','new0'}<=set(selected))

    def test_source_gap_history_is_preserved_without_duplicates(self):
        gap={'page':1,'reason':'未识别'};next_gap={'page':2,'reason':'表头待复核'}
        self.assertEqual(complete.source_gaps([gap],[gap,next_gap]),[gap,next_gap])

    def run_for_recovery(self):
        from backend.agent import Run
        needed,assessment,verification=self.data()
        assessment['coverage'][1]['status']='missing'
        coverage=complete.ledger(QUESTION,needed,assessment,verification)
        run=Run.__new__(Run)
        run.result={'question':QUESTION,'rag':{'verification':{'checks':[{'status':'supported'}]}},'agent':{}}
        run.update_completeness=lambda:coverage
        run.search_rounds=1;run.model_call_limit=12;run.calls=5
        run.step_limit=8;run.tool_count=1;run.completeness_repairs=0
        run.retained_answer_items=[];run.requirements=needed
        run.guard=lambda:None
        run.step=lambda tool,label,fn,inputs:fn()
        return run

    def test_recovery_transport_failure_is_not_an_answer_format_repair(self):
        from backend.agent import EvidenceJudgmentFailure
        run=self.run_for_recovery()
        run.tool=lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('HTTP 503'))
        with self.assertRaisesRegex(EvidenceJudgmentFailure,'HTTP 503'):run.repair_completeness()
        self.assertEqual(run.result['rag']['completion_repair']['status'],'failed')
        self.assertEqual(run.completeness_repairs,1)
        self.assertFalse(run.repair_completeness())

    def test_cancelled_recovery_does_not_run_another_tool(self):
        run=self.run_for_recovery()
        run.tool=lambda *args,**kwargs:(_ for _ in ()).throw(InterruptedError('cancelled'))
        with self.assertRaises(InterruptedError):run.repair_completeness()
        self.assertEqual(run.result['rag']['completion_repair']['status'],'cancelled')

    def test_query_budget_block_leaves_complete_original_question(self):
        run=self.run_for_recovery();run.result['question']='何时'*1000
        run.tool=lambda *args,**kwargs:self.fail('must not search a clipped question')
        self.assertFalse(run.repair_completeness())
        self.assertEqual(run.result['rag']['completion_repair']['status'],'blocked')
        self.assertEqual(len(run.result['question']),2000)

    def test_malformed_draft_text_remains_a_repairable_protocol_error(self):
        run=self.run_for_recovery();run.rag_enabled=True;run.rag_scope=None
        run.database_mode=False;run.spans={};run.delivered_spans=set();run.facts={}
        for text in [['invalid'],{'invalid':'text'}]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError,'综合陈述'):
                run.answer({'claims':[{'kind':'synthesis','text':text}]})


class CompletenessHTTP(unittest.TestCase):
    setUpClass=classmethod(legacy.AgentTests.setUpClass.__func__)
    tearDownClass=classmethod(legacy.AgentTests.tearDownClass.__func__)
    setUp=legacy.AgentTests.setUp
    tearDown=legacy.AgentTests.tearDown
    configure=legacy.AgentTests.configure
    ready=legacy.AgentTests.ready
    query=legacy.AgentTests.query
    script=legacy.AgentTests.script
    assert_trace=legacy.AgentTests.assert_trace
    source=rag_tests.RagAgentTest.source
    setup_rag=rag_tests.RagAgentTest.setup_rag

    def fixture(self, source_missing=False, final_complete=True, repair_only_second=False, reject=False, empty=False, reject_second=False, excerpt_first=False):
        doc,_=self.source('甲产品售后处理要求申请人提交发票。' if source_missing else '甲产品售后处理要求申请人提交发票。赔付上限为100万元，特殊型号除外。')
        docs=[doc]
        if source_missing:
            extra,_=self.source('甲产品赔付条款的适用范围：特殊型号除外。')
            docs.append(extra)
        self.setup_rag();seen={};rounds={'assess':0,'review':0}
        def assess(payload):
            body=json.loads(payload['messages'][-1]['content']);rounds['assess']+=1
            seen['spans']=[s['id'] for s in body['evidence']['spans']]
            seen['evidence_id']=body['evidence']['spans'][0]['evidence_id']
            return {'coverage':[{'requirement_id':r['id'],
                'status':'missing' if source_missing and rounds['assess']==1 and i==1 else 'supported',
                'support_span_ids':[] if source_missing and rounds['assess']==1 and i==1 else seen['spans'],
                'reason':'未取得例外证据' if source_missing and rounds['assess']==1 and i==1 else '原文提供相关条件'}
                for i,r in enumerate(body['requirements'])]}
        def first_answer(payload):
            if empty and 'checkpoint' not in seen:return {'action':'answer','claims':[]}
            if excerpt_first:return {'action':'answer','claims':[{'text':'甲产品售后处理要求申请人提交发票。','evidence_ids':[seen['evidence_id']]}]}
            return {'action':'answer','claims':[{'kind':'synthesis','text':'甲产品售后处理要求申请人提交发票。',
                'support_span_ids':seen['spans']}]}
        def review(payload):
            body=json.loads(payload['messages'][-1]['content']);rounds['review']+=1
            complete_now=bool(seen.get('checkpoint')) and final_complete and not reject_second
            ids={x['text']:x['id'] for x in body['claims']}
            first=next((cid for text,cid in ids.items() if '发票' in text),None)
            second=next((cid for text,cid in ids.items() if '特殊型号' in text),None)
            return {'claims':[{'claim_id':c['id'],'status':'unsupported' if reject or (reject_second and c['id']==second) else 'supported','detail':'与原文逐项核对'} for c in body['claims']],
                'coverage':[{'requirement_id':'R1','status':'supported' if first and not reject else 'missing','claim_ids':[first] if first and not reject else [],'reason':'材料要求'},
                    {'requirement_id':'R2','status':'supported' if complete_now and second and not reject else 'missing','claim_ids':[second] if complete_now and second and not reject else [],'reason':'例外已覆盖' if complete_now and second else '答案尚未给出例外'}]}
        def repaired(payload):
            body=json.loads(payload['messages'][-1]['content'])
            self.assertEqual(body['original_question'],QUESTION)
            self.assertEqual(bool(body['retained_document_answer_items']),not empty)
            seen['checkpoint']=body
            result=first_answer(payload)
            if repair_only_second:result['claims']=[]
            result['claims'].append({'kind':'synthesis','text':'甲产品赔付条款注明特殊型号除外。','support_span_ids':seen['spans']})
            return result
        steps=[{'action':'tool','tool':'document.search','arguments':{'query':'甲产品售后处理发票特殊型号',**({'document_ids':[doc['id']]} if source_missing else {})}},
            {'requirements':PLAN},assess,first_answer]+([] if empty else [review])
        if not reject:
            # Repeating the same retrieved packet may reuse a cached assessment.
            steps += ([assess] if source_missing else [])+[repaired,review]
        self.script(*steps)
        response=self.query(QUESTION,rag_scope={'scope_type':'corpus' if source_missing else 'document','document_ids':[d['id'] for d in docs]})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assert_trace(result)
        return result,seen

    def test_drafting_omission_is_repaired_without_another_search(self):
        result,seen=self.fixture()
        self.assertEqual(result['answer_status'],'supported')
        self.assertTrue(result['rag']['completeness']['complete'])
        self.assertEqual(result['rag']['completion_repair']['action'],'rewrite')
        self.assertEqual(len([e for e in result['events'] if e['tool']=='document.search']),1)
        self.assertEqual(result['rag']['completion_repair']['before']['answered'],1)
        self.assertEqual(result['rag']['completeness']['answered'],2)
        self.assertEqual(result['agent']['calls'],7)

    def test_missing_sources_get_one_real_focused_search_in_fixed_scope(self):
        result,seen=self.fixture(source_missing=True)
        self.assertEqual(result['answer_status'],'supported')
        searches=[e for e in result['events'] if e['tool']=='document.search']
        self.assertEqual(len(searches),2)
        self.assertTrue(searches[1]['inputs']['query'].startswith(QUESTION))
        self.assertEqual(result['rag']['completion_repair']['action'],'retrieve')
        self.assertTrue(any(p['origin']=='server_completeness_recovery' for p in result['agent']['plan']))

    def test_replacement_draft_cannot_silently_drop_previously_accepted_item(self):
        result,_=self.fixture(repair_only_second=True)
        self.assertEqual(result['answer_status'],'supported')
        self.assertEqual(len(result['claims']),2)
        self.assertIn('发票',result['answer']);self.assertIn('特殊型号',result['answer'])
        self.assertTrue(all(c['semantic_check']['status']=='supported' for c in result['claims']))

    def test_still_incomplete_stops_after_one_cycle_and_keeps_partial_answer(self):
        result,_=self.fixture(final_complete=False)
        self.assertEqual(result['answer_status'],'partial')
        self.assertFalse(result['rag']['completeness']['complete'])
        self.assertEqual(result['rag']['completion_repair']['attempt'],1)
        self.assertEqual(len([e for e in result['events'] if e['tool']=='answer.complete']),1)
        self.assertIn('未完整回答',result['answer'])

    def test_rejected_claim_is_not_treated_as_a_simple_omission(self):
        result,_=self.fixture(reject=True)
        self.assertEqual(result['answer_status'],'refused')
        self.assertNotIn('completion_repair',result['rag'])
        self.assertEqual(result['rag']['completeness']['answered'],0)

    def test_empty_initial_draft_with_sufficient_sources_gets_one_rewrite(self):
        result,_=self.fixture(empty=True)
        self.assertTrue(result['rag']['completeness']['complete'])
        self.assertEqual(result['agent']['calls'],6)
        self.assertNotIn('refusal_reason',result)

    def test_new_repair_claim_is_rechecked_and_not_automatically_promoted(self):
        result,_=self.fixture(reject_second=True)
        self.assertEqual(result['answer_status'],'partial')
        self.assertEqual(result['rag']['completeness']['answered'],1)
        self.assertEqual(len(result['claims']),1)

    def test_legacy_excerpt_binding_is_preserved_and_rechecked_during_repair(self):
        result,_=self.fixture(excerpt_first=True,repair_only_second=True)
        self.assertEqual(result['answer_status'],'supported')
        self.assertEqual(result['rag']['completion_repair']['retained_claims'],1)
        self.assertEqual(len(result['claims']),2)
