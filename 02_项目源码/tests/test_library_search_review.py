"""Local library scan/authorization invariants with deterministic test vectors."""
import pytest

from backend.retrieval import RetrievalService
from backend.retrieval.library_search import search_library
from backend.retrieval.service import StaleIndexError


class DeterministicVectors:
    identity = {'model': 'test-only-vectors', 'dimension': 2}

    def __init__(self, on_query=lambda: None):
        self.on_query = on_query

    def embed(self, texts, *, kind, cancelled):
        if kind == 'query':
            self.on_query()
        return [[0.0, 1.0] if 'TAIL_TARGET' in text else [1.0, 0.0] for text in texts]


def source(identifier, pages):
    return {'id': identifier, 'project_id': 'p', 'name': identifier + '.pdf',
            'sha256': identifier * 16, 'parser_version': 'fixture', 'status': 'ready',
            'metadata': {'page_unit': 'physical_page'}, 'warnings': [], 'pages': pages}


def test_vector_library_scans_all_blocks_and_retains_last_source_offsets(tmp_path):
    service = RetrievalService(tmp_path, DeterministicVectors())
    sources = []
    for document in range(3):
        pages = [{'number': page, 'text': f'  Document {document} page {page} ordinary passage.  '}
                 for page in range(1, 301)]
        if document == 2:
            pages[-1]['text'] = '  TAIL_TARGET precise source value 12.50  '
        sources.append(source(str(document), pages))
    handles = [service.prepare(item, 'p') for item in sources]
    result = search_library(handles, 'TAIL_TARGET', mode='vector', limit=1)
    assert result['audit']['vector_rows_scanned'] == 900
    assert result['audit']['indexed_chunks'] == 900
    row = result['results'][0]
    assert row['source_id'] == '2' and row['page'] == 300
    assert sources[2]['pages'][299]['text'][row['start']:row['end']] == row['quote']
    assert row['quote'] == 'TAIL_TARGET precise source value 12.50'


def test_live_source_change_during_embedding_blocks_result_publication(tmp_path):
    state = {'current': True}
    service = RetrievalService(tmp_path, DeterministicVectors(lambda: state.update(current=False)))
    handle = service.prepare(source('a', [{'number': 1, 'text': 'TAIL_TARGET'}]), 'p',
                             is_current=lambda: state['current'])
    with pytest.raises(StaleIndexError):
        search_library([handle], 'TAIL_TARGET', mode='vector')
