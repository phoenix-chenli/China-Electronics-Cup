"""Project-scoped hybrid retrieval, reusable by single-document and cross-source QA."""
from __future__ import annotations

import hashlib
import json
import math
import struct
import threading
import time
from dataclasses import asdict

from .embeddings import EmbeddingError, check_cancelled
from .hybrid import bm25, rrf, terms, vector_scan
from .index_store import IndexStore
from ..text_normalization import query_variants, VERSION as NORMALIZATION_VERSION
from .policy import query_profile, rank_evidence

CHUNKER_VERSION = 'anchored-rows-confirmed-headings900-overlap120-v4'
MAX_RANK_CANDIDATES = 400


class StaleIndexError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class RetrievalService:
    def __init__(self, index_dir, embedder=None, *, reranker=None, batch_size=16, default_mode='hybrid'):
        self.store = IndexStore(index_dir)
        self.embedder = embedder
        self.reranker = reranker
        if default_mode not in ('lexical', 'vector', 'hybrid', 'rerank'):
            raise ValueError('检索模式无效')
        self.default_mode = default_mode
        self.batch_size = max(1, min(64, batch_size))
        self._build_lock = threading.RLock()

    def invalidate(self, project_id, source_id):
        self.store.invalidate(project_id, source_id)

    def prepare(self, source, project_id, cancelled=lambda: False, *, is_current=None):
        """The caller supplies the authorized project; no unselected source is queried.

        is_current must compare the captured source snapshot to the live store.
        It is checked before atomic publish and every search. Archive/reparse
        should also call invalidate, so even an already prepared handle stops.
        """
        from ..source_snapshot import snapshot
        from ..document_engine import _chunks
        if not project_id or source.get('project_id') not in (None, project_id):
            raise PermissionError('检索资料不属于当前项目')
        if source.get('archived') or source.get('status') not in (None, 'ready', 'attention'):
            raise StaleIndexError('当前资料不可检索')
        check_cancelled(cancelled)
        if is_current is not None and not is_current():
            raise StaleIndexError('资料版本已经变化')
        revision = snapshot(source)
        identity = self.embedder.identity if self.embedder is not None else {'model': None, 'dimension': 0}
        version = {'source': {k: revision[k] for k in ('id', 'sha256', 'parser_version', 'revision')},
                   'chunker': CHUNKER_VERSION, 'lexical':'bm25-variants-'+NORMALIZATION_VERSION,'embedding': identity}
        generation = hashlib.sha256(_json([project_id, version]).encode()).hexdigest()
        with self._build_lock:
            with self.store.connect() as db:
                old = db.execute('SELECT generation FROM active WHERE project_id=? AND source_id=?',
                                 (project_id, source['id'])).fetchone()
                if old is not None and old[0] != generation:
                    db.execute('DELETE FROM active WHERE project_id=? AND source_id=?', (project_id, source['id']))
                    db.execute("UPDATE generations SET state='invalidated',updated=? WHERE id=?", (time.time(), old[0]))
                previous = db.execute('SELECT * FROM generations WHERE id=?', (generation,)).fetchone()
            if previous is not None and previous['state'] == 'ready':
                # Reactivate the exact generation only after caller's source check.
                self._publish(generation, project_id, source['id'], cancelled, is_current)
                return PreparedRetrieval(self, source, project_id, generation, cancelled, is_current, cache_hit=True)
            chunks, truncated = _chunks(source, cancelled)
            now = time.time()
            with self.store.connect() as db:
                # Explicit invalidation discards obsolete staging, but an interrupted
                # identical build resumes committed batches without re-embedding.
                if previous is not None and previous['state'] == 'invalidated':
                    db.execute('DELETE FROM generations WHERE id=?', (generation,))
                db.execute('''INSERT OR IGNORE INTO generations
                    (id,project_id,source_id,version_json,state,created,updated,chunk_count,scan_truncated)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    (generation, project_id, source['id'], _json(version), 'building', now, now, len(chunks), int(truncated)))
                done = {row[0] for row in db.execute('SELECT chunk_id FROM chunks WHERE generation=?', (generation,))}
            mode, reason = ('hybrid', None) if self.embedder is not None else ('lexical', 'embedding_not_configured')
            resumed = len(done)
            # Reuse only identical model inputs from this source/project and
            # embedding identity. New locations/structure still get fresh
            # payloads and postings; a cached vector conveys no old provenance.
            reusable = {}
            if self.embedder is not None:
                with self.store.connect() as db:
                    prior = db.execute('''SELECT id,version_json FROM generations
                        WHERE project_id=? AND source_id=? AND id!=?
                        ORDER BY updated DESC LIMIT 3''', (project_id, source['id'], generation)).fetchall()
                    for old_generation in prior:
                        if json.loads(old_generation['version_json']).get('embedding') != identity:
                            continue
                        for row in db.execute('SELECT payload,vector FROM chunks WHERE generation=? AND vector IS NOT NULL', (old_generation['id'],)):
                            if len(row['vector']) != 4 * identity['dimension']:
                                continue
                            payload = json.loads(row['payload'])
                            key = hashlib.sha256((payload.get('retrieval_hint', '') + '\n' + payload['text']).encode()).digest()
                            reusable.setdefault(key, row['vector'])
            reused_vectors = 0
            for start in range(0, len(chunks), self.batch_size):
                check_cancelled(cancelled)
                batch = [(i, c) for i, c in enumerate(chunks[start:start+self.batch_size], start) if c.id not in done]
                if not batch:
                    continue
                vectors = None
                if mode == 'hybrid':
                    try:
                        inputs = [c.retrieval_hint + '\n' + c.text for _, c in batch]
                        cached = [reusable.get(hashlib.sha256(text.encode()).digest()) for text in inputs]
                        missing = [i for i, value in enumerate(cached) if value is None]
                        fresh = self.embedder.embed([inputs[i] for i in missing], kind='passage', cancelled=cancelled) if missing else []
                        if len(fresh) != len(missing):
                            raise EmbeddingError('向量数量不匹配')
                        fresh = iter(fresh)
                        vectors = [list(struct.unpack('<' + 'f' * identity['dimension'], value)) if value is not None else next(fresh) for value in cached]
                        for vector in vectors:
                            if (len(vector) != identity['dimension'] or any(not math.isfinite(v) for v in vector)
                                or sum(v*v for v in vector) <= 0):
                                raise EmbeddingError('向量输出无效')
                        reused_vectors += len(batch) - len(missing)
                    except EmbeddingError:
                        mode, reason, vectors = 'lexical', 'embedding_build_failed', None
                check_cancelled(cancelled)
                with self.store.connect() as db:
                    for offset, (ordinal, chunk) in enumerate(batch):
                        tokens = terms(chunk.text + '\n' + chunk.retrieval_hint)
                        for variant in query_variants(chunk.text+'\n'+chunk.retrieval_hint,max_chars=20000)[1:]:
                            tokens |= terms(variant)
                        vector = vectors[offset] if vectors is not None else None
                        if vector is not None:
                            norm = math.sqrt(sum(v*v for v in vector))
                            if not norm:
                                raise EmbeddingError('零向量不能发布')
                            vector = struct.pack('<' + 'f'*len(vector), *(v / norm for v in vector))
                        db.execute('INSERT OR IGNORE INTO chunks VALUES(?,?,?,?,?,?)',
                                   (generation, ordinal, chunk.id, _json(asdict(chunk)), sum(tokens.values()), vector))
                        db.executemany('INSERT OR IGNORE INTO postings VALUES(?,?,?,?)',
                                       [(generation, term, chunk.id, count) for term, count in tokens.items()])
                    db.execute('UPDATE generations SET updated=? WHERE id=?', (time.time(), generation))
            with self.store.connect() as db:
                ready, vector_count = db.execute('SELECT count(*),count(vector) FROM chunks WHERE generation=?', (generation,)).fetchone()
                if ready != len(chunks):
                    raise StaleIndexError('索引尚未完整构建')
                if vector_count != ready:
                    mode = 'lexical'
                    reason = reason or 'embedding_build_incomplete'
                diagnostics = {'fallback_reason': reason, 'resumed_chunks': resumed, 'reused_identical_passage_vectors': reused_vectors,
                               'indexed_characters': sum(len(c.text) for c in chunks),
                               'parsed_page_count': len(source.get('pages') or []),
                               'indexed_page_count': len({c.page for c in chunks}),
                               'first_indexed_position': {'page':chunks[0].page,'start':chunks[0].start} if chunks else None,
                               'last_indexed_position': {'page':chunks[-1].page,'end':chunks[-1].end} if chunks else None,
                               'source_extraction_limits': (source.get('metadata') or {}).get('extraction_limits'),
                               'parser_processing_truncated': bool(((source.get('metadata') or {}).get('quality_summary') or {}).get('processing_truncated')),
                               'parser_warnings': list(source.get('warnings') or [])[:20],
                               'full_vector_scan': True}
                db.execute('UPDATE generations SET mode=?,diagnostics=?,updated=? WHERE id=?',
                           (mode, _json(diagnostics), time.time(), generation))
            self._publish(generation, project_id, source['id'], cancelled, is_current)
        return PreparedRetrieval(self, source, project_id, generation, cancelled, is_current, cache_hit=False)

    def _publish(self, generation, project_id, source_id, cancelled, is_current):
        check_cancelled(cancelled)
        if is_current is not None and not is_current():
            raise StaleIndexError('构建期间资料已改变，索引未发布')
        with self.store.connect() as db:
            check_cancelled(cancelled)
            state = db.execute('SELECT state FROM generations WHERE id=?', (generation,)).fetchone()
            if state is None or state[0] == 'invalidated':
                raise StaleIndexError('构建期间索引已失效，未发布')
            db.execute("UPDATE generations SET state='ready',updated=? WHERE id=?", (time.time(), generation))
            db.execute('INSERT OR REPLACE INTO active VALUES(?,?,?)', (project_id, source_id, generation))


class PreparedRetrieval:
    def __init__(self, service, source, project_id, generation, cancelled, is_current, *, cache_hit):
        self.service, self.source, self.project_id, self.generation = service, source, project_id, generation
        self.cancelled, self.is_current, self.cache_hit = cancelled, is_current, cache_hit
        self.searches = []
        self._chunks_cache = None

    def _check(self):
        check_cancelled(self.cancelled)
        if self.is_current is not None and not self.is_current():
            raise StaleIndexError('检索期间资料版本已改变')
        if not self.service.store.active(self.project_id, self.source['id'], self.generation):
            raise StaleIndexError('检索索引已失效')

    def prepared(self):
        from ..document_engine import Chunk
        self._check()
        if self._chunks_cache is None:
            with self.service.store.connect() as db:
                records = db.execute('SELECT payload FROM chunks WHERE generation=? ORDER BY ordinal', (self.generation,)).fetchall()
                meta = db.execute('SELECT scan_truncated FROM generations WHERE id=?', (self.generation,)).fetchone()
            chunks = []
            for row in records:
                data = json.loads(row[0])
                if data.get('table_identity') is not None:
                    data['table_identity'] = tuple(data['table_identity'])
                if 'heading_path' in data:
                    data['heading_path'] = tuple(data['heading_path'])
                chunks.append(Chunk(**data))
            self._chunks_cache = chunks, bool(meta[0])
        return self._chunks_cache

    def select(self, question, *, limit=24, character_limit=16000, focus=None, exclude_ids=(), mode=None, pages=None):
        self._check()
        started = time.monotonic()
        mode = mode or self.service.default_mode
        if mode not in ('lexical', 'vector', 'hybrid', 'rerank'):
            raise ValueError('检索模式无效')
        chunks, truncated = self.prepared()
        if pages is not None:
            if not isinstance(pages,(list,tuple,set)) or any(type(p) is not int or p < 1 for p in pages):
                raise ValueError('检索页范围无效')
            chunks = [c for c in chunks if c.page in pages]
        eligible = {c.id for c in chunks}
        if limit < 1 or character_limit < 1:
            return [], bool(chunks)
        original_query = question + ('\n' + focus if focus else '')
        query_text = original_query[:10000]
        query, _ = query_profile(query_text)
        variants=query_variants(query_text,max_chars=10000)
        lexical, semantic, scanned, fallback = [], [], 0, None
        ranks_limit = min(MAX_RANK_CANDIDATES, max(128, limit * 8))
        with self.service.store.connect() as db:
            meta = db.execute('SELECT * FROM generations WHERE id=?', (self.generation,)).fetchone()
            count, average = db.execute('SELECT count(*),avg(length) FROM chunks WHERE generation=?', (self.generation,)).fetchone()
            # Bind every token; SQLite variables are bounded by question length.
            tokens = list(query)[:1500]
            if tokens:
                placeholders = ','.join('?' for _ in tokens)
                rows = db.execute(f'''SELECT p.term,p.chunk_id,p.tf,c.length,d.df FROM postings p
                    JOIN chunks c ON c.generation=p.generation AND c.chunk_id=p.chunk_id
                    JOIN (SELECT term,count(*) AS df FROM postings WHERE generation=?
                          AND term IN ({placeholders}) GROUP BY term) d ON d.term=p.term
                    WHERE p.generation=? AND p.term IN ({placeholders})''',
                    [self.generation, *tokens, self.generation, *tokens]).fetchall()
                lexical = bm25([row for row in rows if row['chunk_id'] in eligible], query, count, average or 1)[:ranks_limit]
            if mode != 'lexical' and meta['mode'] == 'hybrid':
                try:
                    query_vector = self.service.embedder.embed([query_text], kind='query', cancelled=self.cancelled)[0]
                    norm = math.sqrt(sum(v*v for v in query_vector))
                    if not norm or not all(math.isfinite(v) for v in query_vector):
                        raise EmbeddingError('问题向量无效')
                    query_vector = [v / norm for v in query_vector]
                    cursor = db.execute('SELECT ordinal,chunk_id,vector FROM chunks WHERE generation=? ORDER BY ordinal', (self.generation,))
                    def blocks():
                        while True:
                            block = cursor.fetchmany(256)
                            if not block:
                                break
                            yield [row for row in block if row['chunk_id'] in eligible]
                    semantic, scanned = vector_scan(blocks(), query_vector, ranks_limit, self.cancelled)
                except (EmbeddingError, ValueError):
                    fallback = 'embedding_query_failed'
            elif mode != 'lexical':
                fallback = json.loads(meta['diagnostics']).get('fallback_reason') or 'embedding_not_ready'
        check_cancelled(self.cancelled)
        effective = mode if semantic or mode == 'lexical' else 'lexical'
        ranking = lexical if effective == 'lexical' else semantic if mode == 'vector' else rrf([lexical, semantic])
        mapping = {c.id: c for c in chunks}
        if mode == 'rerank' and self.service.reranker is not None:
            ranking = self.service.reranker(query_text, ranking, {k: asdict(v) for k,v in mapping.items()})
        # Zero lexical matches are a retrieval miss, not evidence of absence.
        # Small documents can still be read in full; no arbitrary prefix of a
        # long unmatched document is described as a semantic result.
        if len(chunks) <= limit and sum(len(c.text) for c in chunks) <= character_limit:
            ranking = [(c.id, 0.) for c in chunks]
        ranking, policy = rank_evidence(query_text, ranking,
            {c.id: {**asdict(c), 'source_key': self.source['id']} for c in chunks},
            {self.source['id']: self.source}, maximum=max(32, limit * 4))
        excluded, selected, used = set(exclude_ids), [], 0
        for identifier, _ in ranking:
            chunk = mapping[identifier]
            if identifier in excluded or used + len(chunk.text) > character_limit:
                continue
            # Keep source positions, table identities and periods. Equal text
            # from different sections is not safe to globally deduplicate.
            selected.append(chunk)
            used += len(chunk.text)
            if len(selected) >= limit:
                break
        self._check()
        self.searches.append({'query': question, 'focus': focus, 'mode_requested': mode, 'mode_used': effective,
            'fallback_reason': fallback, 'indexed_chunks': count, 'vector_rows_scanned': scanned,
            'lexical_candidate_count': len(lexical), 'vector_candidate_count': len(semantic),
            'candidate_limit': ranks_limit, 'query_terms_truncated': len(query) > 1500,
            'lexical_query_variants':variants,'query_normalization_scope':'search_only',
            **policy,
            'query_characters_truncated': len(original_query)>len(query_text),
            'selected_chunk_ids': [c.id for c in selected], 'excluded_chunk_ids': sorted(excluded),
            'selected_pages_filter':sorted(pages) if pages is not None else None,'eligible_chunks':len(chunks),
            'scan_truncated': truncated, 'scope': {'project_id': self.project_id, 'source_ids': [self.source['id']]},
            'reranker': getattr(self.service.reranker, 'name', None) if mode == 'rerank' else None,
            'elapsed_ms': round((time.monotonic()-started)*1000, 2)})
        return sorted(selected, key=lambda c: (c.page,c.start)), len(selected) < len(chunks)

    def audit(self):
        with self.service.store.connect() as db:
            meta = db.execute('SELECT * FROM generations WHERE id=?', (self.generation,)).fetchone()
        return {'index_generation': self.generation, 'cache_hit': self.cache_hit, 'mode': meta['mode'],
                'version': json.loads(meta['version_json']), 'coverage': json.loads(meta['diagnostics']),
                'scan_truncated': bool(meta['scan_truncated']), 'searches': list(self.searches)}
