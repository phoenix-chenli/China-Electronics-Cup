"""Library authorization, same-text identity and complete retrieval invariants."""
import pytest
from backend.retrieval import RetrievalService
from backend.retrieval.library_search import search_library
from backend.retrieval.service import StaleIndexError


def source(identifier, text, project='p'):
    return {'id': identifier, 'project_id': project, 'name': identifier + '.txt',
            'sha256': identifier * 8, 'parser_version': 'fixture', 'status': 'ready',
            'metadata': {}, 'warnings': [], 'pages': [{'number': 1, 'text': text}]}


def test_library_search_keeps_source_identity_for_same_page_and_text(tmp_path):
    service = RetrievalService(tmp_path)
    a = service.prepare(source('a', 'pump inspection interval is monthly'), 'p')
    b = service.prepare(source('b', 'pump inspection interval is monthly'), 'p')
    result = search_library([a, b], 'pump inspection interval', limit=5)
    assert {r['source_id'] for r in result['results']} == {'a', 'b'}
    assert result['audit']['source_count'] == 2
    assert result['audit']['indexed_chunks'] == 2
    assert all(r['quote'] == 'pump inspection interval is monthly' for r in result['results'])


def test_library_search_cannot_cross_project_or_use_invalidated_source(tmp_path):
    service = RetrievalService(tmp_path)
    a = service.prepare(source('a', 'seepage drainage'), 'p')
    b = service.prepare(source('b', 'seepage drainage', 'q'), 'q')
    with pytest.raises(PermissionError):
        search_library([a, b], 'seepage')
    service.invalidate('p', 'a')
    with pytest.raises(StaleIndexError):
        search_library([a], 'seepage')


def test_library_search_never_adds_unselected_sources_and_discloses_fallback(tmp_path):
    service = RetrievalService(tmp_path)
    a = service.prepare(source('a', 'pump maintenance'), 'p')
    service.prepare(source('hidden', 'secret pump maintenance'), 'p')
    result = search_library([a], 'pump', mode='hybrid')
    assert {r['source_id'] for r in result['results']} == {'a'}
    assert result['audit']['mode_used'] == 'lexical'
    assert result['audit']['fallback_reason'] == 'selected_library_vectors_incomplete'
    assert search_library([a], 'unmatchedword')['results'] == []


def test_library_search_more_than_eight_documents_and_tail_page(tmp_path):
    service = RetrievalService(tmp_path)
    handles = [service.prepare(source(str(i), 'standard installation procedure'), 'p') for i in range(12)]
    last = source('tail', 'first chapter')
    last['pages'].append({'number': 225, 'text': 'Special drainage inspection interval is fourteen days'})
    handles.append(service.prepare(last, 'p'))
    result = search_library(handles, 'Special drainage inspection fourteen', limit=5)
    assert result['results'][0]['page'] == 225
    assert result['results'][0]['source_id'] == 'tail'
    assert result['audit']['source_count'] == 13


def test_library_search_discloses_unprocessed_pages(tmp_path):
    service = RetrievalService(tmp_path)
    original = source('partial', 'pump maintenance')
    original['format'] = 'pdf'
    original['metadata'] = {'import_batches': {'status': 'partial', 'pending_pages': [201]},
                            'quality_summary': {'processing_truncated': True}}
    result = search_library([service.prepare(original, 'p')], 'pump')
    assert result['audit']['library_complete'] is False
    assert result['audit']['source_coverage'][0]['pending_pages'] == [201]
    assert result['results'][0]['page_unit'] == 'physical_page'
    assert result['results'][0]['source_sha256'] == original['sha256']


def test_bare_numbers_do_not_displace_labelled_evidence(tmp_path):
    service = RetrievalService(tmp_path)
    bare = service.prepare(source('bare', '111'), 'p')
    labelled = service.prepare(source('labelled', 'training duration is 111 hours'), 'p')
    result = search_library([bare, labelled], '111', limit=1)
    assert result['results'][0]['source_id'] == 'labelled'
    assert result['audit']['numeric_only_candidates_skipped'] == 1
    assert search_library([bare], '111')['results'] == []
    assert bare.audit()['coverage']['indexed_page_count'] == 1
