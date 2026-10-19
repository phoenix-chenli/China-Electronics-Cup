"""Export one auditable row per expected physical page, without model calls."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

from .complex_corpus import ROOT, DEFAULT_MANIFEST, load_manifest, prepare_verified_source, make_retrieval, write_json


def run(args):
    from backend.store import Store
    manifest = load_manifest(args.manifest)
    state = json.loads((args.runtime / 'complex-corpus-project.json').read_text())
    store = Store(args.runtime)
    sources = {s['sha256']: s for s in store.sources(state['project_id'])}
    service, preparation = make_retrieval(args.runtime, args.embedding)
    fields = ['原件编号', '原件SHA256', '物理页', '状态', '同页条目数', '印刷页标签', '提取方式',
              '正文字符数', '正文SHA256', '含图', '正文区块数', '索引片段数', '向量片段数', '索引版本']
    rows, summaries = [], []
    for original in manifest['files']:
        source = sources.get(original['sha256'])
        pages = (source or {}).get('pages', [])
        counts = Counter(p['number'] for p in pages)
        by_page = {p['number']: p for p in pages}
        indexed, vectorized, generation, error = Counter(), Counter(), '', None
        handle = None
        try:
            if not source:
                raise ValueError('source_missing')
            handle = prepare_verified_source(service, source, state['project_id'],
                store=store)
            generation = handle.generation
            with service.store.connect() as db:
                for c in db.execute('SELECT payload,vector IS NOT NULL AS has_vector FROM chunks WHERE generation=?', (generation,)):
                    page = json.loads(c['payload'])['page']
                    indexed[page] += 1
                    vectorized[page] += c['has_vector']
            handle._check()
        except Exception as exc:
            error = type(exc).__name__
            indexed, vectorized, generation = Counter(), Counter(), ''
        source_rows = []
        for number in range(1, original['pages'] + 1):
            page = by_page.get(number)
            text = (page or {}).get('text', '')
            status = ('original_or_index_unavailable' if error else 'duplicate_page' if counts[number] > 1
                      else 'not_parsed' if page is None else 'parsed_with_text' if text.strip() else 'parsed_without_text')
            source_rows.append([original['id'], original['sha256'], number, status, counts[number],
                (page or {}).get('printed_page_label', ''), (page or {}).get('extraction', ''), len(text),
                hashlib.sha256(text.encode()).hexdigest() if page is not None else '',
                bool((page or {}).get('has_image')), len((page or {}).get('blocks', [])),
                indexed[number], vectorized[number], generation])
        if handle and not error:
            handle._check()
        rows.extend(source_rows)
        summaries.append({'id': original['id'], 'expected_pages': original['pages'], 'page_rows': len(source_rows),
            'parsed_page_entries': len(pages), 'nonempty_pages': sum(bool(p.get('text', '').strip()) for p in pages),
            'indexed_pages': len(indexed), 'indexed_chunks': sum(indexed.values()), 'vector_chunks': sum(vectorized.values()),
            'duplicate_pages': sorted(n for n,v in counts.items() if v > 1),
            'out_of_range_pages': sorted(n for n in counts if n not in range(1, original['pages'] + 1)),
            'preparation_error': error, 'index_generation': generation})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        writer.writerows(rows)
    summary = {'manifest_sha256': manifest['_loaded_sha256'], 'csv_sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
               'physical_page_rows': len(rows), 'documents': len(summaries), 'sources': summaries,
               'model_chat_api_calls': 0, **preparation,
               'boundary': '每个应处理物理页一行；空白不制造正文。字符和片段数量不代表识别正确率。'}
    write_json(args.output.with_suffix('.json'), summary)
    print(json.dumps({k:v for k,v in summary.items() if k != 'sources'}, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    p.add_argument('--runtime', type=Path, default=ROOT / '.runtime-complex-corpus')
    p.add_argument('--output', type=Path, default=ROOT / '评测材料/复杂文档专项/逐页解析覆盖.csv')
    p.add_argument('--embedding', action='store_true')
    run(p.parse_args())


if __name__ == '__main__':
    main()
