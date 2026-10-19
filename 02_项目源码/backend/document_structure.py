"""Traceable document hierarchy and separate complexity/quality diagnostics.

All offsets refer to the existing parsed page text. A TOC line, marginal header
or inferred title is annotated, never deleted or corrected in the source text.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from itertools import islice
from xml.etree import ElementTree as ET

VERSION = 'zhiheng-document-3'
MAX_HEADINGS = 2000
MAX_RECORDS = 10000
# PDF pages contain individual lines/cells, unlike DOCX paragraphs. The old
# shared 10k prefix stopped halfway through ordinary manuals, then inherited
# its last heading across all unscanned pages.
MAX_STRUCTURE_RECORDS = 100000
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_TOC = re.compile(r'^(?P<title>\S.{0,180}?)(?:\.{2,}|…{2,}|·{2,}|\t+|\s{3,})\s*(?P<label>\d{1,5}|[ivxlcdmIVXLCDM]{1,12})\s*$')
_NUMBERED = re.compile(r'^(?:(第[一二三四五六七八九十百零〇\d]+[章节篇部])\s*|([一二三四五六七八九十百]+)[、．.]\s*|([（(][一二三四五六七八九十\d]+[）)])\s*|(\d+(?:\.\d+){0,6})[.、．\s]\s*)(\S.{0,100})$')
_SECTION = re.compile(r'^([A-Z](?:\.\d+){1,6}|\d+(?:\.\d+){0,6})(?:[.、．]\s*|\s+)([^\d\s].{1,150})$', re.S)
_BARE_NUMBER = re.compile(r'^(?:[A-Z](?:\.\d+)+|\d+(?:\.\d+)*)(?:\.)?$')
_SCOPE = re.compile(r'^(?:chapter|appendix|volume|module|part)\s+([A-Z]|\d+|[IVXLCDM]+)(?:\b|[.:])', re.I)
_CONTENTS = re.compile(r'^(?:目\s*录|contents|table\s+of\s+contents|list\s+of\s+(?:figures|tables|examples|checklists))(?:\s*\(?continued\)?)?$', re.I)


def docx_styles(raw):
    if not raw or len(raw) > 2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        return {}
    root = ET.fromstring(raw)
    result = {}
    for node in root.findall(f'{{{W}}}style')[:2048]:
        identity = node.get(f'{{{W}}}styleId')
        if not identity:
            continue
        def val(path):
            e = node.find(path)
            return e.get(f'{{{W}}}val') if e is not None else None
        result[identity] = {'name': val(f'{{{W}}}name') or identity,
                            'based_on': val(f'{{{W}}}basedOn'),
                            'outline_level': val(f'{{{W}}}pPr/{{{W}}}outlineLvl')}
    return result


def paragraph_properties(node, styles=None):
    styles = styles or {}
    def val(path):
        e = node.find(path)
        return e.get(f'{{{W}}}val') if e is not None else None
    style_id = val(f'{{{W}}}pPr/{{{W}}}pStyle') or ''
    level = val(f'{{{W}}}pPr/{{{W}}}outlineLvl')
    names, visited, inherited = [style_id], set(), style_id
    while inherited and inherited in styles and inherited not in visited and len(visited) < 32:
        visited.add(inherited)
        record = styles[inherited]
        names.append(record['name'])
        if level is None:
            level = record.get('outline_level')
        inherited = record.get('based_on')
    title_style = next((re.fullmatch(r'(?:heading|标题)\s*([1-9])', n.strip(), re.I) for n in names if re.fullmatch(r'(?:heading|标题)\s*([1-9])', n.strip(), re.I)), None)
    heading_level = (int(level) + 1 if re.fullmatch('[0-8]', level) else None) if level is not None else int(title_style.group(1)) if title_style else None
    instruction = ' '.join(t.text or '' for t in node.iter(f'{{{W}}}instrText'))
    toc = any(re.fullmatch(r'(?:toc|目录)\s*[1-9]?', n.strip(), re.I) for n in names) or bool(re.search(r'\bTOC\b', instruction))
    sizes = [e.get(f'{{{W}}}val') for e in node.findall(f'.//{{{W}}}rPr/{{{W}}}sz')]
    runs = [e for e in node.findall(f'.//{{{W}}}r') if e.find(f'{{{W}}}t') is not None]
    bold = bool(runs) and all(e.find(f'{{{W}}}rPr/{{{W}}}b') is not None and e.find(f'{{{W}}}rPr/{{{W}}}b').get(f'{{{W}}}val', 'true') not in ('0', 'false', 'off') for e in runs)
    return {'style_id': style_id, 'style_names': list(dict.fromkeys(names))[:32], 'heading_level': None if toc else heading_level,
            'is_toc': toc, 'outline_level_explicit': level is not None,
            'font_size': max((int(s) / 2 for s in sizes if s and s.isdigit()), default=None), 'bold': bold,
            'bookmarks': [e.get(f'{{{W}}}name') for e in node.iter(f'{{{W}}}bookmarkStart') if e.get(f'{{{W}}}name')][:32]}


def _control_properties(node, path):
    properties = node.find(f'{{{W}}}sdtPr')
    values = {}
    if properties is not None:
        for name in ('tag', 'alias', 'id'):
            child = properties.find(f'{{{W}}}{name}')
            if child is not None:
                value = child.get(f'{{{W}}}val', '')
                if len(value) > 4096:
                    raise ValueError('Word 内容控件属性超过4096字符，无法完整保留，请简化后重试')
                values[name] = value
    return {**values, 'control_path': list(path), 'declaration_only': True,
            'semantic_verified': False}


def docx_body_blocks(body, *, with_controls=False):
    """Include real block content controls (common for TOCs), never deleted runs."""
    def descend(node, top_index, path, toc=False, depth=0, controls=()):
        if depth > 32:
            raise ValueError('Word 内容控件嵌套超过32层，无法完整读取结构；请简化文档后重试')
        if node.tag in (f'{{{W}}}p', f'{{{W}}}tbl'):
            value = (top_index, node, path, toc)
            yield (*value, list(controls)) if with_controls else value
        elif node.tag in (f'{{{W}}}sdt', f'{{{W}}}sdtContent', f'{{{W}}}customXml', f'{{{W}}}ins'):
            if node.tag == f'{{{W}}}sdt':
                controls = (*controls, _control_properties(node, path))
            galleries = [e.get(f'{{{W}}}val', '').casefold() for e in node.findall(f'{{{W}}}sdtPr//{{{W}}}docPartGallery')]
            toc = toc or any(g in ('table of contents', '目录') for g in galleries)
            for i, child in enumerate(node):
                yield from descend(child, top_index, path + [i], toc, depth + 1, controls)
    for index, child in enumerate(body):
        yield from descend(child, index, [index])


def docx_control_annotations(node, text, position, containers=()):
    """Declared business tags beside exact content spans, never extra facts.

    Container tags cover the current paragraph/table span. Inline tags receive
    exact offsets only for paragraphs: table-cell serialization is different,
    so table inline tags retain their XML location and explicitly lack a span.
    """
    member = position.get('archive_member', 'word/document.xml')
    base = {**position, 'archive_member': member}
    result = [{**c, 'scope': 'block_container', 'source_position': dict(base),
               'text': text, 'span_verified': True} for c in containers]
    raw = ''.join(t.text or '' for t in node.iter(f'{{{W}}}t'))
    left = len(raw) - len(raw.lstrip())
    paragraph = node.tag == f'{{{W}}}p'
    offset = 0
    def walk(current, path, depth=0):
        nonlocal offset
        if depth > 64:
            raise ValueError('Word 行内内容控件嵌套过深，无法完整保留标签')
        start = offset
        if current.tag == f'{{{W}}}t':
            offset += len(current.text or '')
        for i, child in enumerate(current):
            walk(child, path + [i], depth + 1)
        if current.tag == f'{{{W}}}sdt':
            entry = {**_control_properties(current, path), 'scope': 'inline', 'span_verified': False,
                     'source_position': {'archive_member': member, 'block_path': path},
                     'text': raw[start:offset]}
            a, b = max(0, start - left), min(len(raw.strip()), offset - left)
            if paragraph and 0 <= a <= b <= len(text) and text[a:b] == raw[max(start, left):min(offset, left + len(raw.strip()))]:
                entry.update(text=text[a:b], span_verified=True,
                             source_position={**base, 'start': position['start'] + a, 'end': position['start'] + b})
            result.append(entry)
    walk(node, position['block_path'])
    return result


def docx_marginal_parts(archive, body):
    """Keep header/footer parts and explicit section references without paging."""
    import posixpath
    relationship_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    relationships = {}
    try:
        raw = archive.read('word/_rels/document.xml.rels')
    except KeyError:
        raw = b''
    if raw:
        if len(raw) > 2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('Word 关系文件超出处理范围')
        for relation in ET.fromstring(raw):
            if relation.get('TargetMode') == 'External':
                continue
            target = relation.get('Target', '')
            member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('word', target))
            if member.startswith('word/') and member.endswith('.xml'):
                relationships[relation.get('Id')] = member
    references = defaultdict(list)
    missing = []
    for number, section in enumerate(body.iter(f'{{{W}}}sectPr'), 1):
        for kind in ('header', 'footer'):
            for reference in section.findall(f'{{{W}}}{kind}Reference'):
                rid = reference.get(f'{{{relationship_ns}}}id')
                item = {'section_index': number, 'kind': kind, 'variant': reference.get(f'{{{W}}}type', 'default'), 'relationship_id': rid}
                member = relationships.get(rid)
                if member:
                    references[member].append(item)
                else:
                    missing.append(item)
    members = set(references) | {n for n in archive.namelist() if re.fullmatch(r'word/(?:header|footer)[^/]*\.xml', n)}
    if len(members) > 256:
        raise ValueError('Word 页眉页脚部件超过256份，请拆分后导入')
    parts, total = [], 0
    for member in sorted(members):
        try:
            raw = archive.read(member)
        except KeyError:
            missing.extend(references[member])
            continue
        total += len(raw)
        if len(raw) > 2_000_000 or total > 8_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('Word 页眉页脚结构超出处理范围，请拆分后导入')
        root = ET.fromstring(raw)
        if root.tag not in (f'{{{W}}}hdr', f'{{{W}}}ftr'):
            missing.extend(references[member])
            continue
        blocks, chunks, offset = [], [], 0
        for index, node, path, toc, controls in docx_body_blocks(root, with_controls=True):
            # These offsets refer only to this ZIP part, never a printed page.
            text = ''.join(t.text or '' for t in node.iter(f'{{{W}}}t')).strip()
            instructions = [t.text or '' for t in node.iter(f'{{{W}}}instrText')]
            if text or instructions:
                position = {'archive_member': member, 'start': offset, 'end': offset + len(text), 'page_unit': 'archive_part', 'block_path': path}
                blocks.append({'text': text, 'start': offset, 'end': offset + len(text), 'block_path': path,
                               'controls': docx_control_annotations(node, text, position, controls), 'field_instructions': instructions,
                               'source_position': position})
                chunks.append(text)
                offset += len(text) + 1
        parts.append({'archive_member': member, 'kind': 'header' if root.tag.endswith('}hdr') else 'footer',
                      'text': '\n'.join(chunks), 'blocks': blocks, 'section_references': references[member],
                      'page_unit': 'archive_part', 'semantic_verified': False})
    return {'parts': parts, 'unresolved_references': missing,
            'note': '保留原页眉页脚与显式节引用；未排版，不推算物理页码或继承范围，字段保留已存文字而不重新计算。'}


def _number_level(text):
    if _BARE_NUMBER.fullmatch(text.strip()):
        return None  # A number without a title is not a section declaration.
    section = _SECTION.match(text.strip())
    if section:
        return len(section[1].split('.'))
    match = _NUMBERED.match(text.strip())
    if not match:
        return None
    if match[1] or match[2]:
        return 1
    if match[3]:
        return 2
    return min(9, (match[4] or '').count('.') + 1)


def _number_path(text):
    section = _SECTION.match(text.strip())
    if section:
        return tuple(section[1].split('.'))
    scope = _SCOPE.match(text.strip())
    return (scope[1].upper(),) if scope else ()


def _joined_heading_records(records, pages):
    """Join a number and title only when the original PDF puts them on one line.

    The anchor is still one exact substring of the original page, including
    its newline. No inferred text is inserted into the source or citation.
    """
    used = set()
    for index, row in enumerate(records):
        if index in used:
            continue
        if index + 1 < len(records) and _BARE_NUMBER.fullmatch(row['text'].strip()):
            other = records[index + 1]
            a, b = row.get('bbox'), other.get('bbox')
            if (row['page'] == other['page'] and a and b and row.get('bold') and other.get('bold')
                    and abs(a[1] - b[1]) < .004 and a[2] <= b[0] + .004
                    and abs((row.get('font_size') or 0) - (other.get('font_size') or 0)) < .5
                    and not _BARE_NUMBER.fullmatch(other['text'].strip())
                    and 2 < len(other['text']) < 150):
                used.add(index + 1)
                yield {**row, 'end': other['end'], 'bbox': [a[0], min(a[1], b[1]), b[2], max(a[3], b[3])],
                       'text': pages[row['page']]['text'][row['start']:other['end']]}
                continue
        yield row


def _compact(text):
    return re.sub(r'\s+', '', text).casefold()


def _bookmark_key(text):
    # PDF bookmarks often add a punctuation separator after "Appendix A"
    # which is printed on its own line. The anchored source span stays exact.
    return re.sub(r'^(appendix[a-z]|chapter\d+)[.:]', r'\1', _compact(text))


def _position(record, page_unit):
    return {k: record[k] for k in ('page', 'start', 'end', 'bbox', 'body_index', 'block_path') if k in record} | {'page_unit': page_unit}


def _subtract(start, end, exclusions):
    spans, cursor = [], start
    for left, right in sorted(exclusions):
        left, right = max(left, start), min(right, end)
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            spans.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end:
        spans.append((cursor, end))
    return spans


def build(result, *, format):
    metadata = result.get('metadata', {})
    pages = result.get('pages', [])
    page_unit = metadata.get('page_unit', 'physical_page')
    by_page = {p['number']: p for p in pages}
    paragraphs = metadata.get('paragraphs', [])
    if paragraphs:
        iterator = ({**p['source_position'], **p} for p in paragraphs)
    else:
        iterator = ({**b, 'page': p['number']} for p in pages for b in p.get('blocks', []))
    records = list(islice(iterator, MAX_STRUCTURE_RECORDS + 1))
    records_truncated = len(records) > MAX_STRUCTURE_RECORDS or bool(metadata.get('paragraphs_truncated'))
    records = records[:MAX_STRUCTURE_RECORDS]
    valid = []
    for record in records:
        current = by_page.get(record.get('page'))
        a, b = record.get('start'), record.get('end')
        if current and type(a) is int and type(b) is int and 0 <= a < b <= len(current['text']) and current['text'][a:b] == record.get('text'):
            valid.append(record)
    valid.sort(key=lambda r: (r['page'], r['start'], r['end']))
    rows_by_page = defaultdict(list)
    for r in valid:
        rows_by_page[r['page']].append(r)
    fonts = [r.get('font_size') for r in valid if isinstance(r.get('font_size'), (int, float)) and len(r['text']) > 25]
    typical = statistics.median(fonts) if fonts else None
    excluded, toc, headings, issues = [], [], [], []
    # Repeated marginal strings only; no global deletion of repeated business data.
    marginal = defaultdict(set)
    toc_pages = {r['page'] for r in valid if _CONTENTS.fullmatch(r['text'].strip())}
    leader_counts = defaultdict(int)
    for r in valid:
        if _TOC.match(r['text'].strip()) and re.search(r'\.{2,}|…{2,}|·{2,}', r['text']):
            leader_counts[r['page']] += 1
    toc_candidate_pages = {p for p, count in leader_counts.items() if count >= 2}
    for r in valid:
        box = r.get('bbox')
        if box and (box[3] <= .10 or box[1] >= .90) and len(r['text']) <= 120:
            # Smaller running headers often last for one chapter, not half of
            # the entire book. Keep the larger real title with the same text.
            if typical is None or not r.get('font_size') or r['font_size'] <= typical * .95:
                marginal[(_compact(r['text']), 'header' if box[3] <= .10 else 'footer')].add(r['page'])
    for r in valid:
        text = r['text'].strip()
        box = r.get('bbox')
        role = None
        if box:
            location = 'header' if box[3] <= .10 else 'footer' if box[1] >= .90 else None
            repeats = marginal[(_compact(text), location)] if location else set()
            nearby = sum(abs(page - r['page']) <= 4 for page in repeats)
            small = typical is None or not r.get('font_size') or r['font_size'] <= typical * .95
            if location and small and (nearby >= 2 or len(repeats) >= max(2, int(len(pages) * .5))):
                role = 'repeated_' + location
            if box[1] >= .88 and by_page[r['page']].get('printed_page_label') == text:
                role = 'declared_page_label'
        match = _TOC.match(text)
        if r.get('is_toc') or match and r['page'] in toc_pages | toc_candidate_pages:
            confirmed = bool(r.get('is_toc') or r['page'] in toc_pages)
            if confirmed:
                role = 'table_of_contents'
            toc.append({'title': match['title'].strip() if match else text,
                        'printed_page_label': match['label'] if match else None,
                        'source_position': _position(r, page_unit), 'target_physical_page': None,
                        'classification': 'declared_or_titled_toc' if confirmed else 'candidate_requires_review',
                        'mapping_status': 'unresolved', 'source': 'docx_toc_style' if r.get('is_toc') else 'leader_pattern'})
        # A titled list page is front matter, including the detached section
        # numbers and captions that its dotted lines are split into.
        if r['page'] in toc_pages and (format == 'pdf' or leader_counts[r['page']]):
            role = role or 'table_of_contents'
        if role:
            excluded.append({'page': r['page'], 'start': r['start'], 'end': r['end'], 'role': role})
    excluded_set = {(e['page'], e['start'], e['end']) for e in excluded}
    claimed = set()
    heading_cutoff = None
    def add(title, level, position, source, status='declared', **extra):
        nonlocal heading_cutoff
        key = (position.get('page'), position.get('start'), position.get('end'))
        if key in claimed or key in excluded_set:
            return
        if len(headings) >= MAX_HEADINGS:
            if heading_cutoff is None or (position['page'], position['start']) < (heading_cutoff['page'], heading_cutoff['start']):
                heading_cutoff = position
            return
        if any(h['source_position']['page'] == position['page'] and h['source_position']['start'] < position['end']
               and position['start'] < h['source_position']['end'] for h in headings):
            return
        claimed.add(key)
        headings.append({'id': '', 'title': title, 'level': level, 'parent_id': None, 'source': source,
                         'source_position': position, 'structure_status': status,
                         'inheritance_allowed': status != 'candidate_requires_review',
                         'confidence': 1.0 if status == 'declared' else .5,
                         'number_path': list(_number_path(title)), **extra})
    for r in valid:
        if r.get('heading_level'):
            add(r['text'], r['heading_level'], _position(r, page_unit), 'docx_heading', style_id=r.get('style_id'))
    # A bookmark is only an anchor after an exact title match on its actual page.
    for entry in metadata.get('outline', [])[:MAX_HEADINGS]:
        if entry.get('source') != 'pdf_bookmark' or _CONTENTS.fullmatch(entry.get('title', '').strip()):
            continue
        rows = rows_by_page.get(entry.get('page'), [])
        found = None
        for index, row in enumerate(rows):
            for size in (1, 2, 3, 4):
                group = rows[index:index + size]
                if len(group) != size:
                    continue
                if (not any((r['page'], r['start'], r['end']) in excluded_set for r in group)
                        and _bookmark_key(''.join(r['text'] for r in group)) == _bookmark_key(entry['title'])):
                    found = {**_position(row, page_unit), 'end': group[-1]['end']}
                    break
            if found:
                break
        if found:
            original = by_page[found['page']]['text'][found['start']:found['end']]
            source = 'pdf_bookmark_exact_anchor' if _compact(original) == _compact(entry['title']) else 'pdf_bookmark_separator_normalized_anchor'
            add(entry['title'], min(9, max(1, int(entry['level']))), found, source)
        else:
            issues.append({'kind': 'bookmark_not_anchored', 'title': entry['title'], 'page': entry.get('page')})
    table_boxes = defaultdict(list)
    for region in metadata.get('table_regions', []):
        if region.get('bbox'):
            table_boxes[region.get('page')].append(region['bbox'])
    for r in _joined_heading_records(valid, by_page):
        if ((r['page'], r['start'], r['end']) in excluded_set or len(r['text']) > 160
                or r['page'] in toc_pages | toc_candidate_pages or r.get('is_toc')):
            continue
        box = r.get('bbox')
        if box and any(t[0] <= (box[0] + box[2]) / 2 <= t[2] and t[1] <= (box[1] + box[3]) / 2 <= t[3]
                       for t in table_boxes.get(r['page'], [])):
            continue
        level = _number_level(r['text'])
        font = r.get('font_size')
        large = bool(typical and isinstance(font, (int, float)) and font >= typical * 1.2)
        scope = _SCOPE.match(r['text'].strip())
        if scope and (large or r.get('bold')):
            level = 1
        anchored = bool(box and r.get('bold') and typical and isinstance(font, (int, float))
                        and font >= typical * .95 and not re.search(r'[。;；]$', r['text'].strip()))
        # Text numbering alone may be a list item. Require actual style/font facts.
        if level and (large or r.get('bold') is True):
            path = _number_path(r['text'])
            root_typography = bool(typical and isinstance(font, (int, float)) and font >= typical * 1.1)
            explicit_scope = bool(re.fullmatch(r'(?:chapter|appendix|volume|module|part)\s+(?:[A-Z]|\d+(?:\.0)?|[IVXLCDM]+)[.:]?', r['text'].strip(), re.I))
            # A bold list item "5. Coefficient" and prose "Chapter 6 covers:"
            # must not replace an actual chapter and inherit across the book.
            strong = anchored and (len(path) > 1 or bool(path) and (root_typography or explicit_scope))
            add(r['text'], level, _position(r, page_unit), 'numbering_and_typography',
                'rule_anchored' if strong else 'candidate_requires_review',
                confidence=.9 if strong else .55, scope_reset=bool(scope))
        elif large and len(r['text']) < 80 and r.get('bold') is True:
            title_like = bool(re.match(r'[A-Z\u3400-\u9fff]', r['text'].strip())) and len(r['text'].strip()) >= 5
            strong = anchored and title_like
            add(r['text'], 1, _position(r, page_unit), 'typography',
                'rule_anchored' if strong else 'candidate_requires_review', confidence=.85 if strong else .5,
                scope_reset=True)
    headings.sort(key=lambda h: (h['source_position']['page'], h['source_position']['start']))
    exclusions_by_page = defaultdict(list)
    for e in excluded:
        exclusions_by_page[e['page']].append((e['start'], e['end']))
    stack = []
    for index, heading in enumerate(headings):
        heading['id'] = f'H{index + 1}'
        if not heading['inheritance_allowed']:
            heading.update(level_gap=False, body_spans=[])
            continue
        path = heading['number_path']
        if heading.get('scope_reset'):
            stack.clear()
        while stack and stack[-1]['level'] >= heading['level']:
            stack.pop()
        # A missing root title must not make 6.3 a child of 5.10. Numeric
        # ancestors must be prefixes, not merely shallower visual headings.
        while stack and path and stack[-1]['number_path'] and path[:len(stack[-1]['number_path'])] != stack[-1]['number_path']:
            stack.pop()
        if stack:
            heading['parent_id'] = stack[-1]['id']
        heading['level_gap'] = heading['level'] > ((stack[-1]['level'] if stack else 0) + 1)
        stack.append(heading)
        stop = next((h['source_position'] for h in headings[index + 1:] if h['inheritance_allowed'] and
                     (h['level'] <= heading['level'] or h.get('scope_reset') or
                      path and h['number_path'] and h['number_path'][:len(path)] != path)), None)
        if records_truncated and valid:
            cutoff = {'page': valid[-1]['page'], 'start': valid[-1]['end']}
            if stop is None or (cutoff['page'], cutoff['start']) < (stop['page'], stop['start']):
                stop = cutoff
        if heading_cutoff and (stop is None or (heading_cutoff['page'], heading_cutoff['start']) < (stop['page'], stop['start'])):
            stop = heading_cutoff
        spans = []
        start = heading['source_position']
        for p in pages:
            if p['number'] < start['page'] or stop and p['number'] > stop['page']:
                continue
            a = start['start'] if p['number'] == start['page'] else 0
            b = stop['start'] if stop and p['number'] == stop['page'] else len(p['text'])
            for left, right in _subtract(a, b, exclusions_by_page[p['number']]):
                spans.append({'page': p['number'], 'start': left, 'end': right, 'page_unit': page_unit})
        heading['body_spans'] = spans
    labels = [{'physical_page': p['number'], 'printed_page_label': p.get('printed_page_label'),
               'basis': p.get('printed_page_label_basis', 'unknown')} for p in pages] if page_unit == 'physical_page' else []
    for entry in toc:
        matched = [p for p in labels if p['printed_page_label'] == entry['printed_page_label'] and p['basis'] == 'pdf_page_label']
        if entry['printed_page_label'] and len(matched) == 1:
            entry.update(target_physical_page=matched[0]['physical_page'], mapping_status='declared_pdf_page_label')
    if records_truncated:
        issues.append({'kind': 'structure_scan_truncated', 'scanned_records': len(records),
                       'inheritance_stopped_at': _position(valid[-1], page_unit) if valid else None})
    if heading_cutoff:
        issues.append({'kind': 'heading_limit_reached', 'inheritance_stopped_at': heading_cutoff})
    return {'schema_version': VERSION, 'page_unit': page_unit, 'headings': headings, 'toc_entries': toc,
            'excluded_spans': excluded, 'page_labels': labels, 'issues': issues,
            'scanned_records': len(records),
            'truncated': records_truncated or heading_cutoff is not None or bool(metadata.get('quality_summary', {}).get('processing_truncated')),
            'structure_status': 'available' if headings else 'unknown',
            'note': '目录与页眉页脚仅旁挂标注，原文字未删除；仅声明标题与具备正文位置和字形依据的标题用于继承，待核候选不进入章节路径。'}


def profiles(result):
    pages, metadata = result.get('pages', []), result.get('metadata', {})
    formulas = metadata.get('formula_structures', [])
    merged = sum(bool(t.get('merged_cells')) for t in metadata.get('table_layouts', [])) + sum(bool(t.get('has_merged_cells')) for t in metadata.get('table_regions', []))
    features = {'pages': len(pages), 'formula_count': len(formulas), 'table_count': len(result.get('tables', [])),
                'merged_table_count': merged, 'multi_column_pages': list(metadata.get('complexity', {}).get('multi_column_pages', [])),
                'heading_count': len(metadata.get('document_structure', {}).get('headings', []))}
    score = min(100, features['table_count'] * 3 + merged * 8 + len(formulas) * 4 + len(features['multi_column_pages']) * 8)
    quality_pages, routes = [], []
    for p in pages:
        q = p.get('quality', {})
        observed = {k: q[k] for k in ('contrast', 'sharpness', 'grayscale_range', 'blank', 'rotation_correction', 'pdf_page_rotation', 'skew_degrees', 'ocr_mean_confidence', 'low_confidence_blocks') if k in q}
        flags = []
        if q.get('blank'):
            flags.append('blank_or_near_uniform')
        if q.get('contrast') is not None and q['contrast'] < 15 and not q.get('blank'):
            flags.append('low_contrast')
        if q.get('sharpness') is not None and q['sharpness'] < 45 and not q.get('blank'):
            flags.append('possible_blur')
        if q.get('rotation_correction'):
            flags.append('orientation_corrected')
        if isinstance(q.get('skew_degrees'), (int, float)) and abs(q['skew_degrees']) > 3:
            flags.append('skew_detected')
        if q.get('ocr_mean_confidence') is not None and q['ocr_mean_confidence'] < .75:
            flags.append('ocr_uncertain')
        if q.get('visual_review_required'):
            flags.append('preprocessing_disagreement')
        if p.get('extraction') == 'unreadable':
            flags.append('unreadable')
        quality_pages.append({'page': p['number'], 'status': 'observed' if observed else 'not_assessed', 'observations': observed,
                              'flags': flags, 'warnings': list(q.get('warnings', [])),
                              'not_assessed': [k for k in ('contrast', 'sharpness', 'skew_degrees', 'ocr_mean_confidence') if q.get(k) is None]})
        action = 'manual_source_check' if 'unreadable' in flags else 'targeted_high_resolution_review' if flags else 'native_extraction' if p.get('extraction') == 'native' else 'retain_ocr_and_verify_selected_evidence'
        if p.get('adaptive_processing'):
            actual = p['adaptive_processing']
            routes.append({'page': p['number'], 'requested_action': actual['requested_route'],
                           'basis': actual['reasons'], 'executed': actual['executed'],
                           'status': actual['status'], 'selected_extraction': p.get('extraction'),
                           'elapsed_ms': actual.get('elapsed_ms'),
                           'verification_status': 'not_verified',
                           'suggested_followup': action, 'followup_executed': False})
        else:
            routes.append({'page': p['number'], 'suggested_action': action, 'basis': flags or [p.get('extraction', 'unknown')], 'executed': False})
    return {'schema_version': VERSION, 'complexity': {'kind': 'rule_estimate', 'score': score, 'scale': [0, 100], 'features': features,
            'note': '结构处理负担估计，不是文档质量、答案正确率或OCR准确率。'},
            'quality': {'kind': 'observed_diagnostics', 'pages': quality_pages, 'accuracy_estimate': None,
                        'note': '仅报告实际测量与识别不确定性；无图像测量的DOCX不推断清晰度。'}, 'processing_plan': routes}


def table_continuations(tables):
    """Link only adjacent physical pages with matching printed title and headers.

    This is a source relationship, never a merged SQL table or inherited unit.
    """
    links, pending = [], []
    ordered = sorted((t for t in tables if type(t.get('page')) is int), key=lambda t: t['page'])
    def title(t):
        value = (t.get('caption') or {}).get('text', '')
        return re.sub(r'\s*[（(]续[）)]\s*$|\s*[（(]continued[）)]\s*$', '', value, flags=re.I).strip()
    def header(t):
        return tuple(tuple(p.get('text', '') for p in c.get('header_path', [])) for c in t.get('columns', []))
    for right in ordered:
        possible = [left for left in ordered if left['page'] + 1 == right['page'] and
                    title(left) and title(left) == title(right)]
        good = [left for left in possible if left.get('column_count') == right.get('column_count') and
                header(left) == header(right) and header(right) and all(header(right)) and
                not any(t.get('truncated') or t.get('structure_unknown') or t.get('header_basis') == 'unknown' for t in (left,right))]
        if len(good) == 1:
            left = good[0]
            links.append({'from_table':left['table_name'],'to_table':right['table_name'],
                'from_page':left['page'],'to_page':right['page'],
                'basis':'exact_printed_title_and_column_paths_adjacent_pages',
                'title_anchors':[left.get('caption'),right.get('caption')],
                'status':'structural_continuation_candidate','requires_semantic_review':True,
                'merged':False,'units_inherited':False})
        elif possible:
            pending.append({'table_name':right['table_name'],'page':right['page'],
                            'status':'unknown','reason':'ambiguous_or_incomplete_title_header_match'})
    return {'links':links[:64],'pending':pending[:64],'truncated':len(links)>64 or len(pending)>64,
            'note':'每页原表、原行和单位独立保留；关联不授权跨表求和或自动继承表头。'}


def enrich(result, *, format):
    """Idempotent metadata enrichment; never mutates source pages or table values."""
    from . import formula_structure
    metadata = result.setdefault('metadata', {})
    if format in ('pdf', 'png', 'jpg', 'jpeg'):
        formulas, seen = [], 0
        for p in result.get('pages', []):
            for b in p.get('blocks', []):
                if b.get('kind') != 'formula_candidate':
                    continue
                seen += 1
                if len(formulas) >= formula_structure.MAX_FORMULAS:
                    continue
                formulas.append(formula_structure.from_text(b['text'], identifier=f'F{len(formulas) + 1}',
                    location={'page': p['number'], 'page_unit': 'physical_page', 'start': b['start'], 'end': b['end'], 'bbox': b.get('bbox')},
                    origin='pdf_native_text' if p.get('extraction') == 'native' else 'ocr_text'))
        metadata['formula_structures'] = formulas
        metadata['formula_structures_truncated'] = seen > len(formulas)
    for formula in metadata.get('formula_structures', []):
        formula['parameter_candidates'] = formula_structure.parameter_candidates(formula, result.get('pages', []))
    metadata['document_structure'] = build(result, format=format)
    metadata['document_profiles'] = profiles(result)
    metadata['table_continuations'] = table_continuations(metadata.get('table_semantics', []))
    from .text_normalization import candidates
    vocabulary = [b.get('text','') for p in result.get('pages', []) for b in p.get('blocks', [])
                  if p.get('extraction') == 'native' or isinstance(b.get('confidence'), (int,float)) and b['confidence'] >= .95]
    metadata['normalization_candidates'] = candidates(result.get('pages', []), vocabulary)
    return result
