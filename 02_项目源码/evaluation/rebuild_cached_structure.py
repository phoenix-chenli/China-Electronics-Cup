"""Rebuild hierarchy from existing parsed pages only; never parse/OCR/embed.

Default is a dry run. --apply updates only structure and its diagnostic profile
inside sources.metadata, in one guarded transaction. The retrieval owner must
rebuild affected indexes afterwards with the new chunker version.
"""
from pathlib import Path
import argparse
import copy
import datetime
import hashlib
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import document_structure as ds, table_semantics as ts
from backend.document_engine import _chunks


def digest(value):
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--annotations-only', action='store_true', help='Only refresh footnotes/units of existing PDF table structures')
    args = parser.parse_args()
    path = args.runtime.resolve() / 'workspace.sqlite'
    connection = sqlite3.connect(f'{path.as_uri()}?mode={"rw" if args.apply else "ro"}', uri=True)
    connection.row_factory = sqlite3.Row
    filter_sql = " AND format='pdf'" if args.annotations_only else ''
    rows = list(connection.execute('SELECT * FROM sources WHERE project_id=? AND archived=0' + filter_sql + ' ORDER BY name', (args.project_id,)))
    if not rows:
        raise SystemExit('No existing active sources matched; nothing changed.')
    changes, records = [], []
    for row in rows:
        metadata = json.loads(row['metadata'])
        source = {'pages': json.loads(row['pages']), 'tables': json.loads(row['tables']), 'metadata': metadata}
        before = copy.deepcopy(metadata.get('document_structure', {}))
        pages_before = digest(source['pages'])
        old_chunks = _chunks(source, lambda: False)[0] if args.annotations_only else None
        annotation_updates = []
        if args.annotations_only:
            regions = {r['table_name']: r for r in metadata.get('table_regions', [])}
            for table in metadata.get('table_semantics', []):
                region = regions.get(table.get('table_name'))
                if row['format'] != 'pdf' or not region:
                    continue
                context = table.get('context', [])
                rebuilt = ts.build_table_semantics(region, format='pdf', source_position=table.get('source_position'),
                    before=[c for c in context if c.get('side') == 'before'],
                    after=[c for c in context if c.get('side') == 'after'],
                    explicit_header_rows=table['header_rows'] if table.get('header_basis') == 'explicit' else ())
                previous = digest({k: table.get(k) for k in ('footnotes', 'units')})
                table.update(footnotes=rebuilt['footnotes'], units=rebuilt['units'])
                if digest({k: table.get(k) for k in ('footnotes', 'units')}) != previous:
                    annotation_updates.append({'table_name': table['table_name'], 'page': table.get('page'),
                                               'footnotes': len(table['footnotes']), 'units': len(table['units'])})
        else:
            metadata['document_structure'] = ds.build(source, format=row['format'])
            metadata['document_profiles'] = ds.profiles(source)
        after = metadata['document_structure']
        assert digest(source['pages']) == pages_before
        chunks, truncated = _chunks(source, lambda: False)
        pages = {p['number']: p for p in source['pages']}
        assert len(pages) == len(source['pages']), 'Duplicate physical page ID'
        assert all(c.text == pages[c.page]['text'][c.start:c.end] and c.id == f'p{c.page}:{c.start}-{c.end}' for c in chunks)
        assert [(c.page, c.start) for c in chunks] == sorted((c.page, c.start) for c in chunks)
        # These pages are an audit selection, not production retrieval rules.
        selected = [339, 340, 341] if 'A01' in row['name'] else [213, 214, 267, 269, 277, 279] if 'B10' in row['name'] else [47, 48, 49] if 'B02' in row['name'] else []
        audit = []
        for page_number in selected:
            audit.append({'physical_page': page_number,
                'before_active_headings': [{'title': h['title'], 'status': h.get('structure_status'), 'anchor_page': h['source_position']['page']}
                    for h in before.get('headings', []) if any(s['page'] == page_number for s in h.get('body_spans', []))],
                'after_chunk_paths': list(dict.fromkeys(tuple(c.heading_path) for c in chunks if c.page == page_number)),
                'after_chunk_ids': [c.id for c in chunks if c.page == page_number]})
        records.append({'source_id': row['id'], 'name': row['name'], 'original_sha256': row['sha256'],
            'physical_pages': len(pages), 'page_payload_sha256': pages_before,
            'before_structure_sha256': digest(before), 'after_structure_sha256': digest(after),
            'before_headings': len(before.get('headings', [])), 'after_headings': len(after['headings']),
            'accepted_headings': sum(h['inheritance_allowed'] for h in after['headings']),
            'scanned_records': after['scanned_records'], 'structure_truncated': after['truncated'],
            'chunk_count': len(chunks), 'chunk_scan_truncated': truncated,
            'exact_chunk_offsets_valid': True, 'physical_page_ids_unchanged': True,
            'original_pages_and_tables_unchanged': True, 'adjacent_chunk_order_valid': True,
            'table_annotation_updates': annotation_updates,
            'chunk_payload_unchanged': chunks == old_chunks if old_chunks is not None else None,
            'selected_page_audit': audit})
        if not args.annotations_only or annotation_updates:
            changes.append((json.dumps(metadata, ensure_ascii=False), row['id'], row['metadata']))
    if args.apply:
        with connection:
            for metadata, source_id, old_metadata in changes:
                cursor = connection.execute('UPDATE sources SET metadata=? WHERE id=? AND metadata=?', (metadata, source_id, old_metadata))
                if cursor.rowcount != 1:
                    raise RuntimeError('Source metadata changed concurrently; entire transaction rolled back.')
            for row in rows:
                now = connection.execute('SELECT pages,tables,sha256,original_path FROM sources WHERE id=?', (row['id'],)).fetchone()
                assert all(now[key] == row[key] for key in ('pages', 'tables', 'sha256', 'original_path'))
    report = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'applied': args.apply,
              'schema_version': ds.VERSION, 'runtime': str(args.runtime.resolve()), 'project_id': args.project_id,
              'source_count': len(records), 'total_physical_pages': sum(r['physical_pages'] for r in records),
              'modified_source_count': len(changes),
              'new_ocr_calls': 0, 'new_model_calls': 0, 'new_embeddings': 0,
              'modified_fields_if_applied': (['sources.metadata.table_semantics.footnotes', 'sources.metadata.table_semantics.units'] if args.annotations_only
                                            else ['sources.metadata.document_structure', 'sources.metadata.document_profiles']),
              'index_rebuild_required': bool(changes),
              'source_revision_refresh_required': bool(changes),
              'embedding_inputs_unchanged': args.annotations_only and all(r['chunk_payload_unchanged'] for r in records), 'notes': [
                  'Metadata changes require RetrievalService.prepare to refresh source generation and index payloads; identical embedding inputs may reuse stored vectors.',
                  'Chunk IDs retain p{physical_page}:{start}-{end}; corrected boundaries can create different spans and IDs.',
                  'Page text, original document identity, tables and printed page labels remain unchanged.',
                  'B10 p213 contains a Section 6.3.2.1 cross-reference; its actual heading is on p214.'],
              'records': records}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(args.report), 'applied': args.apply, 'sources': len(records), 'pages': report['total_physical_pages']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
