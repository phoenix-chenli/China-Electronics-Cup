"""Offline evidence-page retrieval on frozen real-document questions.

Gold annotations are read only by the scorer. They never enter the index,
embedding input, question text or candidate selection. A hit is not a correct
answer, and the annotations do not enumerate all possibly relevant pages.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .complex_corpus import (ROOT, DEFAULT_MANIFEST, load_manifest, sha256, write_json,
                             make_retrieval, prepare_verified_source)


def page_metrics(results, expected, k):
    """Top-k chunks, deduplicated for source/physical-page recall."""
    found = {(r['source_sha256'], r['page']) for r in results[:k]}
    expected = set(expected)
    matched = found & expected
    return {'k_chunks': k, 'expected_pages': len(expected), 'matched_pages': len(matched),
            'hit': bool(matched), 'all_annotated_pages_found': bool(expected) and matched == expected,
            'annotated_page_recall': len(matched) / len(expected) if expected else None}


def compact(text):
    return re.sub(r'\s+', '', text or '')


def open_library(args):
    from backend.store import Store
    manifest = load_manifest(args.manifest)
    state = json.loads((args.runtime / 'complex-corpus-project.json').read_text())
    store = Store(args.runtime)
    rows = {r['sha256']: r for r in manifest['files']}
    all_sources = {s['sha256']: s for s in store.sources(state['project_id']) if s['sha256'] in rows}
    retrieval, embedding_info = make_retrieval(args.runtime, args.embedding)
    handles, errors = [], []
    for row in manifest['files']:
        source = all_sources.get(row['sha256'])
        if not source or source['status'] not in {'ready', 'attention'}:
            errors.append({'id': row['id'], 'source_sha256': row['sha256'],
                           'error_type': 'source_not_imported' if not source else 'source_not_ready'})
            continue
        try:
            handle = prepare_verified_source(retrieval, source, state['project_id'],
                store=store)
        except Exception as exc:
            errors.append({'id': row['id'], 'source_sha256': row['sha256'], 'error_type': type(exc).__name__})
            print(json.dumps({'index_unavailable': row['id'], 'error_type': type(exc).__name__}, ensure_ascii=False), flush=True)
            continue
        handles.append(handle)
        print(json.dumps({'indexed': row['id'], 'cache_hit': handle.cache_hit,
                          'audit': handle.audit()}, ensure_ascii=False), flush=True)
    return manifest, rows, all_sources, handles, {**embedding_info, 'preparation_errors': errors}


def run(args):
    from .core import load_suite, code_digest
    from backend.retrieval.library_search import search_library
    suite = load_suite(args.suite)
    gold = json.loads(args.gold.read_text())
    if gold['suite_sha256'] != sha256(args.suite):
        raise ValueError('问题与评分文件摘要不对应')
    manifest, by_sha, sources, handles, preparation = open_library(args)
    allowed = {r['id']: r for r in manifest['files']}
    # All input sources must be exactly the frozen corpus, not a selected easy subset.
    if {(r['id'], r['sha256']) for r in suite['sources']} != {(r['id'], r['sha256']) for r in manifest['files']}:
        raise ValueError('专项输入与真实资料清单不一致')
    output = {'schema_version': 'zhiheng.real-corpus-retrieval.v1', 'started_at': datetime.now(timezone.utc).isoformat(),
              'suite_sha256': sha256(args.suite), 'gold_sha256': sha256(args.gold),
              'manifest_sha256': manifest['_loaded_sha256'], 'backend_code_sha256': code_digest(ROOT),
              'scorer_sha256': sha256(Path(__file__)), 'status': 'running', 'cases': [],
              'model_chat_api_calls': 0, 'answer_accuracy': None, 'answer_recall': None,
              **preparation,
              'query_language': 'zh', 'original_document_language': 'en',
              'scopes': ['all_documents', 'same_series'],
              'boundary': '预先冻结的中文开发验收问题；衡量标注证据页召回，不评价生成答案或全量相关页精确率。'}
    write_json(args.output, output)
    for case in suite['cases']:
        expected = gold['cases'][case['id']]
        annotations = expected['evidence']
        pages = {(allowed[e['source_id']]['sha256'], e['physical_page']) for e in annotations}
        series = {allowed[sid]['series'] for sid in case['source_ids']}
        entry = {'id': case['id'], 'question': case['question'], 'source_ids': case['source_ids'],
                 'tags': case.get('tags', []), 'expected_physical_pages': [list(p) for p in sorted(pages)],
                 'scopes': {}, 'parsed_quote_checks': []}
        for scope in ('all_documents', 'same_series'):
            selected = handles if scope == 'all_documents' else [h for h in handles if by_sha[h.source['sha256']]['series'] in series]
            required = set(by_sha) if scope == 'all_documents' else {r['sha256'] for r in manifest['files'] if r['series'] in series}
            missing = required - {h.source['sha256'] for h in selected}
            started = time.monotonic()
            try:
                result = search_library(selected, case['question'], limit=10, mode='hybrid' if args.embedding else 'lexical')
                result['audit']['unavailable_selected_sha256'] = sorted(missing)
                if missing:
                    result['audit']['library_complete'] = False
                entry['scopes'][scope] = {'metrics': {str(k): page_metrics(result['results'], pages, k) for k in (5, 10)},
                                          'results': result['results'], 'audit': result['audit']}
            except Exception as exc:
                # Preserve the complete denominator and a visible failure, without provider/private error bodies.
                entry['scopes'][scope] = {'error_type': type(exc).__name__,
                                          'audit': {'library_complete': False,
                                              'unavailable_selected_sha256': sorted(missing),
                                              'source_count': len(selected)},
                                          'metrics': {str(k): page_metrics([], pages, k) for k in (5, 10)}}
            entry['scopes'][scope]['elapsed_seconds'] = round(time.monotonic() - started, 3)
        for e in annotations:
            source = sources.get(allowed[e['source_id']]['sha256'])
            page = next((p for p in (source or {}).get('pages', []) if p.get('number') == e['physical_page']), None)
            exact = e.get('exact_quote', '')
            visual = bool(e.get('visual_annotation')) or e.get('evidence_type') in {'visual', 'visual_annotation', 'spatial', 'image'} or e.get('quote_matching') in {'visual', 'manual_visual'}
            entry['parsed_quote_checks'].append({'source_id': e['source_id'], 'physical_page': e['physical_page'],
                'parsed_page_present': page is not None, 'page_nonempty': bool(page and page.get('text', '').strip()),
                'annotation_kind': 'visual' if visual else 'text',
                'whitespace_normalized_quote_retained': None if not exact else compact(exact) in compact((page or {}).get('text', '')),
                'text_quote_required_for_annotation': not visual,
                'boundary': '只检验转录锚点保留，不等于图表、公式语义理解正确'})
        output['cases'].append(entry)
        write_json(args.output, output)
        print(json.dumps({'case': case['id'], 'all_top10': entry['scopes']['all_documents']['metrics']['10']}, ensure_ascii=False), flush=True)
    summaries = {}
    for scope in output['scopes']:
        summaries[scope] = {}
        for k in ('5', '10'):
            values = [c['scopes'][scope]['metrics'][k] for c in output['cases']]
            total = sum(v['expected_pages'] for v in values)
            summaries[scope][k] = {'cases': len(values), 'cases_with_any_evidence_page': sum(v['hit'] for v in values),
                'cases_with_all_annotated_pages': sum(v['all_annotated_pages_found'] for v in values),
                'case_hit_rate': sum(v['hit'] for v in values)/len(values),
                'annotated_pages': total, 'matched_annotated_pages': sum(v['matched_pages'] for v in values),
                'micro_annotated_page_recall': sum(v['matched_pages'] for v in values)/total if total else None,
                'macro_annotated_page_recall': sum(v['annotated_page_recall'] or 0 for v in values)/len(values)}
    output.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat(), summary=summaries,
                  case_count=len(output['cases']), indexed_document_count=len(handles),
                  required_document_count=len(manifest['files']))
    write_json(args.output, output)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['index', 'run'])
    p.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    p.add_argument('--runtime', type=Path, default=ROOT / '.runtime-complex-corpus')
    p.add_argument('--suite', type=Path, default=DEFAULT_MANIFEST.parent / '专项输入.json')
    p.add_argument('--gold', type=Path, default=ROOT / '评测材料/复杂文档专项/离线评分/参考答案.json')
    p.add_argument('--output', type=Path, default=ROOT / '评测材料/复杂文档专项/专项检索结果.json')
    p.add_argument('--embedding', action='store_true')
    args = p.parse_args()
    if args.command == 'index':
        manifest, _, _, handles, preparation = open_library(args)
        print(json.dumps({'indexed_documents': len(handles), 'required_documents': len(manifest['files']),
                          'model_chat_api_calls': 0, **preparation}))
    else:
        run(args)


if __name__ == '__main__':
    main()
