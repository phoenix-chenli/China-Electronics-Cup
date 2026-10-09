"""Actual BM25/dense retrieval, RRF and bounded provider reranking.

Every evidence item contains one continuous original source range. Similarity
scores rank candidates; they never assert factual support or answer confidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from . import documents, rag_index as index, store as s

RECALL_LIMIT = 30
RERANK_LIMIT = 30
EVIDENCE_LIMIT = 8
CONTEXT_CHAR_LIMIT = 12000
RRF_K = 60


def _guard(cancelled=None, deadline=None):
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("检索已取消")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("检索超过共享总时限，未发布迟到证据")


def _stage(stages, name, fn, cancelled=None, trace=None, deadline=None):
    _guard(cancelled, deadline)
    stage = {"stage": name, "status": "running", "started_at": s.now(), "duration_ms": None}
    stages.append(stage)
    start = time.monotonic()
    if trace:
        trace(dict(stage))
    try:
        value = fn()
        _guard(cancelled, deadline)
        stage.update(status="completed", count=len(value) if isinstance(value, list) else None)
        return value
    except InterruptedError:
        stage.update(status="cancelled", error="检索已取消")
        raise
    except Exception as exc:
        stage.update(status="failed", error=str(exc)[:500])
        raise
    finally:
        stage.update(duration_ms=round((time.monotonic()-start)*1000, 2), finished_at=s.now())
        if trace:
            trace(dict(stage))


def _scope_bm25(query, candidates, cancelled=None, deadline=None):
    """Page/selection fallback still uses real FTS5 BM25 with fixed text."""
    expression = index.match_query(query)
    if not expression or not candidates:
        return []
    with sqlite3.connect(":memory:", uri=True, factory=s.ClosingConnection) as db:
        db.set_progress_handler(lambda: int(_interrupted(cancelled, deadline)), 1000)
        db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED,tokens,title_tokens,bigrams,tokenize='unicode61')")
        for chunk in candidates:
            _guard(cancelled, deadline)
            main, grams = index.lexical_tokens(chunk["text"])
            title, _ = index.lexical_tokens(chunk["title_path"])
            db.execute("INSERT INTO chunks VALUES (?,?,?,?)", (chunk["id"], " ".join(main), " ".join(title), " ".join(grams)))
        rows = db.execute("SELECT chunk_id,bm25(chunks,0,1,1.5,.2) AS score FROM chunks WHERE chunks MATCH ? ORDER BY score,chunk_id LIMIT ?", (expression, RECALL_LIMIT)).fetchall()
    return [{"id": cid, "score": float(score), "rank": i+1} for i, (cid, score) in enumerate(rows)]


def _interrupted(cancelled, deadline):
    return (cancelled is not None and cancelled.is_set()) or (deadline is not None and time.monotonic() >= deadline)


def _bm25(query, scope, candidates, cancelled=None, deadline=None):
    if scope["scope_type"] in {"page", "selection"}:
        return _scope_bm25(query, candidates, cancelled, deadline)
    expression = index.match_query(query)
    if not expression or not candidates:
        return []
    path = index.lexical_path(scope, cancelled=cancelled, deadline=deadline)
    allowed = {c["id"] for c in candidates}
    _guard(cancelled, deadline)
    # A temporary allowed-ID table on an in-memory connection lets SQLite filter
    # before LIMIT without a variable-count cap. The attached publication itself
    # is opened read-only; no application/business writes are performed here.
    with sqlite3.connect(":memory:", uri=True, factory=s.ClosingConnection) as db:
        db.execute("CREATE TEMP TABLE allowed(id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO allowed VALUES (?)", [(cid,) for cid in allowed])
        db.execute("ATTACH DATABASE ? AS lexical", (s.readonly_uri(path),))
        db.set_progress_handler(lambda: int(_interrupted(cancelled, deadline)), 1000)
        try:
            rows = db.execute("SELECT chunk_id,bm25(chunks,0,0,1,1.5,.2) AS score FROM lexical.chunks WHERE chunks MATCH ? AND chunk_id IN (SELECT id FROM allowed) ORDER BY score,chunk_id LIMIT ?", (expression, RECALL_LIMIT)).fetchall()
        except sqlite3.OperationalError:
            _guard(cancelled, deadline)
            raise
    return [{"id": cid, "score": float(score), "rank": i+1} for i, (cid, score) in enumerate(rows)]


def embedding_input(chunk):
    # Derived titles are useful for recall but are not citation text. Their
    # conservative cap leaves room in the 512-position Chinese base encoder.
    title = chunk.get("title_path", "")[-64:]
    return (title+"\n" if title else "")+chunk["text"]


def _dense(query, candidates, scope, cancelled=None, deadline=None):
    from . import rag_providers
    import numpy as np
    provider = rag_providers.describe("embedding")
    if not provider.get("ready"):
        raise ValueError(provider.get("reason") or provider.get("error") or "向量模型未就绪")
    fingerprint, dimension = provider["fingerprint"], int(provider["dimension"])
    expected = scope.get("embedding_provider")
    if expected and (fingerprint != expected.get("fingerprint") or provider.get("revision") != expected.get("revision")):
        raise ValueError("向量模型或配置与固定查询范围不一致；使用关键词检索")
    generations = scope.get("vector_generations")
    if generations is not None and any(generations.get(c["generation_id"], {}).get("status") != "ready" or
                                      generations.get(c["generation_id"], {}).get("fingerprint") != fingerprint
                                      for c in candidates):
        raise ValueError("排队时固定范围的向量索引尚未就绪；使用关键词检索")
    rows = []
    with index.reader() as db:
        for candidate in candidates:
            _guard(cancelled, deadline)
            row = db.execute("SELECT vector,dimension,input_sha256 FROM ef_rag_embeddings WHERE chunk_id=? AND fingerprint=?", (candidate["id"], fingerprint)).fetchone()
            if row:
                expected = hashlib.sha256(embedding_input(candidate).encode()).hexdigest()
                if row["dimension"] != dimension or row["input_sha256"] != expected:
                    raise ValueError("向量索引维度或输入摘要不匹配")
                vector = np.frombuffer(row["vector"], dtype="<f4")
                norm = np.linalg.norm(vector.astype(np.float64))
                if vector.size != dimension or not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 1e-12:
                    raise ValueError("已存向量损坏，未使用假分数")
                rows.append((candidate["id"], vector))
    if not rows:
        raise ValueError("固定文档版本尚无该模型的向量索引")
    if len(rows) != len(candidates):
        raise ValueError("固定范围的向量索引尚不完整；使用关键词检索")
    result = rag_providers.embed([query], cancelled=cancelled, expected_fingerprint=fingerprint, purpose="query", deadline=deadline)
    _guard(cancelled, deadline)
    vector = np.asarray(result["vectors"], dtype=np.float32)
    norm = np.linalg.norm(vector.astype(np.float64))
    if vector.shape != (1, dimension) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("问题向量的数量、维度或数值不正确")
    if result.get("provider", {}).get("fingerprint", fingerprint) != fingerprint:
        raise ValueError("问题嵌入与文档嵌入模型空间不一致")
    matrix = np.vstack([row[1] for row in rows])
    matrix /= np.linalg.norm(matrix.astype(np.float64), axis=1)[:, None]
    scores = matrix @ (vector[0] / norm)
    ranks = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), rows[i][0]))[:RECALL_LIMIT]
    return [{"id": rows[i][0], "score": float(scores[i]), "rank": rank+1} for rank, i in enumerate(ranks)]


def reciprocal_rank_fusion(channels, k=RRF_K):
    merged = {}
    for channel, rows in channels.items():
        for row in rows:
            item = merged.setdefault(row["id"], {"id": row["id"], "rrf_score": 0.0, "channels": {}})
            item["rrf_score"] += 1.0/(k+row["rank"])
            item["channels"][channel] = {"rank": row["rank"], "score": row["score"]}
    out = sorted(merged.values(), key=lambda row: (-row["rrf_score"], row["id"]))
    for rank, row in enumerate(out):
        row["fusion_rank"] = rank+1
    return out


def _rerank(query, fused, by_id, scope, cancelled=None, deadline=None):
    from . import rag_providers
    provider = rag_providers.describe("rerank")
    if not provider.get("ready"):
        raise ValueError(provider.get("reason") or provider.get("error") or "重排模型未就绪")
    expected = scope.get("rerank_provider")
    if expected and (provider.get("fingerprint") != expected.get("fingerprint") or provider.get("revision") != expected.get("revision") or not expected.get("ready")):
        raise ValueError("重排模型或配置与固定查询范围不一致；使用融合顺序")
    candidates = fused[:RERANK_LIMIT]
    # Providers must reject/window overlong pairs with real trace, never silently
    # truncate. Each query calls rerank once; its own bounded windows are visible.
    response = rag_providers.rerank(query, [embedding_input(by_id[c["id"]]) for c in candidates],
                                    cancelled=cancelled, expected_fingerprint=provider["fingerprint"], deadline=deadline)
    _guard(cancelled, deadline)
    scores = response["scores"]
    if len(scores) != len(candidates) or any(not isinstance(score, (int, float)) or not __import__("math").isfinite(score) for score in scores):
        raise ValueError("重排返回数量或分数不正确")
    if response.get("provider", {}).get("fingerprint", provider["fingerprint"]) != provider["fingerprint"]:
        raise ValueError("重排模型在请求期间变化")
    out = [{**candidate, "rerank_score": float(score), "rerank_provider": provider["fingerprint"]} for candidate, score in zip(candidates, scores)]
    out.sort(key=lambda c: (-c["rerank_score"], c["fusion_rank"]))
    for i, row in enumerate(out):
        row["rerank_rank"] = i+1
    return out


def _evidence(chunk, ranking):
    locator = {"document_id": chunk["document_id"], "sha256": chunk["sha256"], "parse_id": chunk["parse_id"],
               "page": chunk["page"], "title_path": chunk["title_path"],
               "text_range": {"start": chunk["start"], "end": chunk["end"], "text_sha256": hashlib.sha256(chunk["text"].encode()).hexdigest()}}
    media=chunk.get("quality",{}).get("media_id",[])
    if len(media)==1:locator["media_id"]=media[0]
    located = documents.resolve(locator, chunk["text"])
    if located["status"] != "located":
        raise ValueError("检索切片无法回到固定原件范围")
    eid, sid = "rag-"+chunk["id"], "span-"+chunk["id"]
    return {"id": eid, "type": "document", "source": chunk["source"], "snippet": chunk["text"],
            "status": "text_supported", "locator": located["locator"], "location_status": "located",
            "quality": {**chunk["quality"],**located.get("quality",{})}, "support_span_ids": [sid],
            "support_spans": [{"id": sid, "evidence_id": eid, "text": chunk["text"], "locator": located["locator"], "quality": chunk["quality"]}],
            "retrieval": {"chunk_id": chunk["id"], "generation_id": chunk["generation_id"], **ranking}}


def _pack(ranked, by_id, scope, cancelled=None, deadline=None):
    selected, characters = [], 0
    covered = []
    for ranking in ranked:
        _guard(cancelled, deadline)
        chunk = by_id[ranking["id"]]
        # Do not spend the entire budget on overlapping windows. Different
        # documents remain independent even if their prose is similar.
        overlap = any(c["document_id"] == chunk["document_id"] and c["sha256"] == chunk["sha256"] and c["page"] == chunk["page"] and
                      max(0, min(c["end"], chunk["end"])-max(c["start"], chunk["start"])) >= .75*min(c["end"]-c["start"], chunk["end"]-chunk["start"]) for c in covered)
        if overlap:
            continue
        if characters+len(chunk["text"]) > CONTEXT_CHAR_LIMIT:
            continue
        selected.append(_evidence(chunk, ranking)); covered.append(chunk); characters += len(chunk["text"])
        for span in chunk.get("context_spans", []):
            if len(selected) >= EVIDENCE_LIMIT or characters+len(span["text"]) > CONTEXT_CHAR_LIMIT:
                break
            if any(c["document_id"] == chunk["document_id"] and c["page"] == span["page"] and c["start"] <= span["start"] and c["end"] >= span["end"] for c in covered):
                continue
            context = {**chunk, **span, "id": hashlib.sha256((chunk["id"]+json.dumps(span, sort_keys=True)).encode()).hexdigest()}
            selected.append(_evidence(context, {"context_for": chunk["id"], "reason": "原文表头和分隔行"}))
            covered.append(context); characters += len(span["text"])
        if len(selected) == EVIDENCE_LIMIT:
            break
    return selected


def search(query, scope, *, cancelled=None, trace=None, deadline=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("文档检索问题需要 1–2000 个字符")
    started = time.monotonic()
    deadline = min(deadline if deadline is not None else started+180, started+180)
    _guard(cancelled, deadline)
    index.initialize()
    stages, channels, errors = [], {}, []
    candidates = _stage(stages, "scope", lambda: index.chunks_for_scope(scope, cancelled=cancelled, deadline=deadline), cancelled, trace, deadline)
    by_id = {c["id"]: c for c in candidates}
    if not candidates:
        return {"status": "insufficient", "evidence": [], "count": 0, "stages": stages,
                "index_manifest": scope, "retrieval": {"channels": {}, "errors": ["固定范围内没有可用文字，或来源已归档"], "duration_ms": round((time.monotonic()-started)*1000, 2)}}
    # A selected exact range is itself requested evidence. It need not contain
    # lexical overlap with a question such as '解释这段文字'.
    if scope["scope_type"] == "selection":
        channels["selection"] = [{"id": c["id"], "rank": i+1, "score": None} for i, c in enumerate(candidates)]
    else:
        try:
            channels["bm25"] = _stage(stages, "bm25", lambda: _bm25(query, scope, candidates, cancelled, deadline), cancelled, trace, deadline)
        except (InterruptedError, TimeoutError):
            raise
        except Exception as exc:
            _guard(cancelled, deadline)
            # A corrupt frozen publication is a provenance failure, not a reason
            # to swap to another generation or a semantic index.
            raise ValueError("固定关键词检索未完成："+str(exc)) from None
        try:
            channels["dense"] = _stage(stages, "dense", lambda: _dense(query, candidates, scope, cancelled, deadline), cancelled, trace, deadline)
        except InterruptedError:
            raise
        except Exception as exc:
            _guard(cancelled, deadline)
            errors.append({"stage": "dense", "error": str(exc)[:500]})
    fused = _stage(stages, "rrf", lambda: reciprocal_rank_fusion(channels), cancelled, trace, deadline)
    ranked = fused
    reranked = False
    if fused and scope["scope_type"] != "selection":
        try:
            ranked = _stage(stages, "rerank", lambda: _rerank(query, fused, by_id, scope, cancelled, deadline), cancelled, trace, deadline)
            reranked = True
        except InterruptedError:
            raise
        except Exception as exc:
            _guard(cancelled, deadline)
            errors.append({"stage": "rerank", "error": str(exc)[:500]})
    evidence = _stage(stages, "evidence_pack", lambda: _pack(ranked, by_id, scope, cancelled, deadline), cancelled, trace, deadline)
    # Archival/revocation can happen while a model request is in flight.
    allowed = {d["document_id"] for d in index.validate_scope(scope, cancelled=cancelled, deadline=deadline)}
    evidence = [e for e in evidence if e["locator"]["document_id"] in allowed]
    _guard(cancelled, deadline)
    return {"status": "degraded" if errors and evidence else "retrieved" if evidence else "insufficient",
            "evidence": evidence, "count": len(evidence), "index_manifest": scope, "stages": stages,
            "retrieval": {"channels": channels, "candidate_count": len(candidates), "fusion": fused[:RERANK_LIMIT],
                          "reranked": reranked, "ranked": ranked[:RERANK_LIMIT], "errors": errors,
                          "lexical_generation": scope.get("lexical_generation"), "rrf_k": RRF_K,
                          "duration_ms": round((time.monotonic()-started)*1000, 2),
                          "boundary": "排序分数只表示相关性；充分性和陈述支持由后续主流程核验"}}
