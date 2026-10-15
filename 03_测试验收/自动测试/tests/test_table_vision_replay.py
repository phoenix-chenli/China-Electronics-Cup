"""Replay recorded public synthetic model output; never invoke a model."""
from collections import Counter
import copy
import json
from pathlib import Path
import unittest
from backend import document_native as n, document_providers as p, rag_index
from tests.test_table_native_goldens import GOLDEN_VALUES


class TableVisionReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(__file__).resolve().parents[2]/'验收证据/复杂表格关系-20261015.json'
        cls.cases={c['id']:c for c in json.loads(path.read_text(encoding='utf-8'))['cases']}

    def page(self,name,mutate=None):
        case=self.cases[name];body=p._strict_json(case['model_response'])
        if mutate:mutate(body)
        validated=p._validate_visual(body,p._observations(case['ocr']['lines']))
        page=n.apply_visual_page(n.apply_ocr_page(n._page(1),case['ocr']),validated)
        return page

    def facts(self,page):
        cells={c['id']:c for t in page['tables'] for c in t['cells']}
        return Counter((tuple(v['row_path']),tuple(v['column_path']),cells[v['value_cell_id']]['text'])
                       for b in page['blocks'] if b.get('status')=='confirmed' for v in b.get('cell_bindings',[]))

    def expected(self):
        return Counter((tuple(rp),tuple(cp),text) for row,col,text,cp,rp in GOLDEN_VALUES)

    def test_clear_recorded_output_restores_all_15_exact_bindings(self):
        page=self.page('clear');self.assertEqual(self.facts(page),self.expected())
        self.assertEqual(len(page['tables'][0]['cells']),30)
        self.assertEqual(len({c['id'] for c in page['tables'][0]['cells']}),30)

    def test_blurred_recorded_output_keeps_weak_row_name_pending_without_false_bindings(self):
        page=self.page('blur');actual=self.facts(page)
        expected=self.expected();self.assertEqual(sum(actual.values()),11)
        self.assertFalse(actual-expected)
        missing=expected-actual
        self.assertEqual({item[0] for item in missing},{('South','C')})
        self.assertEqual(sum(missing.values()),4)

    def test_logical_swap_cannot_pass_using_correct_numbers_in_correct_image_regions(self):
        def swap(body):
            a=next(c for c in body['tables'][0]['cells'] if c['row']==2 and c['column']==2)
            b=next(c for c in body['tables'][0]['cells'] if c['row']==2 and c['column']==3)
            a['column'],b['column']=b['column'],a['column']
        page=self.page('clear',swap)
        self.assertEqual(page['tables'][0]['relation_status'],'pending')
        self.assertFalse(self.facts(page))
        evidence=''.join(page['text'][a:b] for a,b in rag_index._confirmed_ranges(page))
        self.assertNotIn('120',evidence);self.assertNotIn('105',evidence)

    def test_model_copying_a_string_box_is_rejected_instead_of_coerced(self):
        case=self.cases['clear'];body=p._strict_json(case['model_response'])
        body['tables'][0]['box']='[0.04,0.12,0.96,0.86]'
        with self.assertRaisesRegex(ValueError,'四个归一化坐标'):
            p._validate_visual(body,p._observations(case['ocr']['lines']))
