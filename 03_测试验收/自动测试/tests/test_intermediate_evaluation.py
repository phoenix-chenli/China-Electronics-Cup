"""Validate frozen references and replay boundaries without model requests."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('intermediate_evaluation',Path(__file__).parents[1]/'评估中级任务.py')
evaluator=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluator)


class IntermediateEvaluationTests(unittest.TestCase):
    def fixture(self,folder):
        data={'synthetic':True,'table_count':1,'decoy_table_count':0,'boundary':'合成协议夹具',
              'cases':[{'id':'one','question':'按独立记录合计',
              'reference_sql':'SELECT SUM(value) AS total FROM rows_table',
              'expected_columns':['total'],'expected_rows':[[60]]}],
              'seed_sql':'CREATE TABLE rows_table(id INTEGER PRIMARY KEY,value REAL); INSERT INTO rows_table VALUES(1,30),(2,30);'}
        path=Path(folder)/'frozen.json';path.write_text(json.dumps(data))
        path.with_suffix('.sha256').write_text(evaluator.digest(path))
        return path,data

    def test_frozen_distinct_records_are_not_deduplicated_by_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path,data=self.fixture(folder)
            loaded,sha=evaluator.load(path)
            self.assertEqual(loaded['cases'][0]['expected_rows'],[[60]])
            self.assertEqual(sha,evaluator.digest(path))

    def test_tampered_frozen_file_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            path,data=self.fixture(folder);path.write_text(path.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'冻结摘要'):evaluator.load(path)

    def test_wrong_reference_or_alias_fails_even_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path,data=self.fixture(folder)
            for field,value in [('expected_rows',[[30]]),('expected_columns',['other'])]:
                altered=json.loads(json.dumps(data));altered['cases'][0][field]=value
                path.write_text(json.dumps(altered));path.with_suffix('.sha256').write_text(evaluator.digest(path))
                with self.assertRaises(ValueError):evaluator.load(path)

    def test_contract_rejects_wrong_layout_nonfinite_values_and_error(self):
        case={'reference_sql':'SELECT 60 AS total','expected_rows':[[60]],'expected_columns':['total']}
        good={'columns':['total'],'rows':[{'total':60.0}],'answer_status':'supported','answer':'合计60'}
        self.assertTrue(evaluator.check_result(good,case)['strict_sql_rows_match'])
        for altered in [{**good,'columns':['wrong']},{**good,'rows':[{'total':float('nan')}]},
                        {**good,'rows':[{'total':60,'extra':1}]}]:
            self.assertFalse(evaluator.check_result(altered,case)['strict_sql_rows_match'])
        self.assertFalse(evaluator.check_result(good,case,'synthetic failure')['expected_state_match'])

    def test_clarification_is_not_scored_as_sql_or_supported_answer(self):
        value={'status':'needs_clarification','answer':'请选择客户编号1或3。'}
        result=evaluator.check_result(value,{'expected_status':'needs_clarification'})
        self.assertTrue(result['expected_state_match']);self.assertIsNone(result['strict_sql_rows_match'])
        self.assertFalse(evaluator.check_result(value,{'expected_status':'supported'})['expected_state_match'])

    def test_existing_output_and_sealed_first_run_never_read_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            path,data=self.fixture(folder);out=Path(folder)/'existing.json';out.write_text('{}')
            stream=io.StringIO('credential must remain unread')
            with patch('sys.stdin',stream),self.assertRaises(FileExistsError):evaluator.execute(out,data)
            self.assertEqual(stream.tell(),0)
            data['first_run_path']='existing.json'
            with patch.object(evaluator,'ROOT',Path(folder)),patch('sys.stdin',stream),self.assertRaisesRegex(ValueError,'首测已封存'):
                evaluator.execute(Path(folder)/'new.json',data,regression=False)
            self.assertEqual(stream.tell(),0)


if __name__=='__main__':unittest.main()
