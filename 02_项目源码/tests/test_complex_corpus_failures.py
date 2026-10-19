"""Real local originals/indexes; no model/API calls or long-document reprocessing."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.retrieval import EmbeddingError, RetrievalService
from backend.retrieval.library_search import search_library
from backend.retrieval.service import StaleIndexError
from backend.service import Service
from evaluation import complex_corpus, complex_retrieval, core


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def test_job_recovery_never_resumes_excluded_manifest_sources():
    kept = dict(id='new-kept', source_id='kept', status='queued')
    stale = dict(id='old-excluded', source_id='excluded', status='queued')
    recovered = dict(id='old-kept', source_id='kept', status='queued')
    done = dict(id='done-kept', source_id='kept', status='completed')
    selected = complex_corpus.manifest_jobs([kept, stale], [kept, stale, recovered, done], {'kept'})
    assert [j['id'] for j in selected] == ['new-kept', 'old-kept']
    assert complex_corpus.manifest_jobs([], [stale], set()) == []


@pytest.fixture
def corpus(tmp_path):
    product = Service(tmp_path / 'runtime', tmp_path / 'samples', workers=False)
    product.start()
    project_id = product.store.create_project('Local test corpus', '')['id']
    rows, sources = [], []
    for identifier in ('bad', 'good'):
        text = 'pump inspection ' + identifier
        original = tmp_path / (identifier + '.txt')
        original.write_text(text)
        staged = product.root / 'incoming' / identifier
        staged.write_text(text)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        imported = product.import_staged(project_id, [dict(name=original.name, path=staged,
            format='txt', bytes=staged.stat().st_size, sha256=digest)])
        product.process_job(imported['jobs'][0]['id'])
        sources.append(product.store.get('sources', imported['sources'][0]['id']))
        rows.append(dict(id=identifier, file=original.name, sha256=digest, title=identifier,
            series='same-series', pages=1, redistribution='included_with_attribution'))
    manifest = tmp_path / 'manifest.json'
    dump(manifest, {'files': rows})
    dump(product.root / 'complex-corpus-project.json', {'project_id': project_id,
        'purpose': 'complex-corpus-offline-v1'})
    try:
        yield product, rows, sources, manifest
    finally:
        product.close()


@pytest.mark.parametrize('all_fail', [False, True])
def test_preparation_failures_keep_all_48_questions_and_page_denominator(corpus, monkeypatch, tmp_path, all_fail):
    product, rows, sources, manifest = corpus
    original_prepare = RetrievalService.prepare
    def fail_selected(self, source, *args, **kwargs):
        if all_fail or source['name'] == 'bad.txt':
            raise OSError('simulated index preparation failure')
        return original_prepare(self, source, *args, **kwargs)
    monkeypatch.setattr(RetrievalService, 'prepare', fail_selected)
    suite = {'sources': rows, 'cases': [dict(id=f'q{i}', question='pump inspection',
        source_ids=['bad' if i < 24 else 'good']) for i in range(48)]}
    suite_path, gold_path = tmp_path / 'suite.json', tmp_path / 'gold.json'
    dump(suite_path, suite)
    dump(gold_path, {'suite_sha256': complex_corpus.sha256(suite_path), 'cases': {
        case['id']: {'evidence': [{'source_id': case['source_ids'][0], 'physical_page': 1,
                                  'exact_quote': 'pump inspection'}]} for case in suite['cases']}})
    monkeypatch.setattr(core, 'load_suite', lambda _: suite)
    output = tmp_path / 'output.json'
    complex_retrieval.run(SimpleNamespace(manifest=manifest, runtime=product.root,
        suite=suite_path, gold=gold_path, embedding=False, output=output))
    result = json.loads(output.read_text())
    assert result['status'] == 'complete' and result['case_count'] == 48
    assert len(result['cases']) == 48
    assert len(result['preparation_errors']) == (2 if all_fail else 1)
    assert result['indexed_document_count'] == (0 if all_fail else 1)
    for scope in ('all_documents', 'same_series'):
        summary = result['summary'][scope]['10']
        assert summary['cases'] == 48 and summary['annotated_pages'] == 48
        assert summary['matched_annotated_pages'] == (0 if all_fail else 24)
        assert all(not c['scopes'][scope]['audit']['library_complete'] for c in result['cases'])
    assert result['model_chat_api_calls'] == 0


def test_missing_local_model_manifest_downgrades_explicitly(monkeypatch, tmp_path):
    import backend.retrieval
    def fail(*args, **kwargs):
        raise EmbeddingError('missing local model manifest')
    monkeypatch.setattr(backend.retrieval, 'LocalE5', fail)
    retrieval, info = complex_corpus.make_retrieval(tmp_path, True)
    assert retrieval.embedder is None
    assert info == {'embedding_requested': True, 'embedding': 'none',
                    'embedding_initialization_error': 'local_embedding_initialization_failed'}


def test_runtime_original_mutation_is_rejected_before_prepare(corpus, tmp_path):
    _, _, sources, _ = corpus
    original = Path(sources[0]['original_path'])
    original.chmod(0o600)
    original.write_text('altered source')
    with pytest.raises(complex_corpus.RuntimeOriginalError):
        complex_corpus.prepare_verified_source(RetrievalService(tmp_path / 'indexes'), sources[0], sources[0]['project_id'])


def test_runtime_original_mutation_after_prepare_cannot_publish_old_quotes(corpus, tmp_path):
    product, _, sources, _ = corpus
    source = sources[0]
    handle = complex_corpus.prepare_verified_source(RetrievalService(tmp_path / 'indexes'), source,
        source['project_id'], store=product.store)
    original = Path(source['original_path'])
    original.chmod(0o600)
    original.write_text('changed original without changing the Store metadata')
    with pytest.raises(StaleIndexError):
        search_library([handle], 'pump inspection')


def test_runtime_original_mutation_during_ranking_cannot_publish(corpus, tmp_path, monkeypatch):
    import backend.retrieval.library_search as library
    _, _, sources, _ = corpus
    source = sources[0]
    handle = complex_corpus.prepare_verified_source(RetrievalService(tmp_path / 'indexes'), source, source['project_id'])
    original_bm25 = library.bm25
    def mutate(*args, **kwargs):
        ranking = original_bm25(*args, **kwargs)
        original = Path(source['original_path'])
        original.chmod(0o600)
        original.write_text('changed while candidates were ranked')
        return ranking
    monkeypatch.setattr(library, 'bm25', mutate)
    with pytest.raises(StaleIndexError):
        search_library([handle], 'pump inspection')


def test_summary_keeps_failed_and_missing_source_rows(corpus, tmp_path):
    _, rows, sources, _ = corpus
    class FailedIndex:
        def prepare(self, *args, **kwargs):
            raise OSError('index unavailable')
    broken = complex_corpus.summarize_source(rows[0], sources[0], FailedIndex())
    missing = complex_corpus.summarize_source(rows[1], None, FailedIndex())
    assert broken['id'] == 'bad' and broken['page_entries'] == 1
    assert broken['preparation_error'] == 'OSError' and not broken['index_available']
    assert not broken['complete_processing']
    assert missing['id'] == 'good' and missing['pending_pages'] == [1]
    assert not missing['complete_processing'] and missing['preparation_error'] == 'source_missing'


def test_corpus_serve_opens_only_marked_runtime_and_reuses_cli_index(corpus, monkeypatch):
    from fastapi.testclient import TestClient
    import backend.answer_service
    def forbidden(*args, **kwargs):
        raise AssertionError('opening corpus UI must not call a model API')
    monkeypatch.setattr(backend.answer_service, 'ModelGateway', forbidden)
    product, _, _, _ = corpus
    product.close()  # build exits before serve acquires the runtime's exclusive lock
    args = SimpleNamespace(runtime=product.root, embedding=False)
    app = complex_corpus.create_corpus_app(args)
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
        projects = client.get('/api/projects').json()
        assert any(p['id'] == json.loads((product.root / 'complex-corpus-project.json').read_text())['project_id'] for p in projects)
        retrieval = app.state.answers.retrieval_service()
        assert retrieval.store.path.parent == product.root / 'indexes-lexical'
    (product.root / 'complex-corpus-project.json').unlink()
    with pytest.raises(ValueError, match='独立运行目录'):
        complex_corpus.create_corpus_app(args)
