"""Rank fusion and exact blocked cosine scan; all authorized rows are scanned."""
from __future__ import annotations

import heapq
import math
import re
from collections import Counter

from .embeddings import check_cancelled


def terms(text):
    result = Counter(re.findall(r'[a-z0-9]+(?:[.][0-9]+)?', text.lower()))
    for word in re.findall(r'[\u3400-\u9fff]+', text):
        for size in (2, 3):
            result.update(word[i:i + size] for i in range(len(word) - size + 1))
    for term in ('什么', '哪些', '多少', '如何', '是否', '请问', '文档', '资料', '一下', '根据'):
        result.pop(term, None)
    return result


def rrf(rankings, *, k=60):
    scores = {}
    for ranking in rankings:
        for rank, (identifier, _) in enumerate(ranking, 1):
            scores[identifier] = scores.get(identifier, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def vector_scan(rows, query, limit, cancelled):
    """Rows are a fetchmany iterator. Never cap by prefix or lexical candidates."""
    import numpy as np
    heap, scanned = [], 0
    query = np.asarray(query, dtype=np.float32)
    for block in rows:
        check_cancelled(cancelled)
        if not block:
            continue
        matrix = np.vstack([np.frombuffer(row['vector'], dtype='<f4') for row in block])
        if matrix.shape[1] != len(query) or not np.isfinite(matrix).all():
            raise ValueError('向量索引维度或数值无效')
        scores = matrix @ query
        for row, score in zip(block, scores):
            item = (float(score), -row['ordinal'], row['chunk_id'])
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
        scanned += len(block)
    return [(item[2], item[0]) for item in sorted(heap, reverse=True)], scanned


def bm25(rows, query, count, average_length):
    scores = {}
    for row in rows:
        term = row['term']
        idf = math.log(1 + (count - row['df'] + .5) / (row['df'] + .5))
        tf = row['tf']
        score = idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * row['length'] / max(1, average_length)))
        scores[row['chunk_id']] = scores.get(row['chunk_id'], 0) + score * min(query[term], 3)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
