"""Isolated real FTS5/SQLite/NumPy retrieval, with explicit provider fixtures.

These protocol fixtures do not establish real Chinese model answer quality.
"""
import hashlib
import copy
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="evidenceflow-rag-")
os.environ["EVIDENCEFLOW_DATA_DIR"] = TEMP.name
from backend import documents, rag_index as index, rag_retrieval as retrieval, store as s
import backend


class Providers(types.ModuleType):
    def __init__(self):
        super().__init__("backend.rag_providers")
        self.calls = []
        self.rerank_ready = False
        self.embedding_ready = True
        self.fail_embedding = False
        self.bad_vector = False
        self.bad_scores = False
        self.revision = 1
        self.local = True
    def describe(self, role):
        return {"ready": self.embedding_ready if role == "embedding" else self.rerank_ready,
                "fingerprint": "fixture-"+role+"-v"+str(self.revision), "dimension": 4, "local": self.local,
                "revision": self.revision, "connection_revision": self.revision,
                "max_tokens": 512, "model": "deterministic-protocol-fixture", "error": "夹具模型未启用"}
    def token_count(self, text, role="embedding", strict=False):
        return len(text)
    def embed(self, texts, **kwargs):
        self.calls.append(("embed", list(texts), kwargs))
        if self.fail_embedding:
            raise ValueError("模拟嵌入故障")
        vectors = []
        for text in texts:
            if "汽车" in text or "车辆" in text:
                vectors.append([1.0, 0, 0, 0])
            elif "餐饮" in text:
                vectors.append([0, 1.0, 0, 0])
            elif "软件" in text:
                vectors.append([0, 0, 1.0, 0])
            else:
                vectors.append([0, 0, 0, 1.0])
        if self.bad_vector:
            vectors = [[0, 0, 0, 0] for _ in texts]
        return {"vectors": vectors, "provider": self.describe("embedding")}
    def rerank(self, query, passages, **kwargs):
        self.calls.append(("rerank", list(passages), kwargs))
        scores = [10.0 if "精确依据" in text else 0.0 for text in passages]
        return {"scores": scores[:-1] if self.bad_scores else scores, "provider": self.describe("rerank")}


class RagRetrievalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init(); index.initialize()
    @classmethod
    def tearDownClass(cls):
        index.POOL.shutdown(wait=True)
        TEMP.cleanup()
    def setUp(self):
        self.provider = Providers()
        self.modules = patch.dict(sys.modules, {"backend.rag_providers": self.provider})
        self.modules.start()
        self.previous_provider = getattr(backend, "rag_providers", None)
        backend.rag_providers = self.provider
        with s.conn() as db:
            db.execute("UPDATE documents SET status='archived'")
            db.execute("UPDATE ef_rag_generations SET active=0")
            db.execute("UPDATE ef_rag_lexical_manifests SET active=0")
    def tearDown(self):
        self.modules.stop()
        if self.previous_provider is None:
            del backend.rag_providers
        else:
            backend.rag_providers = self.previous_provider
    def source(self, text, suffix="md"):
        path = s.UPLOADS/(hashlib.sha256(os.urandom(20)).hexdigest()+"."+suffix)
        path.write_text(text, encoding="utf-8")
        return s.index_document(path), path
    def scope(self, ids=None):
        return index.freeze_scope(requested_scope={"scope_type": "corpus", **({"document_ids": ids} if ids is not None else {})})
    def test_real_fts_bm25_and_locators(self):
        doc, _ = self.source("# 售后规则\n\n甲产品赔付上限为100万元，例外条件是人为损坏。\n")
        result = retrieval.search("甲产品赔付上限及例外条件", self.scope([doc["id"]]))
        self.assertTrue(result["evidence"])
        self.assertTrue(result["retrieval"]["channels"]["bm25"])
        for evidence in result["evidence"]:
            self.assertEqual(doc["sha256"], evidence["locator"]["sha256"])
            self.assertEqual("located", documents.resolve(evidence["locator"], evidence["snippet"])["status"])
        self.assertTrue(all(stage["duration_ms"] is not None for stage in result["stages"]))
    def test_noise_recall_preserves_original_spelling_and_scope(self):
        wanted,_=self.source('DailyDialog retrievel memory performance has a dialogue score of 26.01.')
        outside,_=self.source('DailyDialog retrieval memory outside the selected corpus.')
        result=retrieval.search('DailyDialog retrieval memory performance',self.scope([wanted['id']]))
        self.assertTrue(result['retrieval']['channels']['noise_tolerant'])
        self.assertTrue(any('retrievel' in e['snippet'] for e in result['evidence']))
        self.assertEqual({wanted['id']},{e['locator']['document_id'] for e in result['evidence']})
    def test_large_scope_noise_preselection_keeps_typo_candidate(self):
        candidates=[{'id':str(i),'text':'unrelated forests birds'} for i in range(450)]
        candidates.append({'id':'wanted','text':'DailyDialog retrievel memory performance'})
        found=retrieval._noise_recall('DailyDialog retrieval memory performance',candidates)
        self.assertEqual(found[0]['id'],'wanted')
    def test_empty_source_reports_gap_instead_of_complete_retrieval(self):
        good,_=self.source('DailyDialog scores have printed values.')
        empty,_=self.source('',suffix='txt')
        result=retrieval.search('DailyDialog scores',self.scope([good['id'],empty['id']]))
        self.assertEqual('degraded',result['status'])
        self.assertTrue(any(g['document_id']==empty['id'] and g['reason_code']=='source_text_missing' for g in result['source_gaps']))
    def test_scope_filter_happens_before_limit(self):
        wanted, _ = self.source("保险规定：车辆购置费用可以报销。")
        for _ in range(35):
            self.source("车辆购置车辆购置车辆购置，其他公司的保险规定。")
        result = retrieval.search("车辆购置保险规定", self.scope([wanted["id"]]))
        self.assertEqual({wanted["id"]}, {e["locator"]["document_id"] for e in result["evidence"]})
    def test_model_document_filter_narrows_frozen_scope_before_recall(self):
        wanted,_=self.source('甲机构报销条件：提交原始发票。')
        other,_=self.source('乙机构报销条件：不接受发票复印件。')
        scope=self.scope([wanted['id'],other['id']]); frozen=copy.deepcopy(scope)
        result=retrieval.search('报销条件',scope,document_ids=[wanted['id']])
        self.assertEqual(scope,frozen)
        self.assertTrue(result['evidence'])
        self.assertEqual({wanted['id']},{e['locator']['document_id'] for e in result['evidence']})
        self.assertEqual(result['retrieval']['document_filter'],[wanted['id']])
    def test_model_document_filter_cannot_expand_scope(self):
        wanted,_=self.source('甲机构条件为提交发票。');outside,_=self.source('乙机构条件为提交凭证。')
        scope=self.scope([wanted['id']])
        for ids in [[],[outside['id']],['unknown'],wanted['id'],[None]]:
            with self.assertRaisesRegex(ValueError,'固定范围'):
                retrieval.search('提交条件',scope,document_ids=ids)
    def test_exact_lexical_body_match_survives_semantic_reranker_demoting_it(self):
        wanted,_=self.source('维修报销的原始凭证要求是原件发票和验收单。')
        for i in range(9):
            self.source(f'一般费用规则第{i}项，申请须经审批，不涉及维修原始凭证。')
        scope=self.scope();chunks=index.chunks_for_scope(scope)
        wanted_id=next(c['id'] for c in chunks if c['document_id']==wanted['id'])
        lexical=[{'id':wanted_id,'score':-10.0,'rank':1}]
        ranked=[{'id':c['id'],'rerank_rank':i+1} for i,c in enumerate(
            sorted(chunks,key=lambda c:c['id']==wanted_id))]
        with patch.object(retrieval,'_bm25',return_value=lexical),\
             patch.object(retrieval,'_dense',return_value=[]),\
             patch.object(retrieval,'reciprocal_rank_fusion',return_value=ranked),\
             patch.object(retrieval,'_rerank',return_value=ranked):
            result=retrieval.search('维修原始凭证要求',scope)
        self.assertGreater(next(r['rerank_rank'] for r in ranked if r['id']==wanted_id),retrieval.EVIDENCE_LIMIT)
        self.assertEqual(result['retrieval']['preserved_lexical_candidate'],wanted_id)
        self.assertEqual(result['evidence'][0]['locator']['document_id'],wanted['id'])
        self.assertLessEqual(result['count'],retrieval.EVIDENCE_LIMIT)
        self.assertEqual(result['evidence'][0]['snippet'],next(c['text'] for c in chunks if c['id']==wanted_id))

    def test_chunk_bounds_preserve_unicode_and_original_text(self):
        doc, _ = self.source("# 条件😀\n\n"+("适用条件为年度营业收入达到门槛，金额为100万元。\n"*100))
        scope = self.scope([doc["id"]]); chunks = index.chunks_for_scope(scope)
        self.assertGreater(len(chunks), 3)
        text = documents.get_page(doc["id"], 1, doc["sha256"], scope["documents"][0]["parse_id"])["text"]
        for chunk in chunks:
            self.assertEqual(text[chunk["start"]:chunk["end"]], chunk["text"])
            self.assertLessEqual(len(chunk["text"]), index.CHUNK_TOKENS)
    def test_original_blocks_are_not_replaced(self):
        doc, _ = self.source("# 规则\n\n规则内容。")
        before = s.blocks(doc["id"])
        self.scope([doc["id"]])
        self.assertEqual(before, s.blocks(doc["id"]))

    def test_short_overlap_does_not_hide_required_original_conditions(self):
        text = '保修期限为12个月。申请人须提交发票和验收单。人为损坏不在保修范围内。'
        doc, _ = self.source(text)
        scope = self.scope([doc['id']])
        full = index.chunks_for_scope(scope)[0]
        short = {**full, 'id': full['id'] + '-short', 'start': full['end'] - 13,
                 'text': full['text'][-13:]}
        packed = retrieval._pack([{'id': short['id']}, {'id': full['id']}],
                                  {short['id']: short, full['id']: full}, scope)
        self.assertEqual(len(packed), 2)
        self.assertIn('发票和验收单', packed[1]['snippet'])
        self.assertTrue(all(documents.resolve(e['locator'], e['snippet'])['status'] == 'located' for e in packed))
        reverse = retrieval._pack([{'id': full['id']}, {'id': short['id']}],
                                   {short['id']: short, full['id']: full}, scope)
        self.assertEqual(len(reverse), 1)
    def test_frozen_lexical_statistics_survive_new_publication(self):
        doc, _ = self.source("软件采购规则：采购金额需审核。")
        old = self.scope([doc["id"]])
        before = retrieval.search("软件采购金额审核", old)["retrieval"]["channels"]["bm25"]
        for _ in range(4):
            self.source("软件采购审核采购审核采购，其他规则。")
        new = self.scope()
        self.assertNotEqual(old["lexical_generation"], new["lexical_generation"])
        after = retrieval.search("软件采购金额审核", old)["retrieval"]["channels"]["bm25"]
        self.assertEqual(before, after)
    def test_replace_keeps_old_answer_version(self):
        doc, path = self.source("赔付上限：100万元。")
        old = self.scope([doc["id"]])
        path.write_text("赔付上限：200万元。", encoding="utf-8"); new_doc = s.index_document(path)
        new = self.scope([doc["id"]])
        self.assertNotEqual(old["documents"][0]["sha256"], new["documents"][0]["sha256"])
        result = retrieval.search("赔付上限", old)
        self.assertIn("100万元", result["evidence"][0]["snippet"])
        self.assertNotIn("200万元", result["evidence"][0]["snippet"])
    def test_reparse_same_sha_has_new_generation(self):
        doc, _ = self.source("营业收入：100万元。")
        old = self.scope([doc["id"]]); source = old["documents"][0]
        documents.reparse(doc["id"], 1, doc["sha256"], source["parse_id"])
        new = self.scope([doc["id"]])
        self.assertEqual(source["sha256"], new["documents"][0]["sha256"])
        self.assertNotEqual(source["parse_id"], new["documents"][0]["parse_id"])
        self.assertNotEqual(source["generation_id"], new["documents"][0]["generation_id"])
    def test_archive_excludes_frozen_retrieval_but_history_resolves(self):
        doc, _ = self.source("客户收入为100万元。")
        scope = self.scope([doc["id"]]); ev = retrieval.search("客户收入", scope)["evidence"][0]
        with s.conn() as db:
            db.execute("UPDATE documents SET status='archived' WHERE id=?", (doc["id"],))
        self.assertEqual([], retrieval.search("客户收入", scope)["evidence"])
        self.assertEqual("located", documents.resolve(ev["locator"], ev["snippet"])["status"])
    def test_selected_text_ignores_keyword_overlap_and_stays_pinned(self):
        doc, _ = self.source("营业收入：100万元。\n其他内容：200万元。")
        bound = documents.resolve({"document_id": doc["id"], "sha256": doc["sha256"], "page": 1}, "营业收入：100万元。")
        scope = index.freeze_scope(document_context={**bound["locator"], "quote": bound["quote"]})
        evs = retrieval.search("解释这段文字", scope)["evidence"]
        self.assertEqual(["营业收入：100万元。"], [e["snippet"] for e in evs])
    def test_wrong_selected_range_does_not_match_another_occurrence(self):
        doc, _ = self.source("营业收入：100万元。\n营业收入：100万元。")
        with self.assertRaises(ValueError):
            index.freeze_scope(document_context={"document_id": doc["id"], "sha256": doc["sha256"], "page": 1,
                                                "text_range": {"start": 0, "end": 2, "text_sha256": "0"*64}, "quote": "营业收入：100万元。"})
    def test_numpy_vectors_retrieve_without_keyword_overlap(self):
        car, _ = self.source("车辆购置补贴为五万元。")
        self.source("餐饮服务的经营时间为工作日。")
        job = index.submit_index(synchronous=True)
        self.assertEqual("completed", job["status"], job)
        result = retrieval.search("汽车采购", self.scope())
        self.assertEqual(car["id"], result["evidence"][0]["locator"]["document_id"])
        self.assertTrue(result["retrieval"]["channels"]["dense"])
    def test_embedding_zero_vectors_are_not_ready(self):
        doc, _ = self.source("软件采购。")
        self.provider.bad_vector = True
        job = index.submit_index([doc["id"]], synchronous=True)
        self.assertEqual("failed", job["status"])
        self.assertIn("零向量", job["error"])
        self.assertNotEqual("ready", index.status()["indexes"][0]["status"])
    def test_corrupt_saved_vector_degrades_without_using_nan_scores(self):
        doc, _ = self.source("车辆购置补贴。")
        index.submit_index([doc["id"]], synchronous=True)
        scope = self.scope([doc["id"]]); chunk = index.chunks_for_scope(scope)[0]
        with s.conn() as db:
            db.execute("UPDATE ef_rag_embeddings SET vector=? WHERE chunk_id=?", (np.array([np.nan, 0, 0, 0], dtype="<f4").tobytes(), chunk["id"]))
        self.provider.calls.clear(); result = retrieval.search("车辆购置补贴", scope)
        self.assertTrue(result["evidence"]); self.assertEqual("degraded", result["status"])
        self.assertNotIn("dense", result["retrieval"]["channels"])
        self.assertEqual([], self.provider.calls)
        self.assertIn("损坏", result["retrieval"]["errors"][0]["error"])
    def test_large_finite_vectors_have_finite_normalized_cosine(self):
        doc, _ = self.source("车辆购置补贴。")
        original = self.provider.embed
        def scaled(*args, **kwargs):
            response = original(*args, **kwargs)
            response["vectors"] = [[3e38, 3e38, 0, 0] for _ in args[0]]
            return response
        with patch.object(self.provider, "embed", scaled):
            job = index.submit_index([doc["id"]], synchronous=True)
            self.assertEqual("completed", job["status"], job)
            result = retrieval.search("车辆购置补贴", self.scope([doc["id"]]))
        scores = [c["score"] for c in result["retrieval"]["channels"]["dense"]]
        self.assertTrue(scores); self.assertTrue(all(np.isfinite(s) and abs(s) <= 1.00001 for s in scores))
    def test_embedding_cache_reuses_verified_inputs(self):
        doc, _ = self.source("软件采购。")
        index.submit_index([doc["id"]], synchronous=True)
        before = len([c for c in self.provider.calls if c[0] == "embed"])
        index.submit_index([doc["id"]], synchronous=True)
        self.assertEqual(before, len([c for c in self.provider.calls if c[0] == "embed"]))
    def test_true_provider_rerank_changes_fusion_order(self):
        wanted, _ = self.source("赔付规则的精确依据是人为损坏不赔付。")
        self.source("赔付赔付赔付规则规则，其他参考内容。")
        self.provider.rerank_ready = True
        result = retrieval.search("赔付规则", self.scope())
        self.assertEqual(wanted["id"], result["evidence"][0]["locator"]["document_id"])
        self.assertTrue(result["retrieval"]["reranked"])
        self.assertTrue(any(c[0] == "rerank" for c in self.provider.calls))
    def test_invalid_rerank_scores_degrade_with_failure_trace(self):
        doc, _ = self.source("赔付规则：人为损坏不赔付。")
        self.provider.rerank_ready = True; self.provider.bad_scores = True
        result = retrieval.search("赔付规则", self.scope([doc["id"]]))
        self.assertTrue(result["evidence"])
        self.assertFalse(result["retrieval"]["reranked"])
        self.assertEqual("failed", next(s for s in result["stages"] if s["stage"] == "rerank")["status"])
    def test_rrf_uses_ranks_not_incompatible_raw_scores(self):
        channels = {"bm25": [{"id": "a", "rank": 1, "score": -1000}, {"id": "b", "rank": 2, "score": -2}],
                    "dense": [{"id": "b", "rank": 1, "score": .9}, {"id": "a", "rank": 2, "score": .8}]}
        result = retrieval.reciprocal_rank_fusion(channels)
        self.assertAlmostEqual(1/61+1/62, result[0]["rrf_score"])
        self.assertAlmostEqual(result[0]["rrf_score"], result[1]["rrf_score"])
    def test_query_syntax_is_data_not_fts_injection(self):
        doc, _ = self.source("赔付规则生效。")
        result = retrieval.search('赔付" OR tokens:* NEAR(规则)', self.scope([doc["id"]]))
        self.assertTrue(result["evidence"])
    def test_corrupt_lexical_publication_is_rejected(self):
        doc, _ = self.source("赔付规则生效。")
        scope = self.scope([doc["id"]]); path = index.lexical_path(scope)
        with path.open("ab") as f:
            f.write(b"tampered")
        with self.assertRaisesRegex(ValueError, "摘要"):
            retrieval.search("赔付规则", scope)
    def test_preview_does_not_call_embedding_or_reranker(self):
        doc, _ = self.source("# 软件采购规则标题\n\n软件采购。")
        preview = index.index_preview([doc["id"]])
        self.assertGreater(preview["chunk_count"], 0)
        self.assertGreater(preview["character_count"], 0)
        self.assertEqual([], self.provider.calls)
        chunks = index.chunks_for_scope(self.scope([doc["id"]]))
        self.assertEqual(sum(len(retrieval.embedding_input(c)) for c in chunks), preview["character_count"])
        self.assertGreater(preview["documents"][0]["characters"], preview["documents"][0]["source_characters"])
    def test_cancel_does_not_publish_late_evidence(self):
        doc, _ = self.source("赔付规则生效。")
        scope = self.scope([doc["id"]]); stop = threading.Event(); stop.set()
        with self.assertRaises(InterruptedError):
            retrieval.search("赔付规则", scope, cancelled=stop)

    def test_trace_reports_start_before_actual_provider_and_terminal_after(self):
        doc, _ = self.source("赔付规则：精确依据是人为损坏不赔付。")
        self.provider.rerank_ready = True
        scope = self.scope([doc["id"]]); events = []
        original = self.provider.rerank
        def rerank(*args, **kwargs):
            self.assertEqual(("rerank", "running"), (events[-1]["stage"], events[-1]["status"]))
            self.assertIsNone(events[-1]["duration_ms"])
            return original(*args, **kwargs)
        with patch.object(self.provider, "rerank", rerank):
            result = retrieval.search("赔付规则", scope, trace=events.append)
        self.assertTrue(result["evidence"])
        for stage in result["stages"]:
            pair = [e for e in events if e["stage"] == stage["stage"]]
            self.assertEqual(2, len(pair)); self.assertEqual("running", pair[0]["status"])
            self.assertIn(pair[1]["status"], {"completed", "failed"})
            self.assertIsNotNone(pair[1]["duration_ms"])

    def test_shared_deadline_reaches_both_actual_provider_calls(self):
        doc, _ = self.source("车辆购置补贴为五万元。")
        self.provider.rerank_ready = True
        self.assertEqual("completed", index.submit_index([doc["id"]], synchronous=True)["status"])
        scope = self.scope([doc["id"]]); self.provider.calls.clear()
        deadline = time.monotonic()+20
        retrieval.search("车辆购置补贴", scope, deadline=deadline)
        calls = [c for c in self.provider.calls if c[0] in {"embed", "rerank"}]
        self.assertEqual({"embed", "rerank"}, {c[0] for c in calls})
        self.assertTrue(all(c[2]["deadline"] == deadline for c in calls))

    def test_expired_deadline_stops_before_any_model_call(self):
        doc, _ = self.source("赔付规则生效。")
        scope = self.scope([doc["id"]]); self.provider.calls.clear()
        with self.assertRaises(TimeoutError):
            retrieval.search("赔付规则", scope, deadline=time.monotonic()-1)
        self.assertEqual([], self.provider.calls)

    def test_provider_timeout_cannot_publish_keyword_fallback_after_total_deadline(self):
        doc, _ = self.source("车辆购置补贴为五万元。")
        index.submit_index([doc["id"]], synchronous=True)
        scope = self.scope([doc["id"]]); events = []
        def too_late(*args, **kwargs):
            time.sleep(max(0, kwargs["deadline"]-time.monotonic())+.02)
            raise ValueError("模拟服务超时")
        with patch.object(self.provider, "embed", too_late):
            with self.assertRaises(TimeoutError):
                retrieval.search("车辆购置补贴", scope, deadline=time.monotonic()+.2, trace=events.append)
        self.assertEqual(("dense", "failed"), (events[-1]["stage"], events[-1]["status"]))
        self.assertFalse(any(e["stage"] == "evidence_pack" for e in events))

    def test_provider_call_timeout_degrades_while_shared_budget_remains(self):
        doc, _ = self.source("车辆购置补贴为五万元。")
        index.submit_index([doc["id"]], synchronous=True)
        scope = self.scope([doc["id"]])
        with patch.object(self.provider, "embed", side_effect=TimeoutError("模拟单次模型请求超时")):
            result = retrieval.search("车辆购置补贴", scope, deadline=time.monotonic()+20)
        self.assertEqual("degraded", result["status"]); self.assertTrue(result["evidence"])
        self.assertEqual("failed", next(stage for stage in result["stages"] if stage["stage"] == "dense")["status"])
        self.assertIn("超时", result["retrieval"]["errors"][0]["error"])

    def test_cancel_during_rerank_stops_before_evidence_pack(self):
        doc, _ = self.source("赔付规则生效。")
        self.provider.rerank_ready = True
        scope = self.scope([doc["id"]]); stop = threading.Event(); events = []
        original = self.provider.rerank
        def cancel_now(*args, **kwargs):
            result = original(*args, **kwargs); stop.set(); return result
        with patch.object(self.provider, "rerank", cancel_now):
            with self.assertRaises(InterruptedError):
                retrieval.search("赔付规则", scope, cancelled=stop, trace=events.append)
        self.assertEqual(("rerank", "cancelled"), (events[-1]["stage"], events[-1]["status"]))
        self.assertFalse(any(e["stage"] == "evidence_pack" for e in events))

    def test_archive_during_query_discards_late_evidence(self):
        doc, _ = self.source("车辆购置补贴为五万元。")
        index.submit_index([doc["id"]], synchronous=True)
        scope = self.scope([doc["id"]]); original = self.provider.embed
        def archive_now(*args, **kwargs):
            response = original(*args, **kwargs)
            with s.conn() as db:
                db.execute("UPDATE documents SET status='archived' WHERE id=?", (doc["id"],))
            return response
        with patch.object(self.provider, "embed", archive_now):
            result = retrieval.search("车辆购置补贴", scope)
        self.assertEqual([], result["evidence"])
        self.assertEqual("insufficient", result["status"])

    def test_newly_completed_vectors_do_not_change_queued_scope(self):
        doc, _ = self.source("车辆购置补贴为五万元。")
        queued = self.scope([doc["id"]])
        index.submit_index([doc["id"]], synchronous=True)
        result = retrieval.search("车辆购置补贴", queued)
        self.assertTrue(result["evidence"])
        self.assertNotIn("dense", result["retrieval"]["channels"])
        fresh = retrieval.search("车辆购置补贴", self.scope([doc["id"]]))
        self.assertTrue(fresh["retrieval"]["channels"]["dense"])

    def test_provider_revision_change_degrades_without_sending_query(self):
        doc, _ = self.source("车辆购置补贴为五万元。")
        self.provider.rerank_ready = True
        index.submit_index([doc["id"]], synchronous=True)
        scope = self.scope([doc["id"]]); self.provider.calls.clear(); self.provider.revision += 1
        result = retrieval.search("车辆购置补贴", scope)
        self.assertTrue(result["evidence"]); self.assertEqual("degraded", result["status"])
        self.assertEqual([], self.provider.calls)
        self.assertEqual({"dense", "rerank"}, {e["stage"] for e in result["retrieval"]["errors"]})

    def test_remote_preview_stops_after_replacement_before_next_batch(self):
        doc, path = self.source("软件采购与精确依据。\n"*700)
        self.provider.local = False
        preview = index.index_preview([doc["id"]]); self.assertGreater(preview["chunk_count"], 16)
        original = self.provider.embed
        def replace_now(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_text("软件采购改为其他规则。", encoding="utf-8"); s.index_document(path)
            return result
        with patch.object(self.provider, "embed", replace_now):
            job = index.submit_index([doc["id"]], synchronous=True, remote_index_opt_in=True,
                                     expected_preview=preview, expected_provider=self.provider.describe("embedding")["fingerprint"])
        self.assertEqual("failed", job["status"]); self.assertIn("版本", job["error"])
        self.assertEqual(1, len([c for c in self.provider.calls if c[0] == "embed"]))

    def test_remote_preview_reparse_between_preview_and_worker_sends_nothing(self):
        doc, _ = self.source("软件采购规则。")
        self.provider.local = False
        preview = index.index_preview([doc["id"]])
        documents.reparse(doc["id"], 1, doc["sha256"], preview["documents"][0]["parse_id"])
        job = index.submit_index([doc["id"]], synchronous=True, remote_index_opt_in=True,
                                 expected_preview=preview, expected_provider=self.provider.describe("embedding")["fingerprint"])
        self.assertEqual("failed", job["status"]); self.assertIn("解析", job["error"])
        self.assertEqual([], self.provider.calls)

    def test_remote_preview_provider_change_before_worker_sends_nothing(self):
        doc, _ = self.source("软件采购规则。")
        self.provider.local = False
        preview = index.index_preview([doc["id"]]); expected = self.provider.describe("embedding")["fingerprint"]
        self.provider.revision += 1
        job = index.submit_index([doc["id"]], synchronous=True, remote_index_opt_in=True,
                                 expected_preview=preview, expected_provider=expected)
        self.assertEqual("failed", job["status"]); self.assertIn("预览", job["error"])
        self.assertEqual([], self.provider.calls)

    def test_empty_explicit_index_scope_does_not_send_all_documents(self):
        self.source("软件采购规则。")
        job = index.submit_index([], synchronous=True)
        self.assertEqual("completed", job["status"]); self.assertEqual(0, job["total"])
        self.assertEqual([], self.provider.calls)

    def test_remote_index_without_fixed_preview_is_rejected(self):
        doc, _ = self.source("软件采购规则。")
        self.provider.local = False
        job = index.submit_index([doc["id"]], synchronous=True, remote_index_opt_in=True)
        self.assertEqual("failed", job["status"]); self.assertIn("预览", job["error"])
        self.assertEqual([], self.provider.calls)

    def test_cancelled_index_does_not_mark_late_vectors_ready(self):
        doc, _ = self.source("软件采购规则。")
        stop = threading.Event(); original = self.provider.embed
        def cancel_now(*args, **kwargs):
            result = original(*args, **kwargs); stop.set(); return result
        with patch.object(self.provider, "embed", cancel_now):
            job = index.submit_index([doc["id"]], synchronous=True, cancelled=stop)
        self.assertEqual("cancelled", job["status"])
        self.assertEqual("cancelled", index.status()["indexes"][0]["vector_status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
