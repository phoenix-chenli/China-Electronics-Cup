"""Reviewable merged cells supported by physical rules, never inferred values.

The ordinary grid reader deliberately needs complete lines.  This bounded
fallback keeps partial header rules and real row-group cells, and rejects an
uncertain edge rather than inventing a missing border or filling a value.
"""
from __future__ import annotations

import math
import re
import statistics

MAX_AXES = 100
MAX_CELLS = 1200
_NUMBER = re.compile(r"[-+−]?\d[\d,.]*(?:%|％)?$")


def _runs(values, minimum, threshold=190):
    start, gap, result = None, 0, []
    for index, value in enumerate(values):
        if value < threshold:
            if start is None:
                start = index
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 2:
                if index-start-gap >= minimum:
                    result.append((start, index-gap))
                start = None
    if start is not None and len(values)-start-gap >= minimum:
        result.append((start, len(values)-gap-1))
    return result


def _segments(gray, vertical, minimum):
    width, height = gray.size
    pixels = gray.load()
    groups = []
    for position in range(width if vertical else height):
        values = ([pixels[position, i] for i in range(height)] if vertical else
                  [pixels[i, position] for i in range(width)])
        for start, end in _runs(values, minimum):
            group = next((g for g in reversed(groups)
                          if position <= g[-1][0]+2 and abs(g[-1][1]-start) <= 5
                          and abs(g[-1][2]-end) <= 5), None)
            if group is None:
                groups.append([(position, start, end)])
            else:
                group.append((position, start, end))
    return [(round(statistics.median(v[0] for v in g)),
             round(statistics.median(v[1] for v in g)),
             round(statistics.median(v[2] for v in g))) for g in groups
            if g[-1][0]-g[0][0] <= 7]


def _axis(values):
    groups = []
    for value in sorted(values):
        if groups and value-groups[-1][-1] <= 3:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [round(statistics.median(g)) for g in groups]


def _edge(gray, position, start, end, vertical):
    pixels = gray.load()
    length = end-start
    if length < 7:
        return None
    # Ignore intersections and inspect real ink in a narrow line corridor.
    observed = []
    limit = gray.width if vertical else gray.height
    for offset in range(max(0, position-2), min(limit, position+3)):
        observed.append(sum((pixels[offset, i] if vertical else pixels[i, offset]) < 190
                            for i in range(start+3, end-2))/(length-5))
    ratio = max(observed, default=0)
    if ratio >= .88:
        return True
    # Text may cross the place where an absent separator would have been.
    # It cannot create a nearly continuous rule; uncertain long fragments are
    # rejected, and the merge itself still needs a nonnumeric observed label.
    if ratio <= .55:
        return False
    return None


def _text(words):
    lines = []
    for word in sorted(words, key=lambda w: (w['bbox'][1], w['bbox'][0])):
        center = (word['bbox'][1]+word['bbox'][3])/2
        height = word['bbox'][3]-word['bbox'][1]
        group = next((g for g in lines if abs(g[0]-center) < max(g[1], height)*.55), None)
        if group is None:
            lines.append([center, height, [word]])
        else:
            group[2].append(word)
    value = '\n'.join(' '.join(w['text'] for w in sorted(g[2], key=lambda w: w['bbox'][0]))
                      for g in sorted(lines))
    return re.sub(r'(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])', '', value) or None


def _table(gray, words, frame, verticals, horizontals):
    width, height = gray.size
    left, top, right, bottom = frame
    xs = _axis([left, right]+[x for x, a, b in verticals
                             if left < x < right and a <= bottom-12 and b >= top+12])
    ys = _axis([top, bottom]+[y for y, a, b in horizontals
                             if top < y < bottom and a >= left-3 and b <= right+3])
    # A bold word can have a long horizontal stroke. Keep an axis only when it
    # physically closes at least one complete column interval.
    ys = [y for y in ys if any(_edge(gray, y, a, b, False) is True
                              for a, b in zip(xs, xs[1:]))]
    if not 3 <= len(xs) <= 40 or not 3 <= len(ys) <= MAX_AXES:
        return None
    rows, columns = len(ys)-1, len(xs)-1
    if rows*columns > MAX_CELLS or min(b-a for a, b in zip(xs, xs[1:])) < 9:
        return None
    h_edges = [[_edge(gray, y, xs[c], xs[c+1], False) for c in range(columns)] for y in ys]
    v_edges = [[_edge(gray, x, ys[r], ys[r+1], True) for x in xs] for r in range(rows)]
    if (not all(v is True for v in h_edges[0]+h_edges[-1])
            or not all(row[0] is True and row[-1] is True for row in v_edges)
            or any(v is None for row in h_edges+v_edges for v in row)):
        return None
    parents = list(range(rows*columns))

    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(a, b):
        parents[find(a)] = find(b)

    for r in range(rows):
        for c in range(columns):
            if c and not v_edges[r][c]:
                union(r*columns+c, r*columns+c-1)
            if r and not h_edges[r][c]:
                union(r*columns+c, (r-1)*columns+c)
    components = {}
    for r in range(rows):
        for c in range(columns):
            components.setdefault(find(r*columns+c), []).append((r, c))
    if all(len(items) == 1 for items in components.values()):
        return None  # Ordinary closed grids remain the existing reader's job.
    ownership, merged = {}, []
    for items in components.values():
        r1, c1 = min(r for r, _ in items), min(c for _, c in items)
        r2, c2 = max(r for r, _ in items), max(c for _, c in items)
        if len(items) != (r2-r1+1)*(c2-c1+1):
            return None
        # Arbitrary body holes are not a license to manufacture merged data.
        if len(items) > 1:
            header = r1 < min(3, max(1, rows//3)) and r2 < min(3, max(1, rows//3))
            left_group = c1 == c2 == 0 and r2-r1 >= 2
            if not (header or left_group):
                return None
            merged.append((r1, c1, r2, c2))
        for position in items:
            ownership[position] = (r1, c1, r2, c2)
    content, unassigned = {}, []
    for word in words:
        box = word.get('bbox', [])
        if len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
            return None
        cx, cy = (box[0]+box[2])*width/2, (box[1]+box[3])*height/2
        if not left < cx < right or not top < cy < bottom:
            continue
        r = next(i for i in range(rows) if ys[i] <= cy < ys[i+1])
        c = next(i for i in range(columns) if xs[i] <= cx < xs[i+1])
        anchor = ownership[r, c]
        r1, c1, r2, c2 = anchor
        if (box[0]*width < xs[c1]-3 or box[2]*width > xs[c2+1]+3
                or box[1]*height < ys[r1]-3 or box[3]*height > ys[r2+1]+3):
            # The rule geometry is independent of an OCR segmentation error.
            # Preserve that observation separately; never force it into a cell.
            if len(unassigned) < 128:
                unassigned.append({'text': word['text'], 'bbox': list(box),
                                   'reason': 'ocr_word_crosses_physical_cell_boundary'})
            continue
        content.setdefault(anchor, []).append(word)
    if len(content) < max(3, columns):
        return None
    for region in merged:
        value = _text(content.get(region, []))
        # Every merge needs an observed label. A missing numeric separator,
        # or an empty rectangle, remains an unresolved scan rather than a merge.
        if not value or any(_NUMBER.fullmatch(token) for token in value.split()):
            return None
    cells, values = [], [[None]*columns for _ in range(rows)]
    for r in range(rows):
        for c in range(columns):
            region = ownership[r, c]
            r1, c1, r2, c2 = region
            anchor = (r, c) == (r1, c1)
            own = content.get(region, []) if anchor else []
            value = _text(own)
            values[r][c] = value
            box = ([xs[c1], ys[r1], xs[c2+1], ys[r2+1]] if anchor else
                   [xs[c], ys[r], xs[c+1], ys[r+1]])
            cells.append({'row': r+1, 'col': c+1, 'text': value,
                          'bbox': [box[0]/width, box[1]/height, box[2]/width, box[3]/height],
                          'rowspan': r2-r1+1 if anchor else 1,
                          'colspan': c2-c1+1 if anchor else 1,
                          'covered_by': None if anchor else [r1+1, c1+1],
                          'confidence': min(w.get('confidence', 0) for w in own) if own else None,
                          'requires_review': True, 'verification': 'physical_partial_rules'})
    return {'bbox': [left/width, top/height, right/width, bottom/height],
            'extraction': 'ocr_grid', 'grid_detection': 'physical_merged_rules',
            'structure_basis': 'physical_segment_rules', 'sql_eligible': True,
            'physical_rule_support': 'closed_outer_frame_and_unambiguous_segment_edges',
            'rows': values, 'row_count': rows, 'column_count': columns, 'cells': cells,
            'header_names': [], 'has_merged_cells': True, 'data_row_offset': 0,
            'unassigned_words': unassigned,
            'requires_review': True, 'verification': 'physical_partial_rules',
            'review_reasons': ['真实格线支持的合并单元格候选；表头、组标签及数值仍须人工核对']
                             + (['部分 OCR 词框跨越真实格线，已单独保留且未强行填入任意单元格'] if unassigned else [])}


def detect_merged_tables(image, words):
    """Return bounded source-local candidates; malformed/broken grids stay prose."""
    if len(words) < 3:
        return []
    gray = image.convert('L')
    gray.thumbnail((1400, 1400))
    verticals = _segments(gray, True, max(35, gray.height*.03))
    groups = []
    for segment in verticals:
        x, top, bottom = segment
        group = next((g for g in groups if abs(g[0][1]-top) <= 4 and abs(g[0][2]-bottom) <= 4), None)
        if group is None:
            groups.append([segment])
        else:
            group.append(segment)
    frames = []
    for group in groups:
        xs = _axis(s[0] for s in group)
        top = round(statistics.median(s[1] for s in group))
        bottom = round(statistics.median(s[2] for s in group))
        if len(xs) >= 3 and xs[-1]-xs[0] >= gray.width*.2 and bottom-top >= 35:
            frames.append((xs[0], top, xs[-1], bottom))
    if not frames:
        return []
    horizontals = _segments(gray, False, max(30, gray.width*.065))
    result = []
    for frame in sorted(set(frames), key=lambda f: (f[2]-f[0])*(f[3]-f[1]), reverse=True)[:12]:
        if any(t['bbox'][0] <= frame[0]/gray.width and t['bbox'][1] <= frame[1]/gray.height
               and frame[2]/gray.width <= t['bbox'][2] and frame[3]/gray.height <= t['bbox'][3]
               for t in result):
            continue
        table = _table(gray, words, frame, verticals, horizontals)
        if table:
            result.append(table)
    return result[:8]
