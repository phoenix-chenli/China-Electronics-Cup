"""Bounded table relationships with source anchors; inferred roles stay explicit.

SQL values are never filled from merged coverage or rewritten from these hints.
Rows/columns are one-based source coordinates, not necessarily SQL row numbers.
"""
from __future__ import annotations

import re

VERSION = 'table-semantics-v1'
MAX_TABLES = 64
MAX_CELLS = 2048
MAX_TEXT = 24000
MAX_CONTEXT = 6
MAX_HEADER_ROWS = 8
UNIT = re.compile(r'(?:单位|Unit[s]?)\s*[:：]\s*([^\n;；]{1,48})', re.I)
INLINE_UNIT = re.compile(r'[（(]\s*(亿元|万元|元|人民币元|美元|万元人民币|%|％|百分比|percent|percentage|万人|人|吨|千克|公斤|kg|t|km|公里|平方米|万元/人|元/吨|天|小时|年|月)\s*[）)]', re.I)
CAPTION = re.compile(r'^\s*(?:表\s*[一二三四五六七八九十\d]+|Table\s*\d+)\s*[.．:：、\-]?', re.I)
FOOTNOTE = re.compile(r'^\s*(?:注(?:释)?|备注|说明|资料来源|数据来源|Note[s]?|Source)\s*[:：.．\d]', re.I)


def _referenced_numeric_notes(context, columns):
    """Keep numbered notes only when a header actually carries that marker.

    Numbered prose below a table is not automatically a footnote. The link is
    still a reviewable structural candidate, never a semantic certification.
    """
    markers = {}
    for column in columns:
        for header in column['header_path']:
            text = header['text'].strip()
            match = re.search(r'(?<=[A-Za-z.)）\u3400-\u9fff])\s*(\d{1,3})$', text)
            if match:
                markers.setdefault(match[1], set()).add(column['column'])
    notes = []
    for item in context:
        if item['side'] != 'after':
            continue
        match = re.match(r'^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3}))\s+\S', item['text'])
        marker = next((part for part in match.groups() if part), None) if match else None
        if marker in markers:
            notes.append({**item, 'marker': marker, 'referenced_columns': sorted(markers[marker]),
                          'role_basis': 'header_marker_and_adjacent_note_candidate',
                          'requires_review': True})
    return notes


def _text(value):
    return '' if value is None else str(value)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) or bool(
        re.fullmatch(r'[-+−]?\d[\d,.，]*(?:%|％)?', _text(value).strip()))


def _anchor(cell, position):
    out = {k: v for k, v in position.items() if k in ('page', 'sheet', 'page_unit', 'body_index')}
    out.update({k: cell[k] for k in ('row', 'col', 'start', 'end', 'bbox') if k in cell})
    return out


def build_table_semantics(region, *, format, source_position=None, before=(), after=(), explicit_header_rows=()):
    """Return one source-local semantic record. Role detection is never certainty."""
    position = dict(source_position or {})
    if region.get('page') is not None:
        position.setdefault('page', region['page'])
    if region.get('bbox') is not None:
        position.setdefault('bbox', region['bbox'])
    reasons = list(region.get('review_reasons', []))
    cells, characters, truncated = [], 0, bool(region.get('truncated'))
    for original in region.get('cells', []):
        cell = {k: original[k] for k in ('row', 'col', 'text', 'rowspan', 'colspan', 'covered_by', 'bbox', 'start', 'end', 'source_text') if k in original}
        size = len(_text(cell.get('text'))) + len(_text(cell.get('source_text')))
        if len(cells) >= MAX_CELLS or characters + size > MAX_TEXT:
            truncated = True
            break
        # Coverage points to its real anchor and never becomes an independent value.
        if cell.get('covered_by'):
            if cell.get('text') is not None:
                cell['source_text'] = cell['text']
            if _text(cell.get('source_text')).strip():
                reasons.append('合并覆盖位置含文字，保留原文但不作为独立数值')
            cell['text'] = None
        cell['cell_id'] = f"R{cell['row']}C{cell['col']}"
        cells.append(cell)
        characters += size
    by_position = {(c['row'], c['col']): c for c in cells}
    width = region.get('column_count', 0)
    count = region.get('row_count', 0)
    explicit = sorted(set(r for r in explicit_header_rows if isinstance(r, int) and 1 <= r <= count))
    explicit_available = explicit and len(explicit) <= MAX_HEADER_ROWS and all(any(c['row'] == r for c in cells) for r in explicit)
    headers, basis = (explicit, 'explicit') if explicit_available else ([], 'unknown')
    if explicit and not explicit_available:
        reasons.append('显式表头超出语义窗口，未补猜完整表头')
    top_rows = sorted({c['row'] for c in cells})[:MAX_HEADER_ROWS]
    if not headers:
        # Only a contiguous leading text block followed by a numeric data row is
        # a header candidate. All-text business tables stay unknown.
        candidate = []
        for r in top_rows:
            row = [c for c in cells if c['row'] == r and not c.get('covered_by') and _text(c.get('text')).strip()]
            if not row:
                break
            if any(_number(c.get('text')) for c in row):
                if candidate:
                    year_values = any(re.fullmatch(r'(?:19|20)\d{2}', _text(c.get('text')).strip()) for c in row)
                    headers, basis = candidate, 'unknown' if year_values else 'inferred'
                    if year_values:
                        reasons.append('候选表头后出现纯数字年份，无法确认其为表头还是业务数据')
                break
            if any(CAPTION.match(_text(c.get('text'))) or UNIT.match(_text(c.get('text'))) or FOOTNOTE.match(_text(c.get('text'))) for c in row):
                # Whole-table metadata rows are not column labels.
                if len(row) == 1:
                    continue
            candidate.append(r)
    if basis != 'explicit':
        reasons.append('表头角色为结构候选，需对照原表确认' if headers else '未能可靠区分表头和业务数据行')
    columns = []
    for col in range(1, width + 1):
        path, seen = [], set()
        for row in headers:
            cell = by_position.get((row, col))
            if not cell:
                continue
            if cell.get('covered_by'):
                cell = by_position.get(tuple(cell['covered_by']))
            if not cell or not _text(cell.get('text')).strip():
                continue
            key = (cell['row'], cell['col'])
            if key in seen:
                continue
            seen.add(key)
            path.append({'text': _text(cell['text']), 'row': cell['row'], 'col': cell['col'], 'evidence': _anchor(cell, position)})
        columns.append({'column': col, 'header_path': path, 'label': ' / '.join(p['text'] for p in path), 'role_basis': basis})
    context, context_characters = [], 0
    for side, entries in (('before', before), ('after', after)):
        for item in list(entries)[:MAX_CONTEXT]:
            text = _text(item.get('text'))
            if context_characters + len(text) > 4000:
                truncated = True
                continue
            if text:
                context.append({**item, 'side': side})
                context_characters += len(text)
    # In-grid captions/notes keep their own source row, not a fictitious paragraph.
    for cell in cells:
        if cell.get('covered_by'):
            continue
        text = _text(cell.get('text'))
        if CAPTION.match(text) or FOOTNOTE.match(text) or UNIT.match(text):
            if context_characters + len(text) > 4000 or len(context) >= 2 * MAX_CONTEXT + MAX_HEADER_ROWS:
                truncated = True
                continue
            context.append({'text': text, 'evidence': _anchor(cell, position), 'side': 'inside'})
            context_characters += len(text)
    captions = [c for c in context if c['side'] != 'after' and CAPTION.match(c['text'])]
    notes = [c for c in context if FOOTNOTE.match(c['text'])]
    notes.extend(_referenced_numeric_notes(context, columns))
    units = []
    for item in context:
        match = UNIT.match(item['text'].strip())
        if match:
            units.append({'text': item['text'], 'value': match[1],
                          'scope': 'ambiguous' if item['side'] == 'after' else 'table_candidate', 'evidence': item.get('evidence', {})})
    for column in columns:
        for header in column['header_path']:
            for match in INLINE_UNIT.finditer(header['text']):
                units.append({'text': header['text'], 'value': match[1], 'scope': 'column', 'column': column['column'], 'evidence': header['evidence']})
    if len({u['value'] for u in units if u['scope'] == 'table_candidate'}) > 1:
        reasons.append('同表上下文存在多个单位声明，未选择统一单位')
        for unit in units:
            if unit['scope'] == 'table_candidate':
                unit['scope'] = 'ambiguous'
    row_headers = []
    serial_column = bool(columns and any(re.fullmatch(r'\s*(?:序号|编号|No\.?|ID|序)\s*', p['text'], re.I) for p in columns[0]['header_path']))
    for c in cells:
        if c['col'] == 1 and c['row'] not in headers and not c.get('covered_by') and _text(c.get('text')).strip() and (serial_column or not _number(c['text'])):
            row_headers.append({'row': c['row'], 'column': c['col'], 'text': _text(c['text']), 'cell_id': c['cell_id'],
                                'rowspan': c.get('rowspan', 1), 'colspan': c.get('colspan', 1),
                                'role_basis': 'explicit_key' if serial_column else 'inferred',
                                'role': 'row_key' if serial_column else 'row_label_candidate', 'evidence': _anchor(c, position)})
    if truncated:
        reasons.append('语义元数据达到预算，仅保留标明范围内的原始单元格与上下文')
    return {'schema_version': VERSION, 'table_name': region['table_name'], 'format': format,
            'page': position.get('page'), 'sheet': position.get('sheet'), 'source_position': position,
            'row_count': count, 'column_count': width, 'header_rows': headers, 'header_basis': basis,
            'columns': columns, 'cells': cells, 'row_headers': row_headers,
            'caption': captions[-1] if captions else None, 'footnotes': notes, 'units': units,
            'context': context, 'requires_review': bool(reasons or region.get('requires_review')),
            'review_reasons': list(dict.fromkeys(reasons)), 'truncated': truncated,
            'limits': {'max_cells': MAX_CELLS, 'max_cell_text_characters': MAX_TEXT,
                       'max_context_characters': 4000, 'max_header_rows': MAX_HEADER_ROWS},
            'interpretation_note': '表头、行头和单位作用域仅是注明依据的结构解释；合并覆盖空值不是独立数值。'}


def page_context(region, siblings, page):
    """Nearby same-page blocks, without borrowing text through another table."""
    box = region.get('bbox')
    if not box or page.get('quality', {}).get('pdf_page_rotation'):
        return [], []  # Rotated visual axes do not safely imply reading order.
    before, after = [], []
    for block in page.get('blocks', []):
        b = block.get('bbox')
        if not b or not block.get('text'):
            continue
        if b[2] <= box[0] or b[0] >= box[2]:
            continue
        if any(other is not region and other.get('bbox') and b[0] < other['bbox'][2] and b[2] > other['bbox'][0]
               and b[1] < other['bbox'][3] and b[3] > other['bbox'][1] for other in siblings):
            continue
        # A neighboring table between this block and the target cuts association.
        side = 'before' if b[3] <= box[1] and box[1] - b[3] <= .12 else 'after' if b[1] >= box[3] and b[1] - box[3] <= .10 else None
        if not side:
            continue
        if any(other is not region and other.get('bbox') and other['bbox'][0] < box[2] and other['bbox'][2] > box[0]
               and (b[3] <= other['bbox'][1] < box[1] if side == 'before' else box[3] < other['bbox'][3] <= b[1]) for other in siblings):
            continue
        record = {'text': block['text'], 'evidence': {'page': region['page'], 'bbox': b,
                  **{k: block[k] for k in ('start', 'end') if k in block}}}
        (before if side == 'before' else after).append(record)
    before.sort(key=lambda x: x['evidence']['bbox'][1])
    after.sort(key=lambda x: x['evidence']['bbox'][1])
    return before[-MAX_CONTEXT:], after[:MAX_CONTEXT]
