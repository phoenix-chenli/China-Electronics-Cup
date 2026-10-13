"""Actual SQL expressions versus adversarial prose; no model requests."""
import unittest
from unittest.mock import Mock
from backend import database_answer as contract

VOCAB={'table_labels':{'Artist':'艺术家','Customer':'客户'}}
class AnswerContractTests(unittest.TestCase):
    def test_followup_filters_use_real_columns_and_do_not_guess_optional_or_parameter_values(self):
        sql="SELECT COUNT(*) FROM Track t JOIN Genre g ON t.GenreId=g.GenreId WHERE g.Name='Rock'"
        prior={'last_sql':sql,'database_connection_id':'public-db'}
        carried=contract.followup_filters('同一流派改按媒体类型统计',prior,'public-db')
        self.assertEqual(carried[0]['column'],'main.genre.name');self.assertEqual(carried[0]['value'],'Rock')
        self.assertEqual(contract.followup_filters('改为Jazz流派',prior,'public-db'),[])
        self.assertEqual(contract.followup_filters('同一流派统计',prior,'other-db'),[])
        self.assertEqual(contract.literal_filters(sql.replace("g.Name='Rock'","g.Name='Rock' OR g.Name='Jazz'")),[])
        self.assertEqual(contract.literal_filters(sql.replace("'Rock'",'?'),['Rock'])[0]['value'],'Rock')
        self.assertEqual(contract.literal_filters(sql.replace("'Rock'",'?')+' AND t.UnitPrice=?',['Rock',.99]),[])

    def test_registered_literals_are_context_and_never_alias_unit_proof(self):
        vocabulary={'indicators':[{'metric_id':'growth','metric_name':'登记增速','unit':'%'},
                                  {'metric_id':'money','metric_name':'登记金额','unit':'万元'}]}
        sql="SELECT value AS 百分比 FROM stats WHERE metric_id='money'"
        labels=contract.source_labels(sql,vocabulary)
        self.assertEqual(labels[0]['labels']['unit'],'万元')
        self.assertEqual(labels[0]['status'],'registry_context_not_value_binding')
        proof=contract.contract(sql,['百分比'],vocabulary)
        self.assertEqual(proof['fields'][0]['kind'],'unverified_measure')
        self.assertIsNone(proof['fields'][0]['unit'])

    def check(self,text,sql='SELECT COUNT(*) AS n FROM Artist',question='艺术家有多少位？',row=None):
        row=row or {'n':275}
        proof=contract.contract(sql,list(row),VOCAB)
        facts={'f1':{'kind':'database_row','row_values':row,'query_provenance':{'answer_contract':proof}}}
        return contract.check({'text':text,'support_fact_ids':['f1']},facts,question)
    def test_simple_count_can_have_a_bounded_natural_answer(self):
        self.assertEqual(self.check('数据库中共有 275 位艺术家。')['status'],'verified_count')
        self.assertEqual(self.check('查询到275条记录。')['status'],'verified_count')
    def test_count_cannot_gain_a_percentage_or_magnitude_unit(self):
        for text in ['艺术家数量为275%。','共有275万位艺术家。','共有275元。','数据库中共有300位艺术家。']:
            self.assertEqual(self.check(text)['status'],'needs_review')
    def test_metric_comparison_and_unknown_semantics_do_not_gain_support(self):
        for text in ['净销售额为275。','艺术家共有275位，同比增加。','有275位艺术家，优于去年。','已查得275，全部优质。']:
            self.assertEqual(self.check(text)['status'],'needs_review')
    def test_alias_cannot_turn_customer_count_into_artist_count(self):
        self.assertEqual(self.check('数据库中共有275位艺术家。','SELECT COUNT(*) AS 艺术家数量 FROM Customer',row={'艺术家数量':275})['status'],'needs_review')
    def test_period_must_be_an_actual_dimension_predicate_not_an_alias_or_id(self):
        for sql in ['SELECT COUNT(*) AS n FROM Artist','SELECT COUNT(*) AS n FROM Artist WHERE ArtistId=2024','SELECT COUNT(*) AS n FROM Artist WHERE year!=2024']:
            self.assertEqual(self.check('共有275条记录。',sql,'2024年有多少艺术家')['status'],'needs_review')
        self.assertEqual(self.check('共有275条记录。','SELECT COUNT(*) AS n FROM Artist WHERE year=2024','2024年有多少艺术家')['status'],'verified_count')
    def test_joins_and_ctes_do_not_inherit_an_unproved_entity_grain(self):
        for sql in ['SELECT COUNT(*) AS n FROM Artist JOIN Album ON Artist.ArtistId=Album.ArtistId','WITH x AS (SELECT * FROM Artist) SELECT COUNT(*) AS n FROM x','SELECT COUNT(*)+1 AS n FROM Artist']:
            self.assertEqual(self.check('共有275位艺术家。',sql)['status'],'needs_review')
    def test_unavailable_contract_and_no_declared_facts_fail_closed(self):
        self.assertEqual(contract.check({'text':'共有275位艺术家。','support_fact_ids':['f1']},{'f1':{'kind':'database_row','row_values':{'n':275}}})['status'],'needs_review')
        self.assertEqual(contract.check({'text':'优于去年','support_fact_ids':[]},{})['status'],'needs_review')

    def test_semantic_review_cannot_waive_count_percentages(self):
        for sql in ('SELECT COUNT(*) AS n FROM Artist','SELECT COUNT(DISTINCT ArtistId) AS n FROM Artist'):
            facts={'f':{'kind':'database_row','row_values':{'n':275},'query_provenance':{'answer_contract':contract.contract(sql,['n'],VOCAB)}}}
            claim={'id':'C1','text':'艺术家数量为275%。','support_fact_ids':['f']}
            claim['database_semantic_check']=contract.check(claim,facts,'艺术家有多少位？')
            judge=Mock()
            self.assertFalse(claim['database_semantic_check']['reviewable'])
            self.assertEqual(contract.review([claim],facts,'艺术家有多少位？',judge)['checks'],[])
            judge.assert_not_called()

    def test_unknown_aggregate_uses_bound_sql_and_strict_review_protocol(self):
        facts={'f':{'kind':'database_row','row_values':{'金额':25.75},'query_provenance':{'sql':'SELECT SUM(amount) AS 金额 FROM settlements',
                     'params':[],'answer_contract':contract.contract('SELECT SUM(amount) AS 金额 FROM settlements',['金额'])}}}
        claim={'id':'C1','text':'查询返回的金额合计数值为25.75，单位未确认。','support_fact_ids':['f']}
        claim['database_semantic_check']=contract.check(claim,facts,'金额合计是多少？')
        judge=Mock(return_value={'checks':[{'claim_id':'C1','status':'supported','detail':'同次SUM结果25.75，未虚构单位。'}],
                                'coverage_status':'supported','missing_requirements':[]})
        reviewed=contract.review([claim],facts,'金额合计是多少？',judge)
        self.assertEqual(reviewed['checks'][0]['status'],'supported')
        self.assertEqual(judge.call_args.args[3]['facts']['f']['query_provenance']['sql'],facts['f']['query_provenance']['sql'])
        for invalid in ({'checks':[],'coverage_status':'supported','missing_requirements':[]},
                        {'checks':[{'claim_id':'other','status':'supported','detail':'匹配'}],'coverage_status':'supported','missing_requirements':[]},
                        {'checks':[{'claim_id':'C1','status':'supported','detail':'匹配'}],'coverage_status':'supported','missing_requirements':['未核验']}):
            judge.return_value=invalid
            with self.assertRaises(ValueError):contract.review([claim],facts,'金额合计是多少？',judge)

    def test_separate_sql_percentage_can_be_reviewed_but_count_cannot_supply_it(self):
        sql='SELECT COUNT(*) AS n, COUNT(*)*100.0/(SELECT COUNT(*) FROM Customer) AS ratio FROM Artist'
        proof=contract.contract(sql,['n','ratio'],VOCAB)
        facts={'f':{'kind':'database_row','row_values':{'n':3,'ratio':50.0},'query_provenance':{'answer_contract':proof}}}
        self.assertTrue(contract.check({'text':'查询数量为3，比例为50%。','support_fact_ids':['f']},facts)['reviewable'])
        self.assertFalse(contract.check({'text':'查询数量为3，比例为3%。','support_fact_ids':['f']},facts)['reviewable'])

    def test_conditional_years_are_context_not_a_single_period_filter(self):
        sql="SELECT SUM(CASE WHEN strftime('%Y',invoice_date)='2020' THEN amount ELSE 0 END) AS a,SUM(CASE WHEN strftime('%Y',invoice_date)='2021' THEN amount ELSE 0 END) AS b FROM invoices"
        proof=contract.contract(sql,['a','b'])
        self.assertEqual(proof['period_years'],[])
        self.assertEqual(set(proof['context_year_labels']),{'2020','2021'})
        proof=contract.contract("SELECT COUNT(*) AS n FROM Artist WHERE year=2024 OR year=2023",['n'],VOCAB)
        self.assertEqual(proof['period_years'],[])
        self.assertEqual(set(proof['context_year_labels']),{'2024','2023'})
