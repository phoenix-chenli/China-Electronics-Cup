"""Frozen reference integrity and paid-run protections; zero model traffic."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('chinook_live_evaluator',Path(__file__).parents[1]/'评估Chinook.py')
evaluator=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluator)


class LiveEvaluationTests(unittest.TestCase):
    def test_frozen_references_execute_readonly(self):
        data,sha=evaluator.validate()
        self.assertEqual(len(data['cases']),20)
        self.assertEqual(sha,evaluator.DATASET.with_suffix('.json.sha256').read_text().split()[0])

    def test_only_line_endings_can_change_without_changing_the_frozen_content(self):
        original=evaluator.DATASET.read_bytes()
        with tempfile.TemporaryDirectory(prefix='evidenceflow-evaluation-lock-') as folder:
            path=Path(folder)/'dataset.json';path.write_bytes(original)
            path.with_suffix('.json.sha256').write_text(hashlib.sha256(original.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')).hexdigest())
            with patch.object(evaluator,'DATASET',path):
                evaluator.validate()
                data=json.loads(path.read_text());data['cases'][0]['expected_rows']=[[999]]
                path.write_text(json.dumps(data,ensure_ascii=False))
                with self.assertRaisesRegex(ValueError,'冻结摘要'):evaluator.validate()

    def test_row_comparison_preserves_order_null_duplicates_and_numeric_types(self):
        case={'expected_rows':[[None,1.0]],'order_matters':True}
        self.assertTrue(evaluator.rows_match([[None,1]],case))
        self.assertFalse(evaluator.rows_match([[0,1]],case))
        self.assertFalse(evaluator.rows_match([[None,True]],case))
        self.assertFalse(evaluator.rows_match([[None,1],[None,1]],case))
        self.assertFalse(evaluator.rows_match([[float('nan'),1]],case))
        self.assertFalse(evaluator.rows_match([[2],[1]],{'expected_rows':[[1],[2]]}))

    def test_existing_result_stops_before_any_client_or_paid_request(self):
        with tempfile.TemporaryDirectory(prefix='evidenceflow-evaluation-paid-') as folder:
            path=Path(folder)/'existing.json';path.write_text('{}')
            with patch.object(evaluator.httpx,'Client') as client:
                with self.assertRaises(FileExistsError):evaluator.execute(path)
                client.assert_not_called()
