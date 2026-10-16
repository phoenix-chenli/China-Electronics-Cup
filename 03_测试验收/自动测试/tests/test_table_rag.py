"""Table row evidence survives chunk boundaries without inventing partial bindings."""
import copy
import json
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-table-rag-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import document_native as native, documents, rag_grounding as grounding, rag_index as index, rag_retrieval as retrieval, store as s


def append_row(page, text, table_id='sales', value_id='value'):
    native._append(page, text, 'table_row', status='confirmed', table_id=table_id,
                   derived_view=True, cell_ids=['header', 'row-label', value_id],
                   table_relation_version='table-relations-v1',
                   cell_bindings=[{'value_cell_id': value_id,
                                   'column_header_ids': ['header'],
                                   'row_header_ids': ['row-label'],
                                   'column_path': ['2025', 'Revenue (CNY)'],
                                   'row_path': ['North', 'A']}])
    return page['blocks'][-1]


class TableRagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
        index.initialize()

    @classmethod
    def tearDownClass(cls):
        TEMP.cleanup()

    def source_page(self, row_text):
        path = s.UPLOADS / (uuid.uuid4().hex + '.md')
        path.write_text('Original source.', encoding='utf-8')
        doc = s.index_document(path)
        original = documents.layout(doc['id'])
        body = {k: copy.deepcopy(v) for k, v in original.items() if k not in {'document', 'parse_id'}}
        page = native._page(1)
        native._append(page, 'Supporting introduction.', status='confirmed')
        row = append_row(page, row_text)
        page['text_available'] = True
        body['pages'] = [page]
        with s.conn() as db:
            documents.save_parse(db, doc['id'], doc['sha256'], body, 'synthetic table evidence')
        return doc, page, row

    def page_scope(self, doc):
        with patch.object(index, '_provider_snapshot', return_value={}):
            return index.freeze_scope(document_context={'document_id': doc['id'], 'sha256': doc['sha256'], 'page': 1})

    def corpus_scope(self, doc):
        with patch.object(index, '_counter', return_value=(len, 'conservative_unicode_characters', index.CHUNK_TOKENS)):
            generation = index._build_document(doc['id'])
        return {'scope_type': 'corpus', 'documents': [{k: generation[k] for k in ('document_id', 'sha256', 'parse_id', 'generation_id')}]}

    def selection_scope(self, doc, page, quote):
        start = page['text'].index(quote)
        located = documents.resolve({'document_id': doc['id'], 'sha256': doc['sha256'], 'page': 1,
                                     'text_range': {'start': start, 'end': start + len(quote)}}, quote)
        self.assertEqual(located['status'], 'located')
        with patch.object(index, '_provider_snapshot', return_value={}):
            scope = index.freeze_scope(document_context={**located['locator'], 'quote': quote})
        return located, scope

    def test_row_stays_whole_after_near_budget_prose_with_source_offsets(self):
        page = native._page(1)
        native._append(page, 'Opening context. ' * 5, status='confirmed')
        row = append_row(page, 'North / A | 2025 / Revenue (CNY): 120 | Cost (CNY): 80')
        native._append(page, 'Additional explanatory context. ' * 3, status='confirmed')
        spans = list(index._source_spans(page, len, 80))
        intersections = [(a, b, hard) for a, b, hard in spans if a < row['end'] and b > row['start']]
        self.assertEqual(intersections, [(row['start'], row['end'], False)])
        self.assertEqual(page['text'][intersections[0][0]:intersections[0][1]], row['text'])
        self.assertTrue(all(b-a <= 80 for a, b, _ in spans))

    def test_oversized_row_is_skipped_without_losing_adjacent_prose(self):
        page = native._page(1)
        native._append(page, 'Introductory evidence.', status='confirmed')
        row = append_row(page, '2025 / Revenue: 120 | ' * 12)
        native._append(page, 'Closing evidence.', status='confirmed')
        spans = list(index._source_spans(page, len, 80))
        self.assertFalse(any(a < row['end'] and b > row['start'] for a, b, _ in spans))
        self.assertEqual([page['text'][a:b] for a, b, _ in spans], ['Introductory evidence.', 'Closing evidence.'])
        self.assertIn(row['text'], page['text'])

    def test_partial_selection_cannot_carry_full_row_cell_bindings(self):
        page = native._page(1)
        row = append_row(page, 'North / A | Revenue: 120 | Cost: 80')
        before = copy.deepcopy(page)
        quality = index._extraction_quality(page, row['start'] + 15, row['end'])
        self.assertFalse(quality['table_complete'])
        self.assertNotIn('table_cell_bindings', quality)
        self.assertNotIn('cell_ids', quality)
        self.assertNotIn('table_relation_version', quality)
        self.assertEqual(page, before)

    def test_complete_selection_has_qualified_bindings_for_same_cell_ids_in_two_tables(self):
        page = native._page(1)
        first = append_row(page, 'North / A | Revenue: 120', table_id='sales')
        second = append_row(page, 'North / A | Cost: 80', table_id='costs')
        quality = index._extraction_quality(page, first['start'], second['end'])
        self.assertTrue(quality['table_complete'])
        self.assertEqual([b['table_id'] for b in quality['table_cell_bindings']], ['sales', 'costs'])
        self.assertEqual([b['value_cell_id'] for b in quality['table_cell_bindings']], ['value', 'value'])
        self.assertTrue(all('table_id' not in b for row in page['blocks'] for b in row['cell_bindings']))

    def test_pending_interval_does_not_allow_fragments_of_confirmed_row(self):
        page = native._page(1)
        row = append_row(page, 'North / A | Revenue: 120 | Cost: 80')
        page['blocks'].append({'id': 'uncertain', 'start': row['start'] + 15,
                               'end': row['start'] + 18, 'status': 'pending'})
        self.assertEqual(list(index._source_spans(page, len, 80)), [])

    def test_overlapping_formula_and_row_preserve_both_whole_or_skip_both(self):
        page = native._page(1)
        row = append_row(page, 'Revenue: 120 | Margin: (120-80)/120')
        native._append(page, ' dependent condition', status='confirmed')
        page['blocks'].append({'id': 'formula', 'kind': 'formula', 'status': 'confirmed',
                               'formula_id': 'margin', 'start': row['start'] + 22,
                               'end': len(page['text']), 'text': page['text'][row['start'] + 22:]})
        atom = index._atomic_blocks(page)[0]
        self.assertEqual(list(index._source_spans(page, len, 100)), [(row['start'], atom['end'], False)])
        self.assertEqual(list(index._source_spans(page, len, 40)), [])

    def test_index_generation_exposes_oversized_row_gap_and_no_partial_relation(self):
        doc, page, row = self.source_page('2025 / Revenue: 120 | ' * 12)
        with patch.object(index, '_counter', return_value=(len, 'conservative_unicode_characters', 80)):
            generation = index._build_document(doc['id'])
        gap = next(g for g in generation['missing_pages'] if g['reason_code'] == 'source_table_row_too_long')
        self.assertEqual(gap['table_ids'], ['sales'])
        self.assertEqual(gap['table_row_ids'], [row['id']])
        with index.reader() as db:
            chunks = [json.loads(r['body']) for r in db.execute('SELECT body FROM ef_rag_chunks WHERE generation_id=?', (generation['generation_id'],))]
        self.assertEqual([c['text'] for c in chunks], ['Supporting introduction.'])
        self.assertFalse(any(c['quality'].get('table_cell_bindings') for c in chunks))
        self.assertEqual(generation['chunker'], 'source-spans-v6-atomic-chart-points')

    def test_real_page_scope_reports_table_gap_and_keeps_only_adjacent_prose(self):
        doc, page, row = self.source_page('2025 / Revenue: 120 | ' * 30)
        scope = self.page_scope(doc)
        chunks = index.chunks_for_scope(scope)
        self.assertEqual([c['text'] for c in chunks], ['Supporting introduction.'])
        gaps = retrieval._source_gaps(scope)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['reason_code'], 'source_table_row_too_long')
        self.assertEqual(gaps[0]['action'], 'inspect_original_table')
        self.assertEqual(gaps[0]['parse_id'], scope['documents'][0]['parse_id'])
        self.assertEqual(gaps[0]['page'], 1)
        self.assertIn('多级表头', gaps[0]['instruction'])

    def test_corpus_table_gap_comes_from_frozen_generation_with_source_ids(self):
        doc, page, row = self.source_page('2025 / Revenue: 120 | ' * 30)
        scope = self.corpus_scope(doc)
        # A published generation carries its own gap even without reopening a
        # current parse, so subsequent replacement cannot erase the warning.
        with patch.object(documents, 'layout', side_effect=AssertionError('must use pinned generation')):
            gaps = retrieval._source_gaps(scope)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['reason_code'], 'source_table_row_too_long')
        self.assertEqual(gaps[0]['action'], 'inspect_original_table')
        self.assertEqual(gaps[0]['table_ids'], ['sales'])
        self.assertEqual(gaps[0]['table_row_ids'], [row['id']])
        self.assertEqual(gaps[0]['generation_id'], scope['documents'][0]['generation_id'])

    def test_short_complete_row_has_no_page_or_corpus_gap(self):
        doc, page, row = self.source_page('North / A | Revenue: 120 | Cost: 80')
        page_scope = self.page_scope(doc)
        page_rows = [c for c in index.chunks_for_scope(page_scope) if c['quality'].get('table_cell_bindings')]
        self.assertEqual(len(page_rows), 1)
        self.assertEqual(page_rows[0]['text'], row['text'])
        self.assertTrue(page_rows[0]['quality']['table_complete'])
        self.assertEqual(retrieval._source_gaps(page_scope), [])
        self.assertEqual(retrieval._source_gaps(self.corpus_scope(doc)), [])

    def test_saved_complete_row_binding_reaches_authoritative_resolver_and_model_packet(self):
        doc, page, row = self.source_page('North / A | Revenue: 120 | Cost: 80')
        located, scope = self.selection_scope(doc, page, row['text'])
        self.assertTrue(located['quality']['table_complete'])
        self.assertEqual(located['quality']['table_cell_bindings'][0]['table_id'], 'sales')
        self.assertEqual(located['locator']['source_refs'][0]['cell_ids'], row['cell_ids'])
        # Selection uses the pinned source directly and makes no embedding,
        # reranking, generation, or other model call.
        result = retrieval.search('解释这行', scope)
        self.assertEqual(result['status'], 'retrieved')
        self.assertEqual(len(result['evidence']), 1)
        evidence = result['evidence'][0]
        # Binding must be recovered from the saved parse, not trusted from an
        # index/protocol fixture that could claim another table identity.
        evidence['quality'] = {'table_complete': True, 'table_cell_bindings': [{'table_id': 'forged'}]}
        span = grounding.bind_source(evidence)
        packet = grounding.packet([span])
        extraction = packet['spans'][0]['extraction']
        self.assertTrue(extraction['table_complete'])
        self.assertEqual(extraction['cell_ids'], row['cell_ids'])
        self.assertEqual(extraction['table_relation_version'], ['table-relations-v1'])
        self.assertEqual(extraction['table_cell_bindings'][0]['table_id'], 'sales')
        self.assertEqual(extraction['table_cell_bindings'][0]['row_path'], ['North', 'A'])
        self.assertEqual(extraction['table_cell_bindings'][0]['column_path'], ['2025', 'Revenue (CNY)'])

    def test_partial_selection_cannot_reclaim_whole_row_refs_or_answer_evidence(self):
        doc, page, row = self.source_page('North / A | Revenue: 120 | Cost: 80')
        quote = 'Revenue: 120'
        located, scope = self.selection_scope(doc, page, quote)
        self.assertFalse(located['quality']['table_complete'])
        self.assertNotIn('table_cell_bindings', located['quality'])
        self.assertTrue(all('cell_ids' not in ref for ref in located['locator']['source_refs']))
        forged = {'id': 'ev', 'type': 'document', 'source': doc['name'], 'snippet': quote,
                  'locator': located['locator'], 'quality': {'confirmed': True, 'table_complete': True}}
        with self.assertRaisesRegex(ValueError, '完整表格行'):
            grounding.bind_source(forged)
        result = retrieval.search('收入是多少', scope)
        self.assertEqual(result['status'], 'insufficient')
        self.assertEqual(result['evidence'], [])
        self.assertEqual(result['source_gaps'][0]['reason_code'], 'source_partial_table_row')
        self.assertEqual(result['source_gaps'][0]['action'], 'select_complete_table_row')
        saved = documents.layout(doc['id'])['pages'][0]
        self.assertEqual(next(b for b in saved['blocks'] if b['id'] == row['id'])['cell_ids'], row['cell_ids'])


if __name__ == '__main__':
    unittest.main()
