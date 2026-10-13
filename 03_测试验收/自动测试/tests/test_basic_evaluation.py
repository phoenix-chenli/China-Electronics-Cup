from contextlib import closing
"""Evaluation diagnostics must not silently turn source checks into accuracy."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / '评估比赛基础任务.py'
spec = importlib.util.spec_from_file_location('basic_evaluation', SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class BasicEvaluationTests(unittest.TestCase):
    def fixture(self, folder, expected=12):
        root=Path(folder)
        (root/'source.pdf').write_bytes(b'synthetic-source-hash-fixture')
        (root/'indicators.json').write_text('{}')
        with closing(sqlite3.connect(root/'reference.db')) as db, db:
            db.execute('CREATE TABLE values_table(value REAL)')
            db.execute('INSERT INTO values_table VALUES (12)')
        data={'frozen_before_model_execution':True,
              'source_snapshot':{'original_pdfs':[{'path':'source.pdf','sha256':evaluator.digest(root/'source.pdf')}],
                                 'indicators_path':'indicators.json','indicators_sha256':evaluator.digest(root/'indicators.json')},
              'database':{'path':'reference.db','sha256':evaluator.digest(root/'reference.db')},
              'cases':[{'id':'test-1','reference':{'sql':'SELECT value FROM values_table','expected_rows':[[expected]]}}]}
        path=root/'dataset.json'
        path.write_text(json.dumps(data))
        return path

    def test_frozen_references_execute_without_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.fixture(folder)
            with patch.object(evaluator,'ROOT',Path(folder)),patch.object(evaluator,'DATASET',path), \
                    patch.object(evaluator,'EXPECTED_SHA',evaluator.digest(path)), \
                    patch.object(evaluator.helpers, 'api', side_effect=AssertionError('模型或服务不应被调用')):
                self.assertEqual(evaluator.load()['cases'][0]['reference']['expected_rows'],[[12]])

    def test_incorrect_reference_is_rejected_even_with_valid_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.fixture(folder,expected=99)
            with patch.object(evaluator,'ROOT',Path(folder)),patch.object(evaluator,'DATASET',path), \
                    patch.object(evaluator,'EXPECTED_SHA',evaluator.digest(path)):
                with self.assertRaisesRegex(ValueError,'独立 SQL'):
                    evaluator.load()

    def test_original_tampering_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.fixture(folder)
            (Path(folder)/'source.pdf').write_bytes(b'changed')
            with patch.object(evaluator,'ROOT',Path(folder)),patch.object(evaluator,'DATASET',path), \
                    patch.object(evaluator,'EXPECTED_SHA',evaluator.digest(path)):
                with self.assertRaisesRegex(ValueError,'PDF'):
                    evaluator.load()

    def test_supported_numbers_ignore_raw_evidence_and_failed_claims(self):
        result = {'answer': '原文100元', 'evidence': [{'snippet': '100元'}],
                  'claims': [{'status': 'unsupported', 'text': '100元'},
                             {'status': 'supported', 'text': '金额2.50元，下降-3.1%。'}]}
        self.assertEqual(evaluator.numeric_coverage(result, ['100', '2.5', '-3.1']),
                         [{'value': '100', 'present': False}, {'value': '2.5', 'present': True},
                          {'value': '-3.1', 'present': True}])

    def test_single_table_contract_rejects_extra_joins_and_columns(self):
        case = {'scope': 'database', 'reference': {'sql': 'SELECT value FROM t', 'expected_rows': [[12]]}}
        result = {'answer_status': 'supported', 'rows': [{'value': 12}],
                  'database_query': {'validation': {'tables': ['main.public_observations']}}}
        self.assertTrue(evaluator.basic_contract(case, result, None)['basic_execution_contract_match'])
        result['database_query']['validation']['tables'].append('main.public_regions')
        self.assertFalse(evaluator.basic_contract(case, result, None)['single_source_match'])
        result['database_query']['validation']['tables'] = ['main.public_observations']
        result['rows'][0]['unit'] = '元'
        self.assertFalse(evaluator.basic_contract(case, result, None)['strict_sql_rows_match'])

    def test_explicit_column_order_is_required_in_addition_to_values(self):
        case={'scope':'database','reference':{'sql':'SELECT value FROM t','expected_rows':[[12]],'expected_columns':['金额_元']}}
        result={'answer_status':'supported','rows':[{'金额_元':12}],'columns':['金额_元'],
                'database_query':{'validation':{'tables':['main.public_observations']}}}
        self.assertTrue(evaluator.basic_contract(case,result,None)['basic_execution_contract_match'])
        result['columns']=['value']
        self.assertTrue(evaluator.basic_contract(case,result,None)['strict_sql_rows_match'])
        self.assertFalse(evaluator.basic_contract(case,result,None)['basic_execution_contract_match'])

    def test_failed_request_never_gets_a_pass_from_preserved_numbers(self):
        case = {'scope': 'database'}
        result = {'answer_status': 'supported', 'rows': [{'value': 12}]}
        self.assertFalse(evaluator.basic_contract(case, result, 'synthetic failure')['basic_execution_contract_match'])

    def test_completed_first_run_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            first=Path(folder)/'first.json';first.write_text('{}')
            with patch.object(evaluator,'load',return_value={}),patch.object(evaluator,'FIRST',first), \
                    patch.object(evaluator.helpers, 'api', side_effect=AssertionError('不得发送请求')):
                with self.assertRaisesRegex(ValueError, '首测仅允许一次'):
                    evaluator.execute(first)

    def test_existing_regression_output_prevents_any_api_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'existing.json'
            path.write_text('{}')
            with patch.object(evaluator,'load',return_value={}),patch.object(evaluator,'FIRST',path), \
                    patch.object(evaluator.helpers, 'api', side_effect=AssertionError('不得发送请求')):
                output=Path(folder)/'existing-regression.json';output.write_text('{}')
                with self.assertRaises(FileExistsError):
                    evaluator.execute(output, regression=True)


if __name__ == '__main__':
    unittest.main()
