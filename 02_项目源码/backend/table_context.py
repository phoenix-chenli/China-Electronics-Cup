"""Small, positional table projections for text citations and real SQL rows.

Never search values to guess a source cell. Covered cells keep their anchor and
never become selected values. A budget loss remains an explicit unusable boundary.
"""
from __future__ import annotations

import copy
import json
import re


DEFAULT_MAX_CHARS = 6000


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def _overlap(left, right):
    return (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))
            and len(left) == len(right) == 4
            and left[0] < right[2] and right[0] < left[2]
            and left[1] < right[3] and right[1] < left[3])


def _span(position, start, end):
    return (isinstance(position.get('start'), int) and isinstance(position.get('end'), int)
            and position['start'] < end and start < position['end'])


def _cell_box_match(cell_box, block_box):
    # Font bounds can spill a fraction across a grid line. The real block's
    # centre must lie inside the source cell, in addition to actual overlap;
    # a tiny edge intersection cannot attach a neighbouring row's value.
    if not _overlap(cell_box, block_box):
        return False
    area = (block_box[2] - block_box[0]) * (block_box[3] - block_box[1])
    overlap = ((min(cell_box[2], block_box[2]) - max(cell_box[0], block_box[0]))
               * (min(cell_box[3], block_box[3]) - max(cell_box[1], block_box[1])))
    return (area > 0 and overlap / area >= .7
            and cell_box[0] <= (block_box[0] + block_box[2]) / 2 < cell_box[2]
            and cell_box[1] <= (block_box[1] + block_box[3]) / 2 < cell_box[3])


def _ocr_offsets(cells, page_text):
    """Validate producer-supplied offsets, never discover a cell by its value."""
    valid, intervals, invalid = {}, [], set()
    for cell in cells:
        if cell.get('offset_basis') != 'ocr_cell_block' or cell.get('covered_by'):
            continue
        start, end, value = cell.get('start'), cell.get('end'), cell.get('text')
        identifier = cell.get('cell_id')
        if (not identifier or type(start) is not int or type(end) is not int
                or not isinstance(value, str) or not value.strip()
                or not 0 <= start < end <= len(page_text) or page_text[start:end] != value):
            continue
        if identifier in valid:
            invalid.add(identifier)
        valid[identifier] = (start, end)
        intervals.append((start, end, identifier))
    furthest, owner = -1, None
    for start, end, identifier in sorted(intervals):
        if start < furthest:
            invalid.update((owner, identifier))
        if end > furthest:
            furthest, owner = end, identifier
    return {identifier: interval for identifier, interval in valid.items() if identifier not in invalid}


def _ocr_span_covered(offsets, page_text, start, end):
    """A broad legacy box is harmless only inside exact, original cell text."""
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(page_text):
        return False
    intervals = sorted((max(start, left), min(end, right)) for left, right in offsets.values()
                       if left < end and start < right)
    cursor = start
    for left, right in intervals:
        if page_text[cursor:left].strip():
            return False
        cursor = max(cursor, right)
    return bool(intervals) and not page_text[cursor:end].strip()


def _budget(tables, max_chars, *, has_table=None):
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 128:
        raise ValueError('表格证据预算至少为128字符')
    result = {'tables': [], 'has_table': bool(tables) if has_table is None else has_table,
              'truncated': False, 'requires_review': False}
    if any(table.get('structure_unknown') for table in tables):
        result['unresolved_structure'] = True
    for table in tables:
        proposed = {**result, 'tables': result['tables'] + [table]}
        if _size(proposed) <= max_chars:
            result['tables'].append(table)
            result['truncated'] |= bool(table.get('truncated'))
            result['requires_review'] |= bool(table.get('requires_review'))
            continue
        result['truncated'] = result['requires_review'] = True
        boundary = {'table_name': table.get('table_name'), 'selected_cell_ids': [],
                    'truncated': True, 'requires_review': True, 'selection_state': 'budget_exceeded'}
        if _size({**result, 'tables': result['tables'] + [boundary]}) <= max_chars:
            result['tables'].append(boundary)
    return result


def _project(table, selected_ids, selected_rows, *, selection_state, extra_reasons=(), verified_ocr_offsets=None, page_text=None):
    cells = table.get('cells') or []
    by_id = {c['cell_id']: c for c in cells if isinstance(c, dict) and c.get('cell_id')}
    header_rows = set(table.get('header_rows') or [])
    selected_ids = [c['cell_id'] for c in cells if c.get('cell_id') in selected_ids
                    and c.get('row') not in header_rows and not c.get('covered_by')]
    needed = {c['cell_id'] for c in cells if c.get('row') in selected_rows or c.get('row') in header_rows}
    # A selected row may inherit a real merged row label from an earlier row.
    selected_row_headers = [r for r in table.get('row_headers', [])
                   if any(r.get('row', 0) <= row < r.get('row', 0) + max(1, r.get('rowspan', 1))
                          for row in selected_rows)]
    needed.update(r['cell_id'] for r in selected_row_headers if r.get('cell_id'))
    for column in table.get('columns', []):
        needed.update(f"R{p['row']}C{p['col']}" for p in column.get('header_path', [])
                      if 'row' in p and 'col' in p)
    # Follow only existing anchor coordinates, never fill their values into the
    # covered position. Closure is bounded by the parser's finite cell registry.
    for _ in range(len(cells) + 1):
        added = {f'R{anchor[0]}C{anchor[1]}' for identifier in needed if identifier in by_id
                 for anchor in [by_id[identifier].get('covered_by')]
                 if isinstance(anchor, (list, tuple)) and len(anchor) == 2}
        if added <= needed:
            break
        needed |= added
    reasons = list(dict.fromkeys([*table.get('review_reasons', []), *extra_reasons]))
    keys = ('schema_version', 'table_name', 'format', 'page', 'sheet', 'source_position',
            'row_count', 'column_count', 'header_rows', 'header_basis', 'columns',
            'units', 'caption', 'footnotes', 'context', 'interpretation_note',
            'extraction', 'structure_basis', 'sql_eligible', 'candidate_audit_truncated')
    result = {key: copy.deepcopy(table[key]) for key in keys if key in table}
    result.update(cells=[copy.deepcopy(c) for c in cells if c.get('cell_id') in needed],
                  row_headers=copy.deepcopy(selected_row_headers), selected_cell_ids=selected_ids,
                  selection_state=selection_state, requires_review=bool(table.get('requires_review') or reasons),
                  review_reasons=reasons, truncated=bool(table.get('truncated')))
    if 'units' in result:
        result['units'] = [unit for unit in result['units']
                           if unit.get('scope') != 'row' or unit.get('row') in selected_rows]
    if table.get('structure_unknown'):
        result['structure_unknown'] = result['requires_review'] = True
    # These records retain their complete content and source position in their
    # dedicated fields. Repeating the same item in generic surrounding context
    # spends the evidence budget without adding another constraint.
    def position_key(record):
        return (record.get('text'), json.dumps(record.get('evidence'), ensure_ascii=False, sort_keys=True))
    specialized = [*(result.get('units') or []), *(result.get('footnotes') or [])]
    if isinstance(result.get('caption'), dict):
        specialized.append(result['caption'])
    seen = {position_key(record) for record in specialized}
    context = []
    for record in result.get('context') or []:
        key = position_key(record)
        if key not in seen:
            context.append(record)
            seen.add(key)
    if 'context' in result:
        result['context'] = context
    if verified_ocr_offsets:
        # This is a prompt projection only. Source cells retain their full
        # geometry and OCR diagnostics. Exact source offsets, verified against
        # this page before projecting, already locate the other retained cells.
        verified = verified_ocr_offsets
        selected = set(selected_ids)
        for cell in result['cells']:
            anchor = cell.get('covered_by')
            covered = (isinstance(anchor, (list, tuple)) and len(anchor) == 2
                       and f'R{anchor[0]}C{anchor[1]}' in verified)
            if cell.get('cell_id') not in verified and not covered:
                continue
            if cell.get('cell_id') not in selected:
                cell.pop('bbox', None)
            for key in ('text_bbox', 'offset_basis', 'confidence', 'crop_ocr_confidence',
                        'verification', 'requires_review'):
                cell.pop(key, None)
            if cell.get('rowspan') == 1:
                cell.pop('rowspan')
            if cell.get('colspan') == 1:
                cell.pop('colspan')
            if cell.get('covered_by') is None:
                cell.pop('covered_by', None)
        def compact_anchor(record):
            anchor = record.get('evidence') or {}
            key = f"R{anchor.get('row')}C{anchor.get('col')}"
            exact = verified.get(key) == (anchor.get('start'), anchor.get('end'))
            if not exact and page_text is not None:
                start, end = anchor.get('start'), anchor.get('end')
                exact = (type(start) is int and type(end) is int and 0 <= start < end <= len(page_text)
                         and isinstance(record.get('text'), str) and page_text[start:end] == record['text'])
            if exact:
                anchor.pop('bbox', None)
                if anchor.get('page_unit') == (result.get('source_position') or {}).get('page_unit'):
                    anchor.pop('page_unit', None)
        for column in result.get('columns') or []:
            for header in column.get('header_path') or []:
                compact_anchor(header)
                anchor = header.get('evidence') or {}
                for key in ('row', 'col'):
                    if key in anchor and header.get(key) == anchor[key]:
                        header.pop(key, None)  # Exact coordinates remain in evidence.
            if column.get('role_basis') == result.get('header_basis'):
                column.pop('role_basis', None)
            path = column.get('header_path') or []
            if column.get('label') == ' / '.join(str(p.get('text', '')) for p in path):
                column.pop('label', None)  # The full ordered path is retained.
        for record in [*(result.get('row_headers') or []), *(result.get('units') or []),
                       *(result.get('context') or []), *(result.get('footnotes') or []),
                       *([result['caption']] if isinstance(result.get('caption'), dict) else [])]:
            compact_anchor(record)
        # The complete source table, rather than this one selected row, defines
        # the positional range. Replace its duplicate box only when every
        # observed main cell has a verified original interval.
        observed = [c for c in cells if not c.get('covered_by') and str(c.get('text') or '').strip()]
        if observed and all(c.get('cell_id') in verified for c in observed):
            position = result.get('source_position') or {}
            position.setdefault('start', min(s[0] for s in verified.values()))
            position.setdefault('end', max(s[1] for s in verified.values()))
            position.pop('bbox', None)
        for key in ('sheet', 'footnotes', 'row_headers'):
            if result.get(key) in (None, []):
                result.pop(key, None)
        if result.get('truncated') is False:
            result.pop('truncated')  # The enclosing context retains its flag.
        # Generic parser descriptors stay in full source metadata. The prompt
        # retains every cell coordinate and the actual review/role constraints.
        result.pop('interpretation_note', None)
        result.pop('schema_version', None)
    if table.get('recovery_origin') == 'on_demand_preserved_grid_no_ocr':
        # Late-table rows share the same immutable grid as their source. Keep
        # exact table/page/row/column identity; do not repeat cell boxes and
        # default merge flags in every prompt row. Non-default merges survive.
        # Header/row-label evidence keeps its own full source anchor.
        for cell in result['cells']:
            cell.pop('bbox', None)
            if cell.get('rowspan') == 1:
                cell.pop('rowspan')
            if cell.get('colspan') == 1:
                cell.pop('colspan')
            if cell.get('covered_by') is None:
                cell.pop('covered_by', None)
        for record in [*(result.get('context') or []), *(result.get('footnotes') or []),
                       *([result['caption']] if isinstance(result.get('caption'), dict) else [])]:
            anchor = record.get('evidence') or {}
            if isinstance(anchor.get('start'), int) and isinstance(anchor.get('end'), int):
                anchor.pop('bbox', None)  # Exact original offsets are retained.
        result['recovery_origin'] = table['recovery_origin']
        result['cell_position_basis'] = '原件同页同表的原始row/col；完整格bbox仍保留在本地原始grid，未修改格值或合并关系。'
    return result


def _unknown(name, position, reason):
    return {'table_name': name, 'source_position': copy.deepcopy(position), 'cells': [],
            'columns': [], 'row_headers': [], 'selected_cell_ids': [], 'header_basis': 'unknown',
            'structure_unknown': True, 'requires_review': True, 'truncated': False,
            'selection_state': 'unknown', 'review_reasons': [reason]}


def _page_semantics(source, page):
    """Recover only located legacy grids, without mutating the parsed source.

    The import-time table count cap is not an absence-of-structure claim. A
    preserved original grid can be interpreted when actually requested. Roles
    remain inferred and still require the existing visual/semantic review.
    """
    from .table_semantics import build_table_semantics, page_context
    metadata = source.get('metadata') or {}
    existing = [table for table in metadata.get('table_semantics') or []
                if table.get('page', (table.get('source_position') or {}).get('page')) == page]
    known = {table.get('table_name') for table in existing}
    regions = [region for region in metadata.get('table_regions') or []
               if isinstance(region, dict) and region.get('page') == page]
    current = next((item for item in source.get('pages') or [] if item.get('number') == page), {})
    for region in regions:
        if region.get('table_name') in known or not region.get('cells') or not region.get('table_name'):
            continue
        if any(not isinstance(cell, dict) or type(cell.get('row')) is not int or type(cell.get('col')) is not int
               or cell['row'] < 1 or cell['col'] < 1 for cell in region['cells']):
            continue  # An incomplete legacy grid remains explicitly unknown.
        before, after = page_context(region, regions, current)
        semantic = build_table_semantics(region, format=source.get('format'), before=before, after=after)
        semantic['recovery_origin'] = 'on_demand_preserved_grid_no_ocr'
        existing.append(semantic)
        known.add(region['table_name'])
    from .table_semantics import INLINE_UNIT
    engineering_unit = re.compile(r'[（(]\s*(mg/L|g/L|µg/L|μg/L|MGD|MPa|kPa|m³/s|m3/s|m³/d|m3/d)\s*[）)]')
    interpreted = []
    for table in existing:
        row_units = []
        for row in table.get('row_headers') or []:
            for pattern in (INLINE_UNIT, engineering_unit):
                for match in pattern.finditer(str(row.get('text', ''))):
                    row_units.append({'text': row['text'], 'value': match[1], 'scope': 'row',
                                      'row': row['row'], 'cell_id': row.get('cell_id'),
                                      'evidence': copy.deepcopy(row.get('evidence') or {})})
        if row_units:
            table = {**table, 'units': [*copy.deepcopy(table.get('units') or []), *row_units]}
        interpreted.append(table)
    return interpreted


def split_evidence_span(source, page, start, end, query=''):
    """Return exact original prose/row spans, with no new source text.

    Table headers are carried by each row's semantic context. Ambiguous spans
    remain explicitly table-shaped; they never become ordinary prose. Priority
    is only literal query/row-label overlap, not an answer or correctness score.
    The caller applies its existing cumulative evidence budget to these spans.
    """
    current = next((item for item in source.get('pages') or [] if item.get('number') == page), {})
    text = current.get('text') or ''
    if not (type(start) is int and type(end) is int and 0 <= start < end <= len(text)):
        return [{'start': start, 'end': end, 'kind': 'unresolved_table', 'priority': 0}]
    semantics = _page_semantics(source, page)
    local = {**source, 'metadata': {**(source.get('metadata') or {}), 'table_semantics': semantics}}
    positions = table_text_spans(local, page, current_page=current)
    regions = [(max(start, r['start']), min(end, r['end'])) for r in positions['regions']
               if r['start'] < end and start < r['end']]
    if not regions:
        unknown = context_for_span(source, page, start, end).get('has_table')
        return [{'start': start, 'end': end, 'kind': 'unresolved_table' if unknown else 'prose',
                 'priority': 0 if unknown else 5}]
    merged = []
    for left, right in sorted(regions):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    out, cursor = [], start
    for left, right in merged:
        if cursor < left:
            out.append({'start': cursor, 'end': left, 'kind': 'prose', 'priority': 5})
        cursor = right
    if cursor < end:
        out.append({'start': cursor, 'end': end, 'kind': 'prose', 'priority': 5})
    words = {w.casefold() for w in re.findall(r'[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}', query)}
    seen = set()
    for row in positions['rows']:
        left, right = max(start, row['start']), min(end, row['end'])
        if left >= right or (left, right) in seen:
            continue
        seen.add((left, right))
        table = next((t for t in semantics if t.get('table_name') == row.get('table_name')), {})
        label = ' '.join(str(r.get('text', '')) for r in table.get('row_headers') or [] if r.get('row') == row.get('row'))
        label_words = {w.casefold() for w in re.findall(r'[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}', label)}
        out.append({'start': left, 'end': right,
                    'kind': 'table_rows' if row.get('row') is not None else 'unresolved_table',
                    'priority': 10 * len(words & label_words), 'table_name': row.get('table_name')})
    if not seen:
        # Never label an unpositioned/ambiguous table region as prose.
        out.extend({'start': left, 'end': right, 'kind': 'unresolved_table', 'priority': 0}
                   for left, right in merged)
    return sorted(out, key=lambda item: (-item['priority'], item['start'], item['end']))


def table_text_spans(source, page, text_length=None, *, current_page=None):
    """Return existing contiguous row spans and confirmed table text regions.

    DOCX offsets come directly from original cells. Physical pages use actual
    text blocks intersecting original cell boxes. No cell values are searched,
    reordered, joined or copied into synthetic prose. Only confirmed table
    regions can replace generic full-table chunks; legacy grids remain subject
    to the existing unknown-structure gate.
    """
    current = current_page if current_page is not None else next((p for p in source.get('pages', []) if p.get('number') == page), {})
    page_text = current.get('text') or ''
    limit = len(page_text) if text_length is None else min(len(page_text), text_length)
    blocks = [b for b in current.get('blocks') or []
              if isinstance(b.get('start'), int) and isinstance(b.get('end'), int)
              and 0 <= b['start'] < b['end'] <= limit and b.get('bbox')]
    result = {'rows': [], 'regions': []}
    for table in (source.get('metadata') or {}).get('table_semantics') or []:
        position = table.get('source_position') or {}
        if table.get('page', position.get('page')) != page:
            continue
        cells = table.get('cells') or []
        ocr_offsets = _ocr_offsets(cells, page_text)
        hint_parts = [str((table.get('caption') or {}).get('text', ''))]
        hint_parts.extend(str(p.get('text', '')) for column in table.get('columns') or []
                          for p in column.get('header_path') or [])
        hint_parts.extend(str(u.get('text') or u.get('value') or '') for u in table.get('units') or [])
        hint_parts.extend(str(c.get('text', '')) for c in table.get('context') or [])
        hint = '\n'.join(dict.fromkeys(p for p in hint_parts if p))[:4000]
        identity = (source.get('id'), source.get('sha256'), source.get('parser_version'),
                    table.get('table_name'), page, table.get('sheet'))
        located = (isinstance(position.get('start'), int) and isinstance(position.get('end'), int)
                   and 0 <= position['start'] < position['end'])
        table_blocks = [b for b in blocks if _overlap(position.get('bbox'), b['bbox'])
                        or any(_overlap(c.get('bbox'), b['bbox']) for c in cells)] if not located else []
        row_spans = []
        for row in sorted({c.get('row') for c in cells if isinstance(c.get('row'), int)}):
            if row in (table.get('header_rows') or []):
                continue
            row_cells = [c for c in cells if c.get('row') == row]
            if located:
                spans = [(c['start'], c['end']) for c in row_cells
                         if isinstance(c.get('start'), int) and isinstance(c.get('end'), int)
                         and 0 <= c['start'] < c['end'] <= limit
                         and (c.get('offset_basis') != 'ocr_cell_block' or c.get('cell_id') in ocr_offsets)]
            else:
                spans = [ocr_offsets[c['cell_id']] for c in row_cells
                         if c.get('cell_id') in ocr_offsets and ocr_offsets[c['cell_id']][1] <= limit]
                spans.extend((b['start'], b['end']) for b in table_blocks
                         if any(c.get('offset_basis') != 'ocr_cell_block'
                                and _cell_box_match(c.get('bbox'), b['bbox']) for c in row_cells))
            if spans:
                row_spans.append({'start': min(s[0] for s in spans), 'end': max(s[1] for s in spans),
                                  'row': row, 'table_name': table.get('table_name'),
                                  'retrieval_hint': hint, 'table_identity': identity})
        if not row_spans:
            continue
        if not located:
            # A block covering several cells has no unambiguous cell offset.
            # Keep its original span so it remains visible as unresolved table
            # evidence; do not assign all its text to the centre cell.
            row_spans.extend({'start': b['start'], 'end': b['end'], 'row': None,
                              'table_name': table.get('table_name'), 'retrieval_hint': hint,
                              'table_identity': identity}
                             for b in table_blocks
                             if not _ocr_span_covered(ocr_offsets, page_text, b['start'], b['end'])
                             and not any(c.get('offset_basis') != 'ocr_cell_block'
                                         and _cell_box_match(c.get('bbox'), b['bbox']) for c in cells))
        if located:
            region = {'start': position['start'], 'end': min(position['end'], limit)}
        elif table_blocks:
            region = {'start': min(b['start'] for b in table_blocks), 'end': max(b['end'] for b in table_blocks)}
        elif ocr_offsets:
            region = {'start': min(s[0] for s in ocr_offsets.values()), 'end': min(limit, max(s[1] for s in ocr_offsets.values()))}
        else:
            continue
        if region['start'] < region['end']:
            result['regions'].append(region)
            result['rows'].extend(row_spans)
    return result


def context_for_span(source, page, start, end, max_chars=DEFAULT_MAX_CHARS):
    if (isinstance(page, bool) or not isinstance(page, int) or page < 1
            or isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, int) or not isinstance(end, int) or start < 0 or start >= end):
        raise ValueError('表格引用需要有效页码和精确文本范围')
    metadata = source.get('metadata') or {}
    current = next((p for p in source.get('pages', []) if p.get('number') == page), {})
    page_text = current.get('text') or ''
    cited_blocks = [b for b in current.get('blocks', []) if b.get('bbox') and _span(b, start, end)]
    boxes = [b['bbox'] for b in cited_blocks]
    semantics = _page_semantics(source, page)
    out, known = [], set()
    for table in semantics:
        position = table.get('source_position') or {}
        if table.get('page', position.get('page')) != page:
            continue
        cells = table.get('cells', [])
        ocr_offsets = _ocr_offsets(cells, page_text)
        matched = [c for c in cells if c.get('offset_basis') == 'ocr_cell_block'
                   and c.get('cell_id') in ocr_offsets and _span(c, start, end)]
        matched.extend(c for c in cells if c.get('offset_basis') != 'ocr_cell_block'
                       and (_span(c, start, end) or any(_cell_box_match(c.get('bbox'), box) for box in boxes)))
        intersects = bool(matched or _span(position, start, end)
                          or any(_overlap(position.get('bbox'), box) for box in boxes))
        if not intersects and not boxes and not isinstance(position.get('start'), int) and not any(
                isinstance(b.get('start'), int) and b.get('bbox') for b in current.get('blocks', [])):
            out.append(_unknown(table.get('table_name'), position,
                                '该页表格缺少可将当前引文定位到原格的文字坐标，不能使用裸表数字'))
            known.add(table.get('table_name'))
            continue
        if not intersects:
            continue
        known.add(table.get('table_name'))
        projection = _project(table, {c.get('cell_id') for c in matched}, {c['row'] for c in matched},
                              selection_state='source_span', extra_reasons=[] if matched else ['引用落在表区，但没有可定位的源单元格'],
                              verified_ocr_offsets=ocr_offsets, page_text=page_text)
        ambiguous_boxes = any(_overlap(position.get('bbox'), b['bbox'])
                              and not _ocr_span_covered(ocr_offsets, page_text,
                                                       max(start, b['start']), min(end, b['end']))
                              and not any(c.get('offset_basis') != 'ocr_cell_block'
                                          and _cell_box_match(c.get('bbox'), b['bbox']) for c in cells)
                              for b in cited_blocks)
        if not projection['selected_cell_ids'] or ambiguous_boxes:
            # A text span on a header/covered cell is not an aggregate SQL
            # scope and cannot turn into unstructured ordinary prose evidence.
            projection['structure_unknown'] = projection['requires_review'] = True
            projection['review_reasons'].append('当前表区引文没有可独立定位的源主格，或文字块实质横跨多个格')
        out.append(projection)
    for region in metadata.get('table_regions') or []:
        name = region.get('table_name')
        if name in known or any(s.get('table_name') == name for s in semantics):
            continue
        if region.get('page') == page and (_span(region, start, end) or any(_overlap(region.get('bbox'), box) for box in boxes)
                                          or not any(isinstance(b.get('start'), int) and b.get('bbox') for b in current.get('blocks', []))):
            out.append(_unknown(name, {k: region[k] for k in ('page', 'bbox', 'start', 'end') if k in region},
                                '该表只有旧网格，缺少表头、行头和单位关系；需重新解析'))
    for layout in metadata.get('table_layouts') or []:
        name = layout.get('table_name') or layout.get('name')
        if any(s.get('table_name') == name for s in semantics):
            continue
        position = layout.get('source_position') or layout
        located = isinstance(position.get('start'), int) and isinstance(position.get('end'), int)
        if not located or position.get('page') == page and _span(position, start, end):
            out.append(_unknown(name, {k: position[k] for k in ('page', 'start', 'end') if k in position},
                                '旧Word表格未保存可确认的文字范围和语义关系；需重新解析后使用表内事实'))
    return _budget(out, max_chars)


def context_for_table(source, table_name, row_numbers=None, max_chars=DEFAULT_MAX_CHARS):
    metadata = source.get('metadata') or {}
    table = next((t for t in metadata.get('table_semantics') or [] if t.get('table_name') == table_name), None)
    if table is None:
        legacy = any(r.get('table_name') == table_name for r in metadata.get('table_regions') or []) or any(
            (r.get('table_name') or r.get('name')) == table_name for r in metadata.get('table_layouts') or [])
        if legacy or source.get('format') == 'xlsx':
            return _budget([_unknown(table_name, {}, '当前表没有可核验的语义关系；需重新解析')], max_chars)
        return _budget([], max_chars)
    if row_numbers is not None and (not isinstance(row_numbers, list) or any(
            isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in row_numbers)):
        raise ValueError('SQL来源行号必须为正整数列表')
    requested = set(row_numbers or [])
    rows, reasons = set(), []
    mapping = table.get('sql_row_mapping')
    if requested and mapping:
        for row in requested:
            matches = [first_source + row - first_sql for first_sql, first_source, count in mapping.get('runs', [])
                       if first_sql <= row < first_sql + count]
            if len(matches) == 1:
                rows.add(matches[0])
            else:
                reasons.append('SQL行号无法唯一映射原表行；未猜测贡献行')
    elif requested and table.get('format') in {'pdf', 'image', 'docx'}:
        rows = requested
    elif requested:
        reasons.append('该格式缺少SQL行号到源行的映射；未猜测贡献行')
    available = {c.get('row') for c in table.get('cells', [])}
    if rows - available:
        reasons.append('所选源行超出已保存单元格窗口')
    selected = {c['cell_id'] for c in table.get('cells', []) if c.get('row') in rows}
    result = _project(table, selected, rows, selection_state='source_rows' if requested else 'table_only', extra_reasons=reasons)
    if reasons:
        result['truncated'] = True
    return _budget([result], max_chars)
