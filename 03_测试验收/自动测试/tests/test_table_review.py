"""Real local HTTP review preserves immutable table evidence and geometry guards."""
import copy
import unittest
import uuid
from unittest.mock import patch

from tests.test_conversations import LocalClient, TEMP, conv, s
from backend import document_native as native, document_review, documents, rag_index


def table_cell(row, column, text, role='data', rowspan=1, colspan=1, status='confirmed'):
    return {'row': row, 'column': column, 'text': text, 'role': role,
            'rowspan': rowspan, 'colspan': colspan, 'status': status,
            'box': [column / 3, row / 4, (column + colspan) / 3, (row + rowspan) / 4]}


def merged_table(pending_cost=False, swapped_values=False):
    cells = [table_cell(0, 0, 'Item', 'corner', rowspan=2),
             table_cell(0, 1, '2024', 'column_header', colspan=2),
             table_cell(1, 1, 'Revenue (kCNY)', 'column_header'),
             table_cell(1, 2, 'Cost (kCNY)', 'column_header', status='pending' if pending_cost else 'confirmed')]
    for row, item, values in [(2, 'A', ['10', '8']), (3, 'B', ['20', '0'])]:
        cells.append(table_cell(row, 0, item, 'row_header'))
        cells.extend(table_cell(row, column, value) for column, value in enumerate(values, 1))
    if swapped_values:
        a, b = [c for c in cells if c['row'] == 2 and c['role'] == 'data']
        a['column'], b['column'] = b['column'], a['column']
    return {'id': 'observed', 'title': 'Sales', 'rows': 4, 'columns': 3,
            'box': [0, 0, 1, 1], 'cells': cells,
            'status': 'pending' if pending_cost else 'confirmed'}


class TableReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = LocalClient()

    @classmethod
    def tearDownClass(cls):
        conv.manager.pool.shutdown(wait=True)
        cls.client.close()
        TEMP.cleanup()

    def source(self, table):
        path = s.UPLOADS / (uuid.uuid4().hex + '.md')
        path.write_text('', encoding='utf-8')
        doc = s.index_document(path)
        original = documents.layout(doc['id'])
        body = {k: copy.deepcopy(v) for k, v in original.items() if k not in {'document', 'parse_id'}}
        raw = 'Raw OCR: Item 2024 Revenue (kCNY) Cost (kCNY) A 10 8 B 20 0'
        page = native.apply_ocr_page(native._page(1), {'lines': [
            {'text': raw, 'box': [.02, .55, .98, .95], 'confidence': .99, 'status': 'confirmed'}]})
        page = native.apply_visual_page(page, {'blocks': [], 'tables': [table], 'charts': [],
                                               'formulas': [], 'issues': [], 'provenance': {}})
        body['pages'] = [page]
        with s.conn() as db:
            documents.save_parse(db, doc['id'], doc['sha256'], body, '公开复杂表格人工复核夹具')
        return doc, documents.layout(doc['id']), raw

    def correct(self, doc, before, table, cell, text):
        return self.client.post('/api/documents/' + doc['id'] + '/review', json={
            'version': doc['sha256'], 'parse_id': before['parse_id'], 'page': 1,
            'target': 'cell:' + table['id'] + ':' + cell['id'],
            'operator': '公开复杂表格验收者', 'accepted': True, 'text': text})

    def assert_raw_ocr_is_withheld(self, doc, data, raw):
        page = data['pages'][0]
        raw_block = next(b for b in page['blocks'] if b.get('source_method') == 'ocr')
        self.assertEqual(raw_block['status'], 'pending')
        self.assertEqual(raw_block['source_pending_reason'], 'table_relation_view')
        self.assertIn(raw, page['text'])
        self.assertFalse(any(a < raw_block['end'] and b > raw_block['start']
                             for a, b, _ in rag_index._source_spans(page, len, rag_index.CHUNK_TOKENS)))
        bound = documents.resolve({'document_id': doc['id'], 'sha256': doc['sha256'],
                                   'parse_id': data['parse_id'], 'page': 1}, raw)
        self.assertEqual(bound['status'], 'unlocated')

    def test_leaf_header_review_restores_only_missing_bindings_and_keeps_old_parse(self):
        doc, before, raw = self.source(merged_table(pending_cost=True))
        old_page = copy.deepcopy(before['pages'][0])
        table = old_page['tables'][0]
        cost = next(c for c in table['cells'] if c['row'] == 1 and c['column'] == 2)
        year = next(c for c in table['cells'] if c['row'] == 0 and c['column'] == 1)
        corner = next(c for c in table['cells'] if c['row'] == 0 and c['column'] == 0)
        rows = [b for b in old_page['blocks'] if b.get('table_id')]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('2024 / Revenue (kCNY)' in b['text'] and 'Cost (kCNY)' not in b['text'] for b in rows))
        self.assertTrue(all(all(v['column'] == 1 for v in b['cell_bindings']) for b in rows))
        self.assert_raw_ocr_is_withheld(doc, before, raw)
        target = next(t for t in document_review.targets(old_page) if t['id'].endswith(':' + cost['id']))
        self.assertEqual(target['status'], 'pending')
        with patch.object(rag_index, 'submit_index', return_value={'id': 'table-review-job'}) as submit:
            response = self.correct(doc, before, table, cost, 'Cost (kCNY)')
        self.assertEqual(response.status_code, 200, response.text)
        submit.assert_called_once_with([doc['id']])
        after = documents.layout(doc['id'])
        self.assertNotEqual(after['parse_id'], before['parse_id'])
        after_page = after['pages'][0]
        self.assertEqual(after_page['tables'][0]['relation_status'], 'consistent')
        self.assertTrue(next(c for c in after_page['tables'][0]['cells'] if c['id'] == cost['id'])['human_reviewed'])
        restored = next(b for b in after_page['blocks'] if b.get('table_id') and '行名：B' in b['text'])
        binding = next(v for v in restored['cell_bindings'] if v['column'] == 2)
        self.assertIn('2024 / Cost (kCNY)：0', restored['text'])
        self.assertEqual(binding['column_header_ids'], [year['id'], cost['id']])
        self.assertEqual(binding['row_path'], ['B'])
        self.assertEqual(binding['row_header_label_ids'], [corner['id']])
        self.assertTrue(set(binding['column_header_ids'] + binding['row_header_ids'] + binding['row_header_label_ids']) <= set(restored['cell_ids']))
        self.assert_raw_ocr_is_withheld(doc, after, raw)
        historical = documents.layout(doc['id'], doc['sha256'], before['parse_id'])
        self.assertEqual(historical['pages'][0], old_page)
        self.assertEqual(next(c for c in historical['pages'][0]['tables'][0]['cells'] if c['id'] == cost['id'])['status'], 'pending')

    def test_text_review_cannot_clear_logical_grid_and_rectangle_conflict(self):
        doc, before, raw = self.source(merged_table(swapped_values=True))
        old_page = copy.deepcopy(before['pages'][0])
        table = old_page['tables'][0]
        value = next(c for c in table['cells'] if c['text'] == '10')
        self.assertEqual(table['relation_status'], 'pending')
        self.assertFalse(any(b.get('table_id') for b in old_page['blocks']))
        self.assert_raw_ocr_is_withheld(doc, before, raw)
        with patch.object(rag_index, 'submit_index', return_value={'id': 'table-review-job'}):
            response = self.correct(doc, before, table, value, '11')
        self.assertEqual(response.status_code, 200, response.text)
        after = documents.layout(doc['id'])
        self.assertNotEqual(after['parse_id'], before['parse_id'])
        page = after['pages'][0]
        self.assertEqual(page['tables'][0]['relation_status'], 'pending')
        self.assertEqual(page['tables'][0]['relation_audit']['geometry']['status'], 'pending')
        self.assertFalse(any(b.get('table_id') for b in page['blocks']))
        self.assertEqual(list(rag_index._source_spans(page, len, rag_index.CHUNK_TOKENS)), [])
        self.assertEqual(next(c for c in page['tables'][0]['cells'] if c['id'] == value['id'])['text'], '11')
        self.assert_raw_ocr_is_withheld(doc, after, raw)
        self.assertEqual(documents.layout(doc['id'], doc['sha256'], before['parse_id'])['pages'][0], old_page)


if __name__ == '__main__':
    unittest.main()
