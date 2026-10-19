"""Project large document catalogs, without projecting away source evidence.

The initial catalog is navigation metadata, not an answer context. Complete
sources remain in the controller. Search/read_image exposes the exact located
page's warnings and genuine SQLite columns before a subsequent SQL action.
Native database catalogs and small document catalogs retain their old shape.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import OrderedDict

from .schema_catalog import build_catalog

VERSION = 'located-document-catalog-v1'
INITIAL_DOCUMENT_CHARACTERS = 2400
DOCUMENT_FORMATS = {'pdf', 'png', 'jpg', 'jpeg', 'docx', 'txt', 'md'}
_PAGE = re.compile(r'^第\s*(\d+)\s*页[：:]\s*(.*)$', re.S)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _notes(source):
    # The engine's existing notes function also supplies draft/publication risks.
    from .document_engine import _notes as source_notes
    return source_notes(source)


def _warning_summary(notes):
    groups, globals_ = OrderedDict(), []
    for note in notes:
        match = _PAGE.match(note)
        if match is None:
            globals_.append(note)
            continue
        page, body = int(match[1]), match[2]
        # Only count-bearing diagnostic prefixes are normalized. Never alter
        # numbers in source text, formulas, table values or page-level warnings.
        template = re.sub(r'^\d+\s*(处文字|个单元格)', r'{数量}\1', body)
        group = groups.setdefault(template, {'count': 0, 'pages': set()})
        group['count'] += 1
        group['pages'].add(page)
    categories = []
    for template, group in list(groups.items())[:6]:
        categories.append({'message_template': template[:145],
                           'message_truncated': len(template) > 145,
                           'warning_count': group['count'], 'page_count': len(group['pages']),
                           'representative_pages': sorted(group['pages'])[:3]})
    return {'total_warnings': len(notes), 'categories': categories,
            'other_category_count': max(0, len(groups) - len(categories)),
            'global_notes': [note[:180] for note in globals_[:5]],
            'global_notes_omitted': max(0, len(globals_) - 5),
            'global_notes_shortened': any(len(note) > 180 for note in globals_[:5]),
            'publication_caution': any(any(term in note for term in ('草案', '征求意见', '初步统计')) for note in notes),
            'meaning': '仅目录风险摘要；代表页不是全部风险页。读取相关页后提供该页原始警告，不能据摘要判断其他页可靠。'}


def project_catalog(source, entry):
    """Return a bounded navigation projection only for oversized documents."""
    if source.get('format') not in DOCUMENT_FORMATS or len(_json(entry)) <= INITIAL_DOCUMENT_CHARACTERS:
        return entry
    notes = entry.get('warnings') or []
    result = {key: copy.deepcopy(entry[key]) for key in
              ('id', 'name', 'format', 'sha256', 'parser_version', 'page_count', 'page_unit') if key in entry}
    result['warnings_summary'] = _warning_summary(notes)
    result['catalog_projection'] = {
        'version': VERSION, 'scope': 'initial_navigation_only',
        'table_count': len(source.get('tables') or []),
        'original_catalog_characters': len(_json(entry)),
        'original_catalog_sha256': hashlib.sha256(_json(entry).encode()).hexdigest(),
        'full_source_preserved': True, 'answer_evidence_omitted': False,
        'access': '完整原文、表结构与警告仍在所选资料内；先用 search 定位页，随后自动展开命中页的真实 SQL Schema 与原始警告。read_image 可核对原页。',
        'table_schema_deferred': bool(source.get('tables'))}
    # Metadata can contain arbitrarily long prose. Shorten only explicitly
    # marked examples, never citations/evidence or underlying source records.
    while len(_json(result)) > INITIAL_DOCUMENT_CHARACTERS:
        summary = result['warnings_summary']
        if summary['global_notes']:
            summary['global_notes'].pop()
            summary['global_notes_omitted'] += 1
        elif summary['categories']:
            summary['categories'].pop()
            summary['other_category_count'] += 1
        else:
            break  # Exact source identity is never truncated to satisfy a cap.
    return result


def expose_page_catalog(entry, source, page, cancelled=lambda: False):
    """Expose a genuinely located page; never infer its tables from names."""
    if not entry.get('catalog_projection') or type(page) is not int:
        return
    located = entry.setdefault('located_pages', [])
    if any(item.get('page') == page for item in located):
        return
    metadata = source.get('metadata') or {}
    names = set()
    for key in ('table_regions', 'table_semantics', 'table_layouts'):
        for table in metadata.get(key) or []:
            if not isinstance(table, dict):
                continue
            position = table.get('source_position') or {}
            actual_page = table.get('page', position.get('page'))
            name = table.get('table_name') or table.get('name')
            if actual_page == page and isinstance(name, str):
                names.add(name)
    imported = [table for table in source.get('tables') or [] if table.get('name') in names]
    page_record = {'page': page,
                   'warnings': [note for note in _notes(source)
                                if (match := _PAGE.match(note)) and int(match[1]) == page],
                   'table_names': [], 'table_location_basis': 'stored_page_metadata',
                   'scope': 'located_page_only_not_full_document'}
    if imported and source.get('data_path'):
        # Restrict discovery before building: an unrelated table earlier in the
        # document must not exhaust the existing schema-catalog budget.
        local_source = {**source, 'tables': imported}
        catalog = build_catalog(local_source, table_names=[], cancelled=cancelled)
        known = {table['name'] for table in entry.get('tables', [])}
        for table in catalog.get('tables', []):
            page_record['table_names'].append(table['name'])
            if table['name'] not in known:
                entry.setdefault('tables', []).append({key: copy.deepcopy(value) for key, value in table.items()
                                                      if key != 'table_context'})
                known.add(table['name'])
        # The exact selected table semantic context is already in text evidence;
        # duplicating it here would waste the evidence budget again.
        for relationship in catalog.get('relationships', []):
            if relationship not in entry.setdefault('relationships', []):
                entry['relationships'].append(copy.deepcopy(relationship))
        page_record['catalog_truncated'] = bool(catalog.get('truncated'))
        page_record['catalog_warnings'] = catalog.get('warnings', [])
    located.append(page_record)
