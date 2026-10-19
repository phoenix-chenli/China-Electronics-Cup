"""Reproduce the real-document library through the product importer; no chat API.

The runtime is separate from the user's ordinary workspace. Gold answers are
never read by this module or added to the library. Local E5 is optional.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / '知识库样本/真实业务文档/资料清单.json'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def load_manifest(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    manifest = json.loads(raw)
    manifest['_loaded_sha256'] = hashlib.sha256(raw).hexdigest()
    seen, hashes = set(), set()
    for row in manifest['files']:
        f = (path.parent / row['file']).resolve()
        if not f.is_relative_to(path.parent) or not f.is_file():
            raise ValueError('资料清单路径无效: ' + row['file'])
        if row['id'] in seen or row['sha256'] in hashes:
            raise ValueError('独立原件清单中存在重复ID或相同字节')
        if sha256(f) != row['sha256']:
            raise ValueError('原件摘要不一致: ' + row['file'])
        if row.get('redistribution') != 'included_with_attribution':
            raise ValueError('本建库入口只接收清单中已标记可随包提供的原件')
        seen.add(row['id']); hashes.add(row['sha256'])
    return manifest


class RuntimeOriginalError(ValueError):
    """The imported original no longer matches the frozen source identity."""


def _file_stamp(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def prepare_verified_source(retrieval, source, project_id, read_current=None, *, store=None):
    """Hash runtime bytes once, then reject any identity/mtime/ctime change.

    Rechecking the file stamp at every handle check avoids hashing the entire
    corpus twice per question. A changed stamp invalidates even restored bytes.
    Neither the original nor the runtime database is mutated here.
    """
    from backend.source_snapshot import snapshot
    from backend.retrieval.service import StaleIndexError
    path = Path(source['original_path'])
    try:
        stamp = _file_stamp(path)
        if sha256(path) != source['sha256'] or _file_stamp(path) != stamp:
            raise RuntimeOriginalError('运行库原件摘要或文件状态已改变')
    except OSError as exc:
        raise RuntimeOriginalError('运行库原件缺失或不可读取') from exc
    if store is not None:
        from backend.source_guard import StoreSourceGuard
        source_guard = StoreSourceGuard(store, source)
        captured = None
    else:
        source_guard = None
        captured = snapshot(source)
    def is_current():
        if source_guard is not None:
            if not source_guard():
                return False
        else:
            current = read_current() if read_current else source
            if not (current and current.get('project_id') == project_id and not current.get('archived')
                    and current.get('status') in {'ready', 'attention'}
                    and current.get('original_path') == str(path) and snapshot(current) == captured):
                return False
        try:
            return _file_stamp(path) == stamp
        except OSError:
            return False
    handle = retrieval.prepare(source, project_id, is_current=is_current)
    if not is_current():
        raise StaleIndexError('索引准备期间资料或原件已改变')
    handle._check()
    return handle


def make_retrieval(runtime, embedding):
    from backend.retrieval import RetrievalService, LocalE5, EmbeddingError
    embedder, error = None, None
    if embedding:
        try:
            embedder = LocalE5(ROOT / 'models/multilingual-e5-small', threads=2)
        except EmbeddingError:
            error = 'local_embedding_initialization_failed'
    service = RetrievalService(Path(runtime) / ('indexes-e5' if embedder else 'indexes-lexical'), embedder)
    return service, {'embedding_requested': bool(embedding),
                     'embedding': 'local-e5' if embedder else 'none',
                     'embedding_initialization_error': error}


def summarize_source(row, source, service, read_current=None, *, store=None):
    if source is None:
        return {'id': row['id'], 'source_id': None, 'title': row['title'], 'series': row['series'],
            'sha256': row['sha256'], 'expected_physical_pages': row['pages'], 'page_entries': 0,
            'status': 'missing', 'complete_processing': False,
            'pending_pages': list(range(1, row['pages'] + 1)), 'native_pages': 0, 'ocr_pages': 0,
            'nonempty_pages': 0, 'characters': 0, 'max_anchored_heading_level': 0,
            'anchored_headings': 0, 'table_regions': 0, 'marginal_spans': 0, 'excluded_spans': 0,
            'import_batches': {}, 'warnings': [], 'retrieval': None,
            'runtime_original_verified': False, 'preparation_error': 'source_missing', 'index_available': False}
    meta = source.get('metadata') or {}
    batches = meta.get('import_batches', {})
    pages = source.get('pages') or []
    pending = batches.get('pending_pages', [])
    page_numbers = [p.get('number') for p in pages]
    complete = (source['status'] in {'ready', 'attention'} and not pending
                and len(page_numbers) == row['pages'] and set(page_numbers) == set(range(1, row['pages'] + 1))
                and batches.get('status') == 'complete' and batches.get('out_of_policy_pages', 0) == 0
                and not meta.get('quality_summary', {}).get('processing_truncated', False))
    structure = meta.get('document_structure') or {}
    headings = structure.get('headings') or []
    indexed, preparation_error, original_verified = None, None, False
    if source['status'] in {'ready', 'attention'} and any(p.get('text', '').strip() for p in pages):
        try:
            handle = prepare_verified_source(service, source, source['project_id'], read_current, store=store)
            indexed = handle.audit()
            handle._check()
            original_verified = True
        except Exception as exc:
            preparation_error = type(exc).__name__
    elif source['status'] not in {'ready', 'attention'}:
        preparation_error = 'source_not_ready'
    else:
        preparation_error = 'source_has_no_searchable_text'
    # The parse claim also needs a valid original; a failed index does not erase
    # page observations, but cannot be called a completely verified source.
    complete = complete and original_verified
    return {'id': row['id'], 'source_id': source['id'], 'title': row['title'], 'series': row['series'],
        'sha256': row['sha256'], 'expected_physical_pages': row['pages'], 'page_entries': len(pages),
        'status': source['status'], 'complete_processing': complete, 'pending_pages': pending,
        'native_pages': sum(p.get('extraction') == 'native' for p in pages),
        'ocr_pages': sum(p.get('extraction') == 'ocr' for p in pages),
        'nonempty_pages': sum(bool(p.get('text', '').strip()) for p in pages),
        'characters': sum(len(p.get('text', '')) for p in pages),
        'max_anchored_heading_level': max((h.get('level', 0) for h in headings), default=0),
        'anchored_headings': len(headings), 'table_regions': len(meta.get('table_regions') or []),
        'marginal_spans': sum(s.get('role') in {'repeated_header', 'repeated_footer', 'declared_page_label'}
                              for s in structure.get('excluded_spans') or []),
        'excluded_spans': len(structure.get('excluded_spans') or []),
        'import_batches': batches, 'warnings': source.get('warnings', []), 'retrieval': indexed,
        'runtime_original_verified': original_verified, 'preparation_error': preparation_error,
        'index_available': indexed is not None}


def manifest_jobs(new_jobs, recovered_jobs, allowed_source_ids):
    """Resume only sources imported or matched from this manifest's originals."""
    selected, seen = [], set()
    for job in [*new_jobs, *recovered_jobs]:
        if (job['source_id'] in allowed_source_ids and job['status'] == 'queued'
                and job['id'] not in seen):
            selected.append(job)
            seen.add(job['id'])
    return selected


def build(args):
    from backend.service import Service, file_format
    from backend.store import identity
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / 'complex-corpus-project.json'
    if (runtime / 'workspace.sqlite').exists() and not marker.exists():
        raise ValueError('目标已有其他工作区，请为专项建库选择单独的空目录')
    product = Service(runtime, ROOT / 'samples', workers=False)
    product.start()
    started = time.monotonic()
    try:
        if marker.exists():
            state = json.loads(marker.read_text())
            project_id = state['project_id']
        else:
            project = product.store.create_project('真实复杂业务资料库', '公开原件离线解析与检索专项，真实问答待API恢复')
            project_id = project['id']
            write_json(marker, {'project_id': project_id, 'purpose': 'complex-corpus-offline-v1'})
        jobs, allowed_source_ids = [], set()
        for row in manifest['files']:
            original = manifest_path.parent / row['file']
            staged = runtime / 'incoming' / identity()
            shutil.copyfile(original, staged)
            result = product.import_staged(project_id, [{'name': original.name, 'path': staged,
                'format': file_format(original.name), 'bytes': original.stat().st_size, 'sha256': row['sha256'],
                'metadata': {'origin': row['url'], 'source_group': row['source_group'],
                    'license': row['license'], 'corpus_id': row['id'], 'series': row['series'],
                    'quality_notes': ['真实公开技术原件；非项目自编材料。保留原件版本与使用条件。']}}])
            jobs.extend(result['jobs'])
            allowed_source_ids.update(s['id'] for s in [*result['sources'], *result['duplicates']])
            for duplicate in result['duplicates']:
                source = product.store.get('sources', duplicate['id'])
                from backend.parsers import PARSER_VERSION
                batches = source.get('metadata', {}).get('import_batches', {})
                if (source['status'] in {'ready', 'failed', 'attention'} and
                    (source['status'] == 'failed' or source['parser_version'] != PARSER_VERSION or batches.get('pending_pages'))):
                    _, job = product.store.retry(source['id'])
                    jobs.append(job)
        jobs = manifest_jobs(jobs, product.store.jobs(project_id), allowed_source_ids)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(product.process_job, j['id']): j for j in jobs}
            for future in as_completed(futures):
                future.result()
                job = futures[future]
                source = product.store.get('sources', job['source_id'])
                batches = source.get('metadata', {}).get('import_batches', {})
                print(json.dumps({'source': source['name'], 'status': source['status'], 'pages': source['page_count'],
                    'pending': len(batches.get('pending_pages', []))}, ensure_ascii=False), flush=True)
        retrieval, embedding_info = make_retrieval(runtime, args.embedding)
        live = {s['sha256']: s for s in product.store.sources(project_id)}
        rows = []
        for row in manifest['files']:
            source = live.get(row['sha256'])
            rows.append(summarize_source(row, source, retrieval,
                store=product.store))
            print(json.dumps({'indexed': row['id'], 'chunks': (rows[-1]['retrieval'] or {}).get('coverage', {}).get('indexed_page_count'),
                              'complete': rows[-1]['complete_processing']}, ensure_ascii=False), flush=True)
            write_json(args.output, {'status': 'running', 'sources': rows, 'model_chat_api_calls': 0})
        summary = {'schema_version': 'zhiheng.real-corpus-offline.v1', 'manifest_sha256': manifest['_loaded_sha256'],
            'project_id': project_id, 'parser_version': __import__('backend.parsers', fromlist=['PARSER_VERSION']).PARSER_VERSION,
            'documents': len(rows), 'physical_pages': sum(r['expected_physical_pages'] for r in rows),
            'documents_over_200_pages': sum(r['expected_physical_pages'] > 200 for r in rows),
            'complete_processing_documents': sum(r['complete_processing'] for r in rows),
            'elapsed_seconds': round(time.monotonic() - started, 2), 'sources': rows,
            'elapsed_scope': '本次命令耗时，包含复用已完成解析；不是全库从零解析的性能基准',
            'model_chat_api_calls': 0, **embedding_info,
            'indexed_documents': sum(r['index_available'] for r in rows),
            'preparation_errors': [{'id': r['id'], 'error_type': r['preparation_error']}
                                   for r in rows if r['preparation_error']],
            'answer_accuracy': None, 'answer_recall': None,
            'boundary': '原件完整性、实际产品解析与本地索引结果；没有生成模型答案，不是问答成绩。'}
        write_json(args.output, summary)
        print(json.dumps({k:v for k,v in summary.items() if k != 'sources'}, ensure_ascii=False), flush=True)
    finally:
        product.close()


def search(args):
    from backend.store import Store
    from backend.retrieval.library_search import search_library
    state = json.loads((args.runtime / 'complex-corpus-project.json').read_text())
    manifest = load_manifest(args.manifest)
    allowed = {r['sha256'] for r in manifest['files'] if not args.series or r['series'] == args.series}
    store = Store(args.runtime)
    selected = [s for s in store.sources(state['project_id']) if s['sha256'] in allowed]
    sources = [s for s in selected if s['status'] in {'ready', 'attention'}]
    service, embedding_info = make_retrieval(args.runtime, args.embedding)
    handles, errors = [], []
    for source in sources:
        try:
            handles.append(prepare_verified_source(service, source, state['project_id'],
                store=store))
        except Exception as exc:
            errors.append({'source_sha256': source['sha256'], 'error_type': type(exc).__name__})
    result = search_library(handles, args.question, limit=args.limit, mode='hybrid' if args.embedding else 'lexical')
    missing = allowed - {h.source['sha256'] for h in handles}
    result['audit']['unavailable_selected_sha256'] = sorted(missing)
    result['audit'].update(embedding_info, preparation_errors=errors)
    if missing:
        result['audit']['library_complete'] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))


def create_corpus_app(args):
    """Open only an explicitly marked offline corpus; never the normal runtime."""
    from backend.main import create_app
    from backend.store import Store
    runtime = args.runtime.resolve()
    if runtime == (ROOT / '.runtime').resolve():
        raise ValueError('专项入口不能使用普通运行目录，请先在独立目录执行build')
    marker = runtime / 'complex-corpus-project.json'
    if not marker.is_file() or not (runtime / 'workspace.sqlite').is_file():
        raise ValueError('请先在此独立运行目录完成专项build')
    state = json.loads(marker.read_text(encoding='utf-8'))
    if state.get('purpose') != 'complex-corpus-offline-v1' or not state.get('project_id'):
        raise ValueError('专项运行目录标记无效')
    if not Store(runtime).get('projects', state['project_id']):
        raise ValueError('专项标记对应的项目不存在')
    empty_samples = runtime / '.complex-empty-samples'
    empty_samples.mkdir(exist_ok=True)
    if any(empty_samples.iterdir()):
        raise ValueError('专项空样本目录必须保持为空')
    return create_app(data_dir=runtime, samples_dir=empty_samples,
        retrieval_factory=lambda _unused: make_retrieval(runtime, args.embedding)[0])


def serve(args):
    import uvicorn
    application = create_corpus_app(args)
    print(f'真实资料库： http://127.0.0.1:{args.port} ；停止服务请按Ctrl+C。', flush=True)
    uvicorn.run(application, host='127.0.0.1', port=args.port, log_level='info')


def valid_port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('端口必须为1024至65535的整数') from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError('端口必须在1024至65535之间')
    return port


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['build', 'search', 'check', 'serve'])
    p.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    p.add_argument('--runtime', type=Path, default=ROOT / '.runtime-complex-corpus')
    p.add_argument('--output', type=Path, default=ROOT / '评测材料/复杂文档专项/实际解析与索引结果.json')
    p.add_argument('--workers', type=int, choices=[1,2], default=2)
    p.add_argument('--embedding', action='store_true', help='使用已随包提供的本地E5，不调用远程模型')
    p.add_argument('--series', help='仅检索指定同类资料系列')
    p.add_argument('--question', default='')
    p.add_argument('--limit', type=int, default=10)
    p.add_argument('--port', type=valid_port, default=8877, help='serve仅监听127.0.0.1，默认8877')
    args = p.parse_args()
    if args.command == 'build': build(args)
    elif args.command == 'search': search(args)
    elif args.command == 'serve': serve(args)
    else:
        manifest = load_manifest(args.manifest)
        print(json.dumps({'verified_originals': len(manifest['files']), 'model_chat_api_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
