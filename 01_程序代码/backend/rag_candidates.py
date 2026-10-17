"""Query-local, read-only candidate access and bounded exact vector retrieval.

Full source hashes and revocation checks remain in rag_index. Nothing here
caches a model judgment or shares private chunks between requests.
"""
from __future__ import annotations
from collections import OrderedDict
from collections.abc import Mapping
import hashlib
import heapq
import json
import re
import sqlite3
import time
from . import store as s

SCHEMA_VERSION = 3
HYDRATION_BATCH = 128
CHUNK_CACHE = 192
CHUNK_CACHE_BYTES = 4*1024*1024
NOISE_CANDIDATES = 400
VECTOR_BATCH = 512
NOISE_WORD = re.compile(r'[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}')
NOISE_STOP={'what','which','the','is','in','of','on','for','with','and','to','a','an','was','does','by','as','it','that','this'}


def noise_grams(text):
    words=set(NOISE_WORD.findall(text.lower()))
    return sorted({'b'+word[i:i+2] for word in words for i in range(len(word)-1)})


class ScopedChunks(Mapping):
    """A bounded body cache over a pinned publication and catalog read snapshot."""
    def __init__(self, scope, document_ids=None, cancelled=None, deadline=None):
        from . import rag_index as index
        self.cancelled,self.deadline=cancelled,deadline
        self.index=index
        self.db=None
        self.cache=OrderedDict()
        self.cache_sizes={};self.cache_bytes=0
        self.metrics={'mode':'indexed_lazy','hydrated_chunks':0,'body_cache_limit':CHUNK_CACHE,
            'peak_cached_chunks':0,'noise_preselection_limit':NOISE_CANDIDATES,
            'body_cache_json_byte_limit':CHUNK_CACHE_BYTES,'peak_cached_json_bytes':0,
            'vector_batch_limit':VECTOR_BATCH,'vector_rows_validated':0,
            'peak_vector_batch_rows':0,'vector_scan_passes':0}
        try:
            sources=index.validate_scope(scope,cancelled=cancelled,deadline=deadline)
            if document_ids is not None:sources=[row for row in sources if row['document_id'] in document_ids]
            self.sources={row['generation_id']:row for row in sources}
            path=index.lexical_path(scope,cancelled=cancelled,deadline=deadline)
            self.db=sqlite3.connect(':memory:',uri=True,factory=s.ClosingConnection)
            self.db.row_factory=sqlite3.Row
            self.db.set_progress_handler(lambda:int(self.interrupted()),1000)
            self.db.execute('ATTACH DATABASE ? AS lexical',(s.readonly_uri(path),))
            version=self.db.execute('PRAGMA lexical.user_version').fetchone()[0]
            if version>SCHEMA_VERSION:raise ValueError('固定检索索引格式超出当前支持范围，请重建索引')
            self.available=version==SCHEMA_VERSION
            if not self.available:return
            self.db.execute('ATTACH DATABASE ? AS catalog',(s.readonly_uri(s.APP_DB),))
            self.db.execute('CREATE TEMP TABLE allowed_generations(id TEXT PRIMARY KEY)')
            self.db.executemany('INSERT INTO allowed_generations VALUES (?)',[(gid,) for gid in self.sources])
            self.db.commit()
            self.db.execute('BEGIN')
            # The shared catalog and publication are attached read-only. A
            # transaction pins the vectors during both validation and scoring.
            for gid,source in self.sources.items():
                row=self.db.execute('SELECT 1 FROM catalog.ef_rag_generations WHERE id=? AND document_id=? AND sha256=? AND parse_id=?',
                    (gid,source['document_id'],source['sha256'],source['parse_id'])).fetchone()
                if not row:raise ValueError('固定切片代次缺失，未改用当前版本')
            self.count=self.db.execute('SELECT count(*) FROM lexical.chunk_meta m JOIN allowed_generations a ON a.id=m.generation_id WHERE m.usable=1').fetchone()[0]
            self.metrics['scope_chunks']=self.count
        except BaseException:
            self.close()
            raise

    def interrupted(self):
        return (self.cancelled is not None and self.cancelled.is_set()) or (self.deadline is not None and time.monotonic()>=self.deadline)

    def guard(self):self.index._guard(self.cancelled,self.deadline)

    def close(self):
        if self.db is not None:
            self.db.close();self.db=None
        self.cache.clear()
        self.cache_sizes.clear();self.cache_bytes=0

    def __len__(self):return self.count

    def __iter__(self):
        self.guard()
        for row in self.db.execute('SELECT m.chunk_id FROM lexical.chunk_meta m JOIN allowed_generations a ON a.id=m.generation_id WHERE m.usable=1'):
            self.guard();yield row[0]

    def _remember(self,row):
        from .rag_retrieval import embedding_input
        chunk=json.loads(row['body']);source=self.sources.get(chunk.get('generation_id'))
        if not source or any(chunk.get(key)!=source[key] for key in ('document_id','sha256','parse_id')) or chunk.get('id')!=row['id']:
            raise ValueError('切片来源与固定代次不一致')
        if hashlib.sha256(embedding_input(chunk).encode()).hexdigest()!=row['expected_input_sha']:
            raise ValueError('切片内容与固定索引输入摘要不一致')
        self.metrics['hydrated_chunks']+=1
        size=len(row['body'].encode('utf-8'))
        if size>CHUNK_CACHE_BYTES:return chunk
        self.cache[row['id']]=chunk;self.cache.move_to_end(row['id'])
        self.cache_sizes[row['id']]=size;self.cache_bytes+=size
        while len(self.cache)>CHUNK_CACHE or self.cache_bytes>CHUNK_CACHE_BYTES:
            cid,_=self.cache.popitem(last=False);self.cache_bytes-=self.cache_sizes.pop(cid)
        self.metrics['peak_cached_chunks']=max(self.metrics['peak_cached_chunks'],len(self.cache))
        self.metrics['peak_cached_json_bytes']=max(self.metrics['peak_cached_json_bytes'],self.cache_bytes)
        return chunk

    def load(self,ids):
        """Return only requested, authorized complete chunks, in requested order."""
        ids=list(dict.fromkeys(ids));found={cid:self.cache[cid] for cid in ids if cid in self.cache}
        missing=[cid for cid in ids if cid not in found]
        for offset in range(0,len(missing),HYDRATION_BATCH):
            self.guard();batch=missing[offset:offset+HYDRATION_BATCH]
            rows=self.db.execute('SELECT c.id,c.body,m.input_sha256 AS expected_input_sha FROM catalog.ef_rag_chunks c JOIN lexical.chunk_meta m ON m.chunk_id=c.id AND m.generation_id=c.generation_id JOIN allowed_generations a ON a.id=m.generation_id WHERE m.usable=1 AND c.id IN ('+','.join('?' for _ in batch)+')',batch)
            for row in rows:self.guard();found[row['id']]=self._remember(row)
        if len(found)!=len(ids):raise ValueError('固定范围内的完整切片已缺失，未使用其他来源替代')
        return [found[cid] for cid in ids]

    def __getitem__(self,cid):
        if cid in self.cache:
            self.cache.move_to_end(cid);return self.cache[cid]
        return self.load([cid])[0]

    def bm25(self,query,limit):
        expression=self.index.match_query(query)
        if not expression or not self.count:return []
        self.guard()
        rows=self.db.execute('SELECT chunk_id,bm25(chunks,0,0,1,1.5,.2) AS score FROM lexical.chunks WHERE chunks MATCH ? AND generation_id IN (SELECT id FROM allowed_generations) AND EXISTS (SELECT 1 FROM lexical.chunk_meta m WHERE m.chunk_id=chunks.chunk_id AND m.usable=1) ORDER BY score,chunk_id LIMIT ?',(expression,limit)).fetchall()
        self.metrics['lexical_returned_rows']=len(rows)
        return [{'id':row[0],'score':float(row[1]),'rank':i+1} for i,row in enumerate(rows)]

    def noise_candidates(self,query):
        # Round-robin query grams prevent a long clause from exhausting the
        # entire query budget. The original spelling is retained in evidence.
        words=sorted(set(NOISE_WORD.findall(query.lower()))-NOISE_STOP,key=lambda word:(-len(word),word))[:24]
        buckets=[noise_grams(word) for word in words];grams=[]
        for position in range(max(map(len,buckets),default=0)):
            for bucket in buckets:
                if position<len(bucket) and bucket[position] not in grams:grams.append(bucket[position])
                if len(grams)==96:break
            if len(grams)==96:break
        if not grams:return []
        expression=' OR '.join('"'+gram.replace('"','""')+'"' for gram in grams)
        self.guard()
        rows=self.db.execute('SELECT chunk_id,bm25(noise) AS score FROM lexical.noise WHERE noise MATCH ? AND EXISTS (SELECT 1 FROM lexical.chunk_meta m JOIN allowed_generations a ON a.id=m.generation_id WHERE m.chunk_id=noise.chunk_id AND m.usable=1) ORDER BY score,chunk_id LIMIT ?',(expression,NOISE_CANDIDATES)).fetchall()
        self.metrics['noise_preselected_chunks']=len(rows)
        ids=[row[0] for row in rows];found={}
        # Spelling comparison only needs text, not complete chunk bodies and
        # table/chart context. Hydrate full evidence after ranks are known.
        for offset in range(0,len(ids),HYDRATION_BATCH):
            self.guard();batch=ids[offset:offset+HYDRATION_BATCH]
            for row in self.db.execute('SELECT c.id,c.text FROM catalog.ef_rag_chunks c JOIN lexical.chunk_meta m ON m.chunk_id=c.id AND m.generation_id=c.generation_id JOIN allowed_generations a ON a.id=m.generation_id WHERE m.usable=1 AND c.id IN ('+','.join('?' for _ in batch)+')',batch):
                found[row['id']]={'id':row['id'],'text':row['text']}
        if len(found)!=len(ids):raise ValueError('固定范围内的模糊检索文本已缺失')
        return [found[cid] for cid in ids]

    def continuation_candidates(self,chunk):
        self.guard()
        rows=self.db.execute('SELECT chunk_id FROM lexical.chunk_meta WHERE generation_id=? AND usable=1 AND page=? AND start<=4 ORDER BY end,chunk_id',
            (chunk['generation_id'],chunk['page']+1)).fetchall()
        return self.load([row[0] for row in rows])

    def _vector_batches(self,fingerprint,include_inputs=True):
        self.metrics['vector_scan_passes']+=1
        # CROSS JOIN fixes the loop order: selected generations first. SQLite
        # must not scan all model spaces when a user filters one document.
        sql=('SELECT m.chunk_id AS id,m.input_sha256 AS expected_input_sha,e.vector,e.dimension,e.input_sha256 FROM allowed_generations a CROSS JOIN lexical.chunk_meta m INDEXED BY chunk_meta_scope LEFT JOIN catalog.ef_rag_embeddings e ON e.chunk_id=m.chunk_id AND e.fingerprint=? WHERE m.generation_id=a.id AND m.usable=1'
             if include_inputs else 'SELECT m.chunk_id AS id,e.vector FROM allowed_generations a CROSS JOIN lexical.chunk_meta m INDEXED BY chunk_meta_scope CROSS JOIN catalog.ef_rag_embeddings e WHERE m.generation_id=a.id AND m.usable=1 AND e.chunk_id=m.chunk_id AND e.fingerprint=?')
        cursor=self.db.execute(sql,(fingerprint,))
        while True:
            self.guard();rows=cursor.fetchmany(VECTOR_BATCH)
            if not rows:break
            self.metrics['peak_vector_batch_rows']=max(self.metrics['peak_vector_batch_rows'],len(rows))
            yield rows

    def dense(self,query,scope,limit):
        from . import rag_providers
        import numpy as np
        self.guard();provider=rag_providers.describe('embedding')
        if not provider.get('ready'):raise ValueError(provider.get('reason') or provider.get('error') or '向量模型未就绪')
        fingerprint,dimension=provider['fingerprint'],int(provider['dimension'])
        expected=scope.get('embedding_provider')
        if expected and (fingerprint!=expected.get('fingerprint') or provider.get('revision')!=expected.get('revision')):
            raise ValueError('向量模型或配置与固定查询范围不一致；使用关键词检索')
        generations=scope.get('vector_generations')
        if generations is not None and any(generations.get(gid,{}).get('status')!='ready' or generations.get(gid,{}).get('fingerprint')!=fingerprint for gid in self.sources if self.db.execute('SELECT 1 FROM lexical.chunk_meta WHERE generation_id=? AND usable=1 LIMIT 1',(gid,)).fetchone()):
            raise ValueError('排队时固定范围的向量索引尚未就绪；使用关键词检索')
        validated=0
        # Validate before any outbound query embedding. Two bounded scans avoid
        # both N point queries and an unbounded N-by-dimension resident matrix.
        for rows in self._vector_batches(fingerprint):
            for row in rows:
                self.guard()
                if row['vector'] is None:raise ValueError('固定范围的向量索引尚不完整；使用关键词检索')
                if row['dimension']!=dimension or row['input_sha256']!=row['expected_input_sha']:
                    raise ValueError('向量索引维度或输入摘要不匹配')
                if len(row['vector'])!=dimension*4:
                    raise ValueError('已存向量损坏，未使用假分数')
            matrix=np.frombuffer(b''.join(row['vector'] for row in rows),dtype='<f4').reshape(len(rows),dimension)
            norms=np.linalg.norm(matrix.astype(np.float64),axis=1)
            if not np.isfinite(matrix).all() or not np.isfinite(norms).all() or np.any(norms<=1e-12):
                raise ValueError('已存向量损坏，未使用假分数')
            validated+=len(rows)
            self.metrics['vector_rows_validated']=validated
        if validated!=self.count or not validated:raise ValueError('固定范围的向量索引尚不完整；使用关键词检索')
        self.metrics['vector_rows_validated']=validated
        response=rag_providers.embed([query],cancelled=self.cancelled,expected_fingerprint=fingerprint,purpose='query',deadline=self.deadline)
        self.guard();query_vector=np.asarray(response['vectors'],dtype=np.float32)
        norm=np.linalg.norm(query_vector.astype(np.float64))
        if query_vector.shape!=(1,dimension) or not np.isfinite(query_vector).all() or not np.isfinite(norm) or norm<=1e-12:
            raise ValueError('问题向量的数量、维度或数值不正确')
        if response.get('provider',{}).get('fingerprint',fingerprint)!=fingerprint:raise ValueError('问题嵌入与文档嵌入模型空间不一致')
        top=[]
        scored=0
        for rows in self._vector_batches(fingerprint,include_inputs=False):
            matrix=np.frombuffer(b''.join(row['vector'] for row in rows),dtype='<f4').reshape(len(rows),dimension).copy()
            matrix/=np.linalg.norm(matrix.astype(np.float64),axis=1)[:,None]
            # A matrix-vector reduction avoids dispatching many tiny BLAS
            # jobs. It still examines every authorized vector, with no ANN.
            scores=np.einsum('ij,j->i',matrix,query_vector[0]/norm,optimize=False)
            top=heapq.nsmallest(limit,top+[(float(score),row['id']) for row,score in zip(rows,scores)],key=lambda item:(-item[0],item[1]))
            scored+=len(rows)
        if scored!=self.count:raise ValueError('固定范围的向量索引尚不完整；使用关键词检索')
        self.metrics['vector_rows_scored']=scored
        return [{'id':cid,'score':score,'rank':i+1} for i,(score,cid) in enumerate(top)]
