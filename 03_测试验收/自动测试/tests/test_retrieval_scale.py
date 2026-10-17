"""Indexed scope, exact bounded vectors and streaming index contracts."""
import copy
import hashlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
from tests import test_rag_retrieval as fixtures
from backend import documents,rag_index as index,rag_retrieval as retrieval,store as s
from backend.rag_candidates import ScopedChunks,CHUNK_CACHE,VECTOR_BATCH
import numpy as np


class RetrievalScaleTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.RagRetrievalTest.setUpClass.__func__)
    tearDownClass=classmethod(fixtures.RagRetrievalTest.tearDownClass.__func__)
    setUp=fixtures.RagRetrievalTest.setUp
    tearDown=fixtures.RagRetrievalTest.tearDown
    source=fixtures.RagRetrievalTest.source
    scope=fixtures.RagRetrievalTest.scope

    def long_source(self,n=540):
        paragraphs=[]
        for i in range(n):
            text=f'record{i:08d} software revenue region north expense limit '
            paragraphs.append((text+'background '*30)[:299]+'.')
        return self.source('\n\n'.join(paragraphs))

    def test_regular_query_never_materializes_all_scope_bodies(self):
        doc,_=self.long_source(90);scope=self.scope([doc['id']])
        with patch.object(index,'chunks_for_scope',side_effect=AssertionError('whole-corpus load')):
            result=retrieval.search('record00000000',scope)
        self.assertTrue(result['evidence'])
        self.assertEqual(result['retrieval']['execution']['mode'],'indexed_lazy')
        self.assertLessEqual(result['retrieval']['execution']['peak_cached_chunks'],CHUNK_CACHE)

    def test_bm25_ranking_and_scores_match_full_scope_reference(self):
        doc,_=self.long_source(70);outside,_=self.source('record00000020 outside outside')
        scope=self.scope([doc['id'],outside['id']]);chunks=index.chunks_for_scope(scope)
        narrowed=[c for c in chunks if c['document_id']==doc['id']]
        with_holder=ScopedChunks(scope,[doc['id']])
        try:
            self.assertEqual(retrieval._bm25('record00000020',scope,narrowed),with_holder.bm25('record00000020',30))
        finally:with_holder.close()

    def test_exact_vector_order_matches_reference_across_multiple_batches(self):
        doc,_=self.long_source();index.submit_index([doc['id']],synchronous=True)
        scope=self.scope([doc['id']]);chunks=index.chunks_for_scope(scope)
        self.assertGreater(len(chunks),VECTOR_BATCH)
        rng=np.random.default_rng(614)
        with s.conn() as db:
            for chunk in chunks:
                vector=rng.normal(size=4).astype('<f4')
                db.execute('UPDATE ef_rag_embeddings SET vector=? WHERE chunk_id=?',(vector.tobytes(),chunk['id']))
        expected=retrieval._dense('汽车',chunks,scope)
        lazy=ScopedChunks(scope)
        try:
            actual=lazy.dense('汽车',scope,30)
            self.assertEqual([r['id'] for r in actual],[r['id'] for r in expected])
            np.testing.assert_allclose([r['score'] for r in actual],[r['score'] for r in expected],rtol=1e-6,atol=1e-7)
            self.assertEqual(lazy.metrics['vector_rows_validated'],len(chunks))
            self.assertEqual(lazy.metrics['vector_scan_passes'],2)
            self.assertLessEqual(lazy.metrics['peak_vector_batch_rows'],VECTOR_BATCH)
            self.assertEqual(lazy.metrics['hydrated_chunks'],0)
        finally:lazy.close()

    def test_last_vector_is_checked_before_query_embedding(self):
        doc,_=self.long_source();index.submit_index([doc['id']],synchronous=True)
        scope=self.scope([doc['id']]);lazy=ScopedChunks(scope)
        try:
            batches=list(lazy._vector_batches(self.provider.describe('embedding')['fingerprint']))
            tail=batches[-1][-1]['id']
        finally:lazy.close()
        with s.conn() as db:db.execute('UPDATE ef_rag_embeddings SET vector=? WHERE chunk_id=?',(np.array([np.nan,0,0,0],dtype='<f4').tobytes(),tail))
        self.provider.calls.clear();result=retrieval.search('software revenue',scope)
        self.assertNotIn('dense',result['retrieval']['channels'])
        self.assertEqual(self.provider.calls,[])
        self.assertIn('损坏',result['retrieval']['errors'][0]['error'])

    def test_index_reuses_valid_rows_and_embeds_only_one_missing_input(self):
        doc,_=self.long_source(25);first=index.submit_index([doc['id']],synchronous=True)
        scope=self.scope([doc['id']]);chunks=index.chunks_for_scope(scope)
        self.assertEqual(first['status'],'completed')
        with s.conn() as db:db.execute('DELETE FROM ef_rag_embeddings WHERE chunk_id=?',(chunks[7]['id'],))
        self.provider.calls.clear();job=index.submit_index([doc['id']],synchronous=True)
        sent=[text for kind,texts,kw in self.provider.calls if kind=='embed' for text in texts]
        self.assertEqual(len(sent),1);self.assertEqual(sent[0],retrieval.embedding_input(chunks[7]))
        self.assertEqual(job['embedded_chunks'],1)
        self.assertEqual(job['reused_chunks'],len(chunks)-1)
        self.assertEqual(job['processed'],job['total'])
        self.assertLessEqual(job['peak_chunk_batch'],16)

    def test_partial_corrupt_cache_reembeds_one_input(self):
        doc,_=self.long_source(20);index.submit_index([doc['id']],synchronous=True)
        chunks=index.chunks_for_scope(self.scope([doc['id']]))
        with s.conn() as db:db.execute('UPDATE ef_rag_embeddings SET vector=? WHERE chunk_id=?',(np.zeros(4,dtype='<f4').tobytes(),chunks[0]['id']))
        self.provider.calls.clear();job=index.submit_index([doc['id']],synchronous=True)
        self.assertEqual(job['status'],'completed');self.assertEqual(job['embedded_chunks'],1)

    def test_malformed_cached_blob_is_rebuilt_without_resending_valid_rows(self):
        doc,_=self.long_source(20);index.submit_index([doc['id']],synchronous=True)
        chunks=index.chunks_for_scope(self.scope([doc['id']]))
        with s.conn() as db:db.execute('UPDATE ef_rag_embeddings SET vector=? WHERE chunk_id=?',(b'bad',chunks[0]['id']))
        self.provider.calls.clear();job=index.submit_index([doc['id']],synchronous=True)
        self.assertEqual(job['status'],'completed');self.assertEqual(job['embedded_chunks'],1)

    def test_sql_progress_interruption_has_cancelled_stage(self):
        stop=threading.Event();stages=[]
        def interrupted():stop.set();raise sqlite3.OperationalError('interrupted')
        with self.assertRaises(InterruptedError):retrieval._stage(stages,'bm25',interrupted,stop)
        self.assertEqual(stages[-1]['status'],'cancelled')
        self.assertIsNotNone(stages[-1]['duration_ms'])

    def test_body_cache_evicts_and_rejects_out_of_scope_ids(self):
        wanted,_=self.long_source(220);other,_=self.source('outside material')
        scope=self.scope([wanted['id'],other['id']]);all_chunks=index.chunks_for_scope(scope)
        lazy=ScopedChunks(scope,[wanted['id']])
        try:
            ids=[c['id'] for c in all_chunks if c['document_id']==wanted['id']]
            self.assertEqual(len(lazy.load(ids)),len(ids));self.assertEqual(len(lazy.cache),CHUNK_CACHE)
            self.assertEqual(lazy[ids[0]]['id'],ids[0])
            outside=next(c['id'] for c in all_chunks if c['document_id']==other['id'])
            with self.assertRaisesRegex(ValueError,'固定范围'):lazy[outside]
        finally:lazy.close()

    def test_body_cache_respects_json_byte_budget(self):
        doc,_=self.long_source(4);scope=self.scope([doc['id']]);ids=[c['id'] for c in index.chunks_for_scope(scope)]
        lazy=ScopedChunks(scope)
        try:
            with patch('backend.rag_candidates.CHUNK_CACHE_BYTES',100):
                self.assertEqual(len(lazy.load(ids)),len(ids))
            self.assertEqual(lazy.cache_bytes,0)
            self.assertEqual(len(lazy.cache),0)
        finally:lazy.close()

    def test_changed_chunk_body_does_not_override_published_input_hash(self):
        doc,_=self.source('fixed original input');scope=self.scope([doc['id']]);chunk=index.chunks_for_scope(scope)[0]
        changed={**chunk,'text':'changed unregistered input'}
        with s.conn() as db:db.execute('UPDATE ef_rag_chunks SET body=? WHERE id=?',(json.dumps(changed),chunk['id']))
        lazy=ScopedChunks(scope)
        try:
            with self.assertRaisesRegex(ValueError,'输入摘要'):lazy.load([chunk['id']])
        finally:lazy.close()

    def test_filtered_vector_plan_searches_selected_generations_before_vectors(self):
        first,_=self.long_source(5);second,_=self.long_source(8)
        index.submit_index([first['id'],second['id']],synchronous=True);scope=self.scope([first['id'],second['id']])
        lazy=ScopedChunks(scope,[first['id']]);queries=[]
        lazy.db.set_trace_callback(lambda sql:queries.append(sql) if 'catalog.ef_rag_embeddings' in sql else None)
        try:
            lazy.dense('software',scope,30)
            self.assertEqual(lazy.metrics['vector_rows_scored'],5)
            for sql in queries:
                plan=[row[3] for row in lazy.db.execute('EXPLAIN QUERY PLAN '+sql)]
                self.assertFalse(any('SCAN e' in step for step in plan),plan)
                self.assertTrue(any('SEARCH e' in step for step in plan),plan)
        finally:lazy.close()

    def test_noise_preselection_loads_no_complete_bodies(self):
        doc,_=self.long_source(35);scope=self.scope([doc['id']]);lazy=ScopedChunks(scope)
        try:
            self.assertTrue(lazy.noise_candidates('record00000010'))
            self.assertEqual(lazy.metrics['hydrated_chunks'],0)
            self.assertEqual(len(lazy.cache),0)
        finally:lazy.close()

    def test_attached_catalog_and_publication_are_read_only(self):
        doc,_=self.source('software revenue');scope=self.scope([doc['id']]);lazy=ScopedChunks(scope)
        try:
            for sql in ['DELETE FROM catalog.ef_rag_chunks','DELETE FROM lexical.chunk_meta']:
                with self.assertRaises(sqlite3.OperationalError):lazy.db.execute(sql)
        finally:lazy.close()

    def test_typo_transposition_survives_indexed_bigram_preselection(self):
        doc,_=self.source('abdcef protocol payload details')
        result=retrieval.search('abcdef',self.scope([doc['id']]))
        self.assertTrue(result['retrieval']['channels']['noise_tolerant'])
        self.assertTrue(any('abdcef' in e['snippet'] for e in result['evidence']))
        self.assertLessEqual(result['retrieval']['execution']['noise_preselected_chunks'],400)

    def test_unchanged_scope_preparation_does_not_load_parse_body(self):
        doc,_=self.long_source(30);scope=self.scope([doc['id']])
        with patch.object(documents,'layout',side_effect=AssertionError('loaded full parse')):
            again=index.freeze_scope(requested_scope={'scope_type':'document','document_ids':[doc['id']]})
        self.assertEqual(scope['lexical_generation'],again['lexical_generation'])

    def test_parse_identity_lookup_does_not_select_body(self):
        doc,_=self.source('identity lookup')
        with s.conn() as db:
            statements=[];db.set_trace_callback(statements.append)
            pid=documents.active_parse_id(db,doc['id'],doc['sha256'])
        self.assertEqual(pid,documents.current_parse_id(doc['id'],doc['sha256']))
        self.assertTrue(statements)
        self.assertFalse(any('SELECT *' in statement.upper() or 'BODY' in statement.upper() for statement in statements))

    def test_source_hash_is_still_checked_on_repeated_scope_preparation(self):
        doc,_=self.source('fixed original');self.scope([doc['id']])
        _,path=documents.version(doc['id'],doc['sha256']);path.write_text('bad original!',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'摘要'):self.scope([doc['id']])

    def test_preview_character_count_matches_embedding_input_without_materialization(self):
        doc,_=self.source('# '+('复杂标题'*30)+'\n\n软件采购与费用。\n\n第二条正文。')
        scope=self.scope([doc['id']]);expected=sum(len(retrieval.embedding_input(c)) for c in index.chunks_for_scope(scope))
        with patch.object(retrieval,'embedding_input',side_effect=AssertionError('preview loaded full body')):
            preview=index.index_preview([doc['id']])
        self.assertEqual(preview['character_count'],expected)

    def test_legacy_publication_keeps_its_original_frozen_scope(self):
        doc,_=self.source('legacy fixed reference')
        with patch('backend.rag_candidates.SCHEMA_VERSION',1):scope=self.scope([doc['id']])
        frozen=copy.deepcopy(scope)
        result=retrieval.search('legacy reference',scope)
        self.assertTrue(result['evidence']);self.assertEqual(result['retrieval']['execution']['mode'],'page_selection_or_legacy')
        self.assertEqual(scope,frozen)

    def test_cancelled_search_closes_lazy_reader(self):
        doc,_=self.source('software revenue');index.submit_index([doc['id']],synchronous=True)
        scope=self.scope([doc['id']]);stop=threading.Event();opened=[]
        original=ScopedChunks.__init__;embed=self.provider.embed
        def capture(candidate,*args,**kwargs):original(candidate,*args,**kwargs);opened.append(candidate)
        def cancel(*args,**kwargs):stop.set();return embed(*args,**kwargs)
        with patch.object(ScopedChunks,'__init__',capture),patch.object(self.provider,'embed',cancel):
            with self.assertRaises(InterruptedError):retrieval.search('software',scope,cancelled=stop)
        self.assertTrue(opened);self.assertTrue(all(c.db is None for c in opened))

    def test_invalid_scope_still_reports_a_validation_error(self):
        for scope in [None,{}, {'scope_type':'unknown','documents':[]}]:
            with self.subTest(scope=scope),self.assertRaises(ValueError):retrieval.search('question',scope)
