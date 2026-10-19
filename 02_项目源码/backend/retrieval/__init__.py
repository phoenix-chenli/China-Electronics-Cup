"""Offline real embeddings with versioned, project-scoped retrieval indexes."""
from .embeddings import LocalE5, EmbeddingError
from .service import RetrievalService, PreparedRetrieval, StaleIndexError
from .rerank import ExactContextReranker

__all__ = ['LocalE5', 'EmbeddingError', 'RetrievalService', 'PreparedRetrieval', 'StaleIndexError', 'ExactContextReranker']
