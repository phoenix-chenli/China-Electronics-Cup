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
from difflib import SequenceMatcher
from collections import Counter
import heapq

from . import documents, rag_index as index, store as s

RECALL_LIMIT = 30
RERANK_LIMIT = 30
EVIDENCE_LIMIT = 8
CONTEXT_CHAR_LIMIT = 12000
RRF_K = 60


def _noise_recall(query, candidates, cancelled=None, deadline=None):
    """Bounded spelling-tolerant recall only; original evidence is never rewritten."""
    stop={'what','which','the','is','in','of','on','for','with','and','to','a','an','was','does','by','as','it','that','this'}
    terms=sorted(set(re.findall(r'[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}',query.lower()))-stop,key=lambda t:(-len(t),t))[:24]
    if not terms:return []
    # Cheap bounded preselection for large corpora, then expensive spelling
    # comparisons over at most 400 original chunks. No index text is rewritten.
    if len(candidates)>400:
        anchors=sorted({term[i:i+3] for term in terms[:3] for i in range(len(term)-2)})[:24]
        def preselection():
            for chunk in candidates:
                _guard(cancelled,deadline)
                text=chunk['text'].lower()
                score=4*sum(term in text for term in terms)+sum(anchor in text for anchor in anchors)
                yield score,chunk['id'],chunk
        candidates=[row[2] for row in heapq.nlargest(400,preselection(),key=lambda row:(row[0],row[1]))]
    frequency=Counter(); vocabularies=[]
    for chunk in candidates:
        _guard(cancelled,deadline)
        words=set(re.findall(r'[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}',chunk['text'].lower()))
        vocabularies.append(words);frequency.update(words)
    scores=[]
    for chunk,words in zip(candidates,vocabularies):
        _guard(cancelled,deadline)
        score=0.;matched=0
        for term in terms:
            choices=sorted(w for w in words if abs(len(w)-len(term))<=2)
            best=1. if term in words else max((SequenceMatcher(None,term,w,autojunk=False).ratio() for w in choices[:256]),default=0.)
            if best>=.82:
                matched+=1;score+=best/(1+frequency.get(term,0))
        if matched>=min(2,len(terms)):
            scores.append({'id':chunk['id'],'score':score,'matched_terms':matched})
    scores.sort(key=lambda x:(-x['score'],-x['matched_terms'],x['id']))
    return [{**row,'rank':i+1} for i,row in enumerate(scores[:RECALL_LIMIT])]


def _source_gaps(scope, cancelled=None, deadline=None, document_ids=None):
    gaps=[]
    for source in scope['documents']:
        _guard(cancelled,deadline)
        if document_ids is not None and source['document_id'] not in document_ids:continue
        if source.get('generation_id'):
            with index.reader() as db:
                row=db.execute('SELECT body FROM ef_rag_generations WHERE id=?',(source['generation_id'],)).fetchone()
            missing=json.loads(row['body']).get('missing_pages',[]) if row else []
        else:
            body=documents.layout(source['document_id'],source['sha256'],source['parse_id'])
            missing=[]
            for p in body['pages']:
                if p['number']!=scope.get('page'):continue
                if not p['text_available']:missing.append({'page':p['number'],'reason':'没有可用文字或结构待复核'})
                elif any(obj.get('status')=='pending' for kind in ('formulas','tables','charts') for obj in p.get(kind,[])):
                    missing.append({'page':p['number'],'reason':'页面局部结构尚待核验','reason_code':'source_structure_pending'})
                if scope.get('scope_type')!='selection' and any(len(b['text'])>index.CHUNK_TOKENS for b in index._formula_blocks(p)):
                    missing.append({'page':p['number'],'reason':'完整公式超过检索片段窗口，请打开原文单独核验','reason_code':'source_formula_too_long'})
                if scope.get('scope_type')!='selection' and any(b['table_row_ids'] and len(p['text'][b['start']:b['end']])>index.CHUNK_TOKENS for b in index._atomic_blocks(p)):
                    missing.append({'page':p['number'],'reason':'完整表格行超过检索片段窗口，请打开原表单独核验','reason_code':'source_table_row_too_long'})
                if scope.get('scope_type')=='selection':
                    start,end=scope['text_range']['start'],scope['text_range']['end']
                    if any(b['start']<end and b['end']>start and not (start<=b['start'] and end>=b['end']) for b in index._table_row_blocks(p)):
                        missing.append({'page':p['number'],'reason':'选区只包含表格行的一部分，缺少完整对应关系','reason_code':'source_partial_table_row'})
        for page in missing:
            code=page.get('reason_code','source_text_missing')
            actions={'source_formula_too_long':('inspect_original_formula','请打开该固定版本的完整公式原文进行核验'),
                     'source_partial_table_row':('select_complete_table_row','请选择完整表格行，或打开原表同时核验多级表头与行名'),
                     'source_table_row_too_long':('inspect_original_table','请打开该固定版本的原表核验整行及多级表头；检索未拆开这些对应关系'),
                     'source_structure_pending':('reparse_original','本页有局部结构待核验，请对照原件人工复核或重新解析；未核验结构不作为确定证据')}
            action,instruction=actions.get(code,('reparse_original','请对这一固定原件页重新识别；只有空文本且无原图时无法恢复证据'))
            gaps.append({**source,**page,'document_name':s.doc_get(source['document_id'])['name'],
                         'action':action,'reason_code':code,'instruction':instruction})
    return gaps


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


def _continuation(chunk, by_id):
    """Carry a possible native-PDF sentence continuation as separate evidence."""
    methods=chunk.get('quality',{}).get('source_methods',[])
    if methods!=['pdf_native'] or not chunk.get('source','').lower().endswith('.pdf'):return None
    from .rag_grounding import native_sentence_tail
    tail=native_sentence_tail(chunk['text'])
    if not re.search(r'(?:增长|下降|减少|增加|达到|分别为|人均|同比|为|达|占|约|零售额|收入|余额|总额|总量|数量)\s*$',tail):return None
    choices=[other for other in by_id.values() if all(other.get(key)==chunk.get(key) for key in ('document_id','sha256','parse_id'))
             and other['page']==chunk['page']+1 and other['start']<=4
             and other.get('quality',{}).get('source_methods')==['pdf_native']
             and re.match(r'^\s*[-+]?\d[\d,.]*\s*(?:%|％|亿|万|元|人|个|家|吨|微克|所|倍)',other['text'])]
    return min(choices,key=lambda c:c['end']) if choices else None


def _pack(ranked, by_id, scope, cancelled=None, deadline=None):
    selected, characters = [], 0
    covered = []
    for ranking in ranked:
        _guard(cancelled, deadline)
        chunk = by_id[ranking["id"]]
        # Do not spend the entire budget on overlapping windows. Different
        # documents remain independent even if their prose is similar.
        # Judge how much of this candidate is already present. A short returned
        # excerpt must not suppress a longer original containing missing rules.
        # Separate source revisions and parses cannot cover each other's text.
        overlap = any(c["document_id"] == chunk["document_id"] and c["sha256"] == chunk["sha256"] and
                      c["parse_id"] == chunk["parse_id"] and c["page"] == chunk["page"] and
                      max(0, min(c["end"], chunk["end"])-max(c["start"], chunk["start"])) >= .75*(chunk["end"]-chunk["start"]) for c in covered)
        if overlap:
            continue
        if characters+len(chunk["text"]) > CONTEXT_CHAR_LIMIT:
            continue
        selected.append(_evidence(chunk, ranking)); covered.append(chunk); characters += len(chunk["text"])
        continuation=_continuation(chunk,by_id)
        if continuation and len(selected)<EVIDENCE_LIMIT and characters+len(continuation['text'])<=CONTEXT_CHAR_LIMIT and not any(
                c['document_id']==continuation['document_id'] and c['page']==continuation['page'] and c['start']<=continuation['start'] and c['end']>=continuation['end'] for c in covered):
            selected.append(_evidence(continuation,{'context_for':chunk['id'],'reason':'同版本下一页的数值开头；仍须核对原句承接，不自动认定指标或单位'}))
            covered.append(continuation);characters+=len(continuation['text'])
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


def search(query, scope, *, cancelled=None, trace=None, deadline=None, document_ids=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("文档检索问题需要 1–2000 个字符")
    started = time.monotonic()
    deadline = min(deadline if deadline is not None else started+180, started+180)
    _guard(cancelled, deadline)
    index.initialize()
    stages, channels, errors = [], {}, []
    candidates = _stage(stages, "scope", lambda: index.chunks_for_scope(scope, cancelled=cancelled, deadline=deadline), cancelled, trace, deadline)
    if document_ids is not None:
        allowed_ids = {d['document_id'] for d in scope.get('documents', [])}
        if (not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 8
                or not all(isinstance(d, str) and d in allowed_ids for d in document_ids)):
            raise ValueError('检索文档筛选必须来自本轮固定范围，不能扩大范围')
        candidates = [c for c in candidates if c['document_id'] in document_ids]
    gaps=_stage(stages,'source_quality',lambda:_source_gaps(scope,cancelled,deadline,document_ids),cancelled,trace,deadline)
    candidates=[c for c in candidates if c.get('quality',{}).get('table_complete') is not False]
    by_id = {c["id"]: c for c in candidates}
    if not candidates:
        return {"status": "insufficient", "evidence": [], "count": 0, "stages": stages,"source_gaps":gaps,
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
        channels['noise_tolerant']=_stage(stages,'noise_tolerant',lambda:_noise_recall(query,candidates,cancelled,deadline),cancelled,trace,deadline)
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
    # Preserve one independent lexical candidate within the existing budget:
    # a semantic reranker can favor document titles over an exact body match.
    # The candidate still requires original-source and factual-support review.
    lexical = channels.get('bm25', [])
    protected_id = lexical[0]['id'] if lexical else None
    def pack_evidence():
        selected = _pack(ranked, by_id, scope, cancelled, deadline)
        if protected_id and not any(e['retrieval']['chunk_id']==protected_id for e in selected):
            protected = next((row for row in ranked if row['id']==protected_id), None)
            if protected is not None:
                packing_ranked = [{**protected, 'packing_reason':'best_bm25_recall_preserved'}]+[
                    row for row in ranked if row['id']!=protected_id]
                selected = _pack(packing_ranked, by_id, scope, cancelled, deadline)
        return selected
    evidence = _stage(stages, "evidence_pack", pack_evidence, cancelled, trace, deadline)
    # Archival/revocation can happen while a model request is in flight.
    allowed = {d["document_id"] for d in index.validate_scope(scope, cancelled=cancelled, deadline=deadline)}
    evidence = [e for e in evidence if e["locator"]["document_id"] in allowed]
    _guard(cancelled, deadline)
    return {"status": "degraded" if (errors or gaps) and evidence else "retrieved" if evidence else "insufficient","source_gaps":gaps,
            "evidence": evidence, "count": len(evidence), "index_manifest": scope, "stages": stages,
            "retrieval": {"channels": channels, "candidate_count": len(candidates), "fusion": fused[:RERANK_LIMIT],
                          "document_filter": list(document_ids) if document_ids is not None else None,
                          "preserved_lexical_candidate": protected_id,
                          "reranked": reranked, "ranked": ranked[:RERANK_LIMIT], "errors": errors,
                          "lexical_generation": scope.get("lexical_generation"), "rrf_k": RRF_K,
                          "duration_ms": round((time.monotonic()-started)*1000, 2),
                          "boundary": "排序分数只表示相关性；充分性和陈述支持由后续主流程核验"}}
