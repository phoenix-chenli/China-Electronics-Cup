"""Independent-oracle integrity, result comparison and paid-run protection."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'评估优化.py'
spec=importlib.util.spec_from_file_location('quality_evaluation',SCRIPT)
evaluator=importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class QualityEvaluationTests(unittest.TestCase):
    def dataset(self,folder,mutate=lambda data:None):
        data=json.loads(evaluator.DEFAULT.read_text())
        mutate(data)
        path=Path(folder)/'dataset.json'
        path.write_text(json.dumps(data,ensure_ascii=False))
        path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest())
        return path

    def test_reference_sql_is_checked_against_independent_database(self):
        def wrong(data):
            next(c for c in data['cases'] if c['reference'].get('sql'))['reference']['expected_rows']=[['incorrect']]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'独立SQL'):
                evaluator.load(self.dataset(folder,wrong))

    def test_modified_document_cannot_keep_original_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'原文摘要'):
                evaluator.load(self.dataset(folder,lambda d:d['documents'][0].update(text='被替换的原文')))

    def test_unfrozen_or_tampered_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.dataset(folder)
            path.write_text(path.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'冻结摘要'):evaluator.load(path)
            with self.assertRaisesRegex(ValueError,'已冻结'):
                evaluator.load(self.dataset(folder,lambda d:d.update(frozen_before_model_execution=False)))

    def test_result_comparison_preserves_null_empty_grain_and_order(self):
        self.assertTrue(evaluator.rows_match([{'total':12.0001,'ratio':None}],[[12,None]]))
        self.assertFalse(evaluator.rows_match([{'total':12,'ratio':0}],[[12,None]]))
        self.assertFalse(evaluator.rows_match([{'total':12},{'total':12}],[[12]]))
        self.assertFalse(evaluator.rows_match([{'a':2},{'a':1}],[[1],[2]]))
        self.assertTrue(evaluator.rows_match([],[]))
        self.assertFalse(evaluator.rows_match([{'a':float('nan')}],[[0]]))

    def test_existing_checkpoint_prevents_secret_read_and_paid_execution(self):
        data,sha,texts=evaluator.load(evaluator.DEFAULT)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'existing.json';path.write_text('{}')
            args=SimpleNamespace(output=str(path),split='development',case_ids=None,limit=1)
            with patch.object(evaluator,'_execute') as run,patch('sys.stdin',io.StringIO('')):
                with self.assertRaises(FileExistsError):evaluator.execute(args,data,sha,texts)
                run.assert_not_called()

    def test_setup_failure_removes_isolated_credentials(self):
        data,sha,texts=evaluator.load(evaluator.DEFAULT);created=[]
        def fail(args,data,sha,texts,credential,work,chosen,out):
            created.append(work)
            (work/'.private').mkdir();(work/'.private/connections.key').write_text('synthetic-key')
            with sqlite3.connect(work/'app.db') as db:
                db.execute('CREATE TABLE ef_connections(secret TEXT)')
                db.execute('INSERT INTO ef_connections VALUES (?)',('synthetic-encrypted-value',))
            raise ValueError('synthetic setup failure')
        with tempfile.TemporaryDirectory() as folder:
            args=SimpleNamespace(output=str(Path(folder)/'result.json'),split='development',case_ids=None,limit=1)
            with patch.object(evaluator,'_execute',side_effect=fail),patch('sys.stdin',io.StringIO(json.dumps({'endpoint':'https://example.invalid','model':'synthetic','secret':'synthetic'}))):
                with self.assertRaisesRegex(ValueError,'setup failure'):evaluator.execute(args,data,sha,texts)
            for work in created:
                self.assertFalse((work/'.private/connections.key').exists())
                with sqlite3.connect(work/'app.db') as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ef_connections').fetchone()[0],0)
                import shutil
                shutil.rmtree(work)
            report=json.loads(Path(args.output).read_text())
            self.assertEqual(report['phase'],'failed')
            self.assertEqual(report['exception_type'],'ValueError')


if __name__=='__main__':unittest.main()
