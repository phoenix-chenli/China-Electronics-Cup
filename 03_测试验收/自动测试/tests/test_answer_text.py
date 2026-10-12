"""How a database answer is worded, and what basis a sentence must declare."""
import os
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-answer-text-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import agent, rag_grounding as grounding


class RowFactTextTest(unittest.TestCase):
    def test_a_single_row_reads_as_a_plain_statement(self):
        self.assertEqual('artist_count 为 275。',
                         agent.row_fact_text(0, {'artist_count':275}, ['artist_count'], single=True))

    def test_a_single_row_with_several_columns_joins_the_pairs(self):
        self.assertEqual('total 为 2328.6；average 为 5.65。',
                         agent.row_fact_text(0, {'total':2328.6,'average':5.65}, ['total','average'], single=True))

    def test_several_rows_keep_the_row_number_that_makes_a_fact_traceable(self):
        text=agent.row_fact_text(1, {'genre_name':'Latin','track_count':579}, ['genre_name','track_count'])
        # Text values stay quoted so a literal "0" is never read as the number 0.
        self.assertEqual('返回结果第 2 行：genre_name = "Latin"；track_count = 579。', text)

    def test_the_displayed_precision_is_used_when_it_differs_from_the_raw_value(self):
        self.assertEqual('amount 为 1716.00。',
                         agent.row_fact_text(0, {'amount':1716.0}, ['amount'], {'amount':'1716.00'}, single=True))

    def test_a_null_and_a_zero_are_not_confused(self):
        self.assertEqual('note 为 null。', agent.row_fact_text(0, {'note':None}, ['note'], single=True))
        self.assertEqual('note 为 0。', agent.row_fact_text(0, {'note':0}, ['note'], single=True))


class DatabaseSynthesisTest(unittest.TestCase):
    def facts(self):
        return {
            'q1-row-1': {'id':'q1-row-1','kind':'database_row','text':'row','row_values':{'artist_count':275},
                         'query_id':'q1','evidence_ids':['sql-e1']},
            'q1-row-2': {'id':'q1-row-2','kind':'database_row','text':'row','row_values':{'artist_count':999},
                         'query_id':'q1','evidence_ids':['sql-e2']},
        }

    def test_a_sentence_that_declares_its_facts_is_accepted(self):
        claim=grounding.synthesis({'kind':'synthesis','text':'数据库中共有 275 位艺术家。','support_fact_ids':['q1-row-1']},
                                  {},{},{},available_facts=self.facts())
        self.assertEqual(['q1-row-1'],claim['support_fact_ids'])
        self.assertEqual(['sql-e1'],claim['evidence_ids'])
        self.assertEqual('pending_review',claim['status'])

    def test_an_invented_number_is_refused(self):
        with self.assertRaisesRegex(ValueError,'原文未提供的数值'):
            grounding.synthesis({'kind':'synthesis','text':'数据库中共有 300 位艺术家。','support_fact_ids':['q1-row-1']},
                                {},{},{},available_facts=self.facts())

    def test_declaring_facts_narrows_the_gate_instead_of_widening_it(self):
        item={'kind':'synthesis','text':'数据库中共有 999 位艺术家。','support_fact_ids':['q1-row-1']}
        with self.assertRaisesRegex(ValueError,'原文未提供的数值'):
            grounding.synthesis(item,{},{},self.facts(),available_facts=self.facts())
        claim=grounding.synthesis({**item,'support_fact_ids':['q1-row-2']},{},{},self.facts(),available_facts=self.facts())
        self.assertEqual(['q1-row-2'],claim['support_fact_ids'])

    def test_an_unknown_or_malformed_fact_reference_is_refused(self):
        with self.assertRaisesRegex(ValueError,'不存在的数据库事实'):
            grounding.synthesis({'kind':'synthesis','text':'共有 275 位。','support_fact_ids':['q9-row-9']},
                                {},{},{},available_facts=self.facts())
        with self.assertRaisesRegex(ValueError,'格式不正确'):
            grounding.synthesis({'kind':'synthesis','text':'共有 275 位。','support_fact_ids':[275]},
                                {},{},{},available_facts=self.facts())

    def test_a_sentence_with_neither_span_nor_fact_is_refused(self):
        with self.assertRaisesRegex(ValueError,'必须有文档原文位置或数据库事实'):
            grounding.synthesis({'kind':'synthesis','text':'数据库中共有 275 位艺术家。'},{},set(),{})

    def test_the_document_path_is_unchanged(self):
        span={'id':'s1','evidence_id':'e1','source':'说明.pdf','text':'限额为100元。','locator':{'page':1}}
        with patch.object(grounding.documents,'resolve',return_value={'status':'located'}):
            claim=grounding.synthesis({'kind':'synthesis','text':'限额为100元。','support_span_ids':['s1']},{'s1':span},{'s1'})
            self.assertEqual(['s1'],claim['support_span_ids'])
            self.assertEqual([],claim['support_fact_ids'])
            # A span that was not delivered is still refused.
            with self.assertRaises(ValueError):
                grounding.synthesis({'kind':'synthesis','text':'限额为100元。','support_span_ids':['s1']},{},{'s1'})

    def test_a_fact_bound_sentence_counts_as_supported_without_claiming_review(self):
        claims=[{'kind':'synthesis','status':'pending_review','evidence_ids':['sql-e1'],'text':'有依据'},
                {'kind':'synthesis','status':'pending_review','evidence_ids':[],'text':'无依据'},
                {'kind':'excerpt','status':'supported','evidence_ids':['sql-e1'],'text':'摘录'}]
        agent.bind_database_synthesis(claims)
        self.assertEqual(['supported','pending_review','supported'],[c['status'] for c in claims])


if __name__=='__main__':
    unittest.main()
