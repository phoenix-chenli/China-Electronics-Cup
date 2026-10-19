"""Evidence retrieval across an explicitly selected library, without answer generation.

Uses the same persisted product chunks and indexes. The caller supplies prepared,
authorized sources; it never discovers or searches another project's sources.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict

from .embeddings import EmbeddingError
from .hybrid import bm25, rrf, vector_scan
from .policy import query_profile, rank_evidence


def search_library(handles, question, *, limit=10, mode='lexical'):
    handles = list(handles)
    if not handles or len(handles) > 128:
        raise ValueError('请选择1至128份已建立索引的资料')
    if mode not in ('lexical', 'hybrid', 'vector'):
        raise ValueError('检索模式无效')
    if not isinstance(question, str) or not question.strip():
        raise ValueError('问题不能为空')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('返回数量应为1至100')
    first = handles[0]
    if any(h.service is not first.service or h.project_id != first.project_id for h in handles):
        raise PermissionError('同次检索只能使用同一项目和索引服务')
    if len({h.source['id'] for h in handles}) != len(handles):
        raise ValueError('选择的资料不能重复')
    for h in handles:
        h._check()
    started = time.monotonic()
    generations = [h.generation for h in handles]
    by_generation = {h.generation: h for h in handles}
    marks = ','.join('?' for _ in generations)
    query, _ = query_profile(question[:2000])
    tokens = list(query)[:256]
    candidate_limit = max(128, min(400, limit * 8))
    lexical, semantic, scanned, fallback = [], [], 0, None
    with first.service.store.connect() as db:
        count, average, vectors = db.execute(
            f'SELECT count(*),avg(length),count(vector) FROM chunks WHERE generation IN ({marks})', generations).fetchone()
        if tokens:
            tmarks = ','.join('?' for _ in tokens)
            rows = db.execute(f'''SELECT p.term,p.generation || ':' || p.chunk_id AS chunk_id,
                    p.tf,c.length,d.df FROM postings p
                JOIN chunks c ON c.generation=p.generation AND c.chunk_id=p.chunk_id
                JOIN (SELECT term,count(*) AS df FROM postings
                    WHERE generation IN ({marks}) AND term IN ({tmarks}) GROUP BY term) d ON d.term=p.term
                WHERE p.generation IN ({marks}) AND p.term IN ({tmarks})''',
                [*generations, *tokens, *generations, *tokens]).fetchall()
            lexical = bm25(rows, query, count, average or 1)[:candidate_limit]
        if mode != 'lexical':
            if first.service.embedder is None or vectors != count or not count:
                fallback = 'selected_library_vectors_incomplete'
            else:
                try:
                    vector = first.service.embedder.embed([question[:2000]], kind='query', cancelled=first.cancelled)[0]
                    norm = math.sqrt(sum(v*v for v in vector))
                    if not norm or not all(math.isfinite(v) for v in vector):
                        raise EmbeddingError('问题向量无效')
                    cursor = db.execute(f'''SELECT generation || ':' || chunk_id AS chunk_id,
                        vector FROM chunks WHERE generation IN ({marks}) ORDER BY generation,ordinal''', generations)
                    def blocks():
                        ordinal = 0
                        while True:
                            rows = cursor.fetchmany(256)
                            if not rows:
                                return
                            block = []
                            for row in rows:
                                block.append({'chunk_id': row['chunk_id'], 'vector': row['vector'], 'ordinal': ordinal})
                                ordinal += 1
                            yield block
                    semantic, scanned = vector_scan(blocks(), [v/norm for v in vector], candidate_limit, first.cancelled)
                except (EmbeddingError, ValueError):
                    fallback = 'library_query_embedding_failed'
        effective = mode if semantic or mode == 'lexical' else 'lexical'
        ranking = lexical if effective == 'lexical' else semantic if effective == 'vector' else rrf([lexical, semantic])
        chunks = {h.generation + ':' + c.id: {**asdict(c), 'source_key': h.generation}
                  for h in handles for c in h.prepared()[0]}
        ranking, policy = rank_evidence(question[:2000], ranking, chunks,
            {h.generation: h.source for h in handles}, maximum=max(32, limit * 4))
        results = []
        for identifier, score in ranking:
            generation, chunk_id = identifier.split(':', 1)
            chunk = chunks[identifier]
            source = by_generation[generation].source
            results.append({'source_id': source['id'], 'source_name': source['name'], 'chunk_id': chunk_id,
                'page': chunk['page'], 'start': chunk['start'], 'end': chunk['end'], 'quote': chunk['text'],
                'page_unit': source.get('metadata', {}).get('page_unit', 'physical_page' if source.get('format') == 'pdf' else 'logical_section'),
                'source_sha256': source.get('sha256'),
                'heading_path': chunk.get('heading_path', []), 'score': score, 'generation': generation})
            if len(results) >= limit:
                break
    for h in handles:
        h._check()
    coverage = []
    for h in handles:
        audit = h.audit()
        batch = h.source.get('metadata', {}).get('import_batches', {})
        coverage.append({'source_id': h.source['id'], 'source_sha256': h.source.get('sha256'),
            'scan_truncated': audit['scan_truncated'], 'parser_processing_truncated': audit['coverage'].get('parser_processing_truncated', False),
            'pending_pages': batch.get('pending_pages', []), 'import_status': batch.get('status'),
            'indexed_page_count': audit['coverage'].get('indexed_page_count'),
            'parsed_page_count': audit['coverage'].get('parsed_page_count')})
    complete = all(not r['scan_truncated'] and not r['parser_processing_truncated'] and not r['pending_pages']
                   and r['import_status'] in (None, 'complete') for r in coverage)
    return {'results': results, 'audit': {
        'project_id': first.project_id, 'source_ids': [h.source['id'] for h in handles],
        'source_count': len(handles), 'indexed_chunks': count, 'mode_requested': mode, 'mode_used': effective,
        'vector_rows_scanned': scanned, 'fallback_reason': fallback, 'candidate_limit': candidate_limit,
        'candidate_filter': policy['selection_policy'], **policy,
        'source_coverage': coverage, 'library_complete': complete,
        'query_terms_truncated': len(query) > len(tokens), 'query_characters_truncated': len(question) > 2000,
        'elapsed_ms': round((time.monotonic()-started)*1000, 2),
        'scope': '已选资料已索引片段检索；覆盖范围见source_coverage。返回引用候选，不生成答案、不代表问答正确率'}}
