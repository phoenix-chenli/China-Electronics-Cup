"""Literal TSV observations and bounded, review-only Windows table candidates."""
from __future__ import annotations

import csv
import io
import math
import re

from .ocr_native import detect_grid, text_blocks

REQUIRED = {'level', 'page_num', 'block_num', 'par_num', 'line_num',
            'left', 'top', 'width', 'height', 'conf', 'text'}
MAX_RECORDS = 150_000
MAX_CELL_CROPS = 80
_CJK_SPACE = re.compile(r'(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff，。、；：！？])|(?<=[，。、；：！？]) +(?=[\u3400-\u9fff])')


def join_words(words):
    # Only formatting between actual CJK observations changes. Never join or
    # repair numeric tokens, signs, decimals, percentages or Latin word gaps.
    value = ' '.join(words)
    return _CJK_SPACE.sub('', value)


def input_dpi(image):
    value = image.info.get('dpi')
    try:
        x, y = value
        if all(math.isfinite(float(n)) and 70 <= float(n) <= 2400 for n in (x, y)):
            return round(float(x)), round(float(y))
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def parse_tsv(raw, sizes):
    """TSV is not CSV quoting: an OCR quote must remain a literal character.

    Validate the whole response before exposing any observation. In particular,
    return-code zero plus missing TSV configuration is not an empty image.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 8_000_000:
        raise ValueError('empty_or_oversized_tsv')
    value = raw.decode('utf-8-sig')
    if '\x00' in value:
        raise ValueError('nul_in_tsv')
    reader = csv.DictReader(io.StringIO(value), delimiter='\t', quoting=csv.QUOTE_NONE, strict=True)
    fields = reader.fieldnames or []
    if not REQUIRED.issubset(fields) or len(fields) != len(set(fields)):
        raise ValueError('missing_tsv_header')
    pages = {number: [] for number in sizes}
    records = 0
    seen_pages = set()
    for row in reader:
        records += 1
        if records > MAX_RECORDS or None in row or any(v is None for v in row.values()):
            raise ValueError('malformed_tsv_record')
        level = int(row['level'])
        if level not in range(1, 6):
            raise ValueError('invalid_tsv_level')
        number = int(row['page_num'])
        if number not in sizes:
            raise ValueError('unexpected_tsv_page')
        seen_pages.add(number)
        if level != 5 or not row['text'].strip():
            continue
        key = tuple(int(row[k]) for k in ('page_num', 'block_num', 'par_num', 'line_num'))
        if any(n < 1 for n in key):
            raise ValueError('invalid_word_identity')
        x, y, w, h = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
        width, height = sizes[number]
        confidence = float(row['conf'])
        if (w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > width or y+h > height
                or not math.isfinite(confidence) or not -1 <= confidence <= 100):
            raise ValueError('invalid_word_geometry_or_confidence')
        text = row['text'].strip()
        if any(char in text for char in '\t\r\n'):
            raise ValueError('tsv_control_in_text')
        pages[number].append({'text': text, 'bbox': [x/width, y/height, (x+w)/width, (y+h)/height],
                              'confidence': max(0., confidence/100), 'line_key': key})
    if records != sum(bool(line.strip()) for line in value.splitlines())-1:
        raise ValueError('tsv_record_count_mismatch')
    if seen_pages != set(sizes):
        raise ValueError('incomplete_tsv_pages')
    return pages, records


def line_blocks(words):
    groups = {}
    for word in words:
        groups.setdefault(word['line_key'], []).append(word)
    blocks = []
    for group in groups.values():
        raw = ' '.join(word['text'] for word in group)
        text = join_words(word['text'] for word in group)
        block = {'text': text, 'bbox': [min(w['bbox'][0] for w in group), min(w['bbox'][1] for w in group),
                 max(w['bbox'][2] for w in group), max(w['bbox'][3] for w in group)],
                 'confidence': sum(w['confidence'] for w in group)/len(group),
                 'kind': 'formula_candidate' if any(c in text for c in '=∑∫√≤≥') else 'text'}
        if text != raw:
            block.update(engine_text=raw, text_normalization='cjk_interword_spaces')
        blocks.append(block)
    return text_blocks(blocks)


def table_candidates(image, words):
    if len(words) < 3:
        return []
    tables = detect_grid(image, words, allow_open_outer=True)
    for table in tables:
        ambiguous = table.pop('_requires_cell_ocr', False)
        table.update(verification='page_ocr_geometry', requires_review=True,
                     review_reasons=['机器单元格候选；表头、行名、数字、符号和单位须对照原件确认'])
        if ambiguous:
            table['review_reasons'].append('部分整页文字框跨格；未将越界文字分配给任意单元格')
        for cell in table['cells']:
            left, top, right, bottom = cell['bbox']
            own = [w for w in words if left <= w['bbox'][0] and top <= w['bbox'][1]
                   and w['bbox'][2] <= right and w['bbox'][3] <= bottom]
            value, _ = line_blocks(own)
            cell.update(text=value or None, verification='page_ocr_geometry', requires_review=True)
            table['rows'][cell['row']-1][cell['col']-1] = value or None
    return tables


def cell_crops(image, tables):
    """Bound one batched reread; a new number is never a formatting repair."""
    from PIL import Image, ImageOps
    jobs, pixels = [], 0
    for table in tables:
        for cell in table['cells']:
            # Empty observations remain unknown, not zero or proof of blank.
            if not cell.get('text'):
                continue
            if len(jobs) >= MAX_CELL_CROPS:
                return jobs, True
            box = cell['bbox']
            coords = [round(box[0]*image.width)+2, round(box[1]*image.height)+2,
                      round(box[2]*image.width)-2, round(box[3]*image.height)-2]
            if coords[2] <= coords[0] or coords[3] <= coords[1]:
                continue
            crop = image.crop(coords)
            factor = min(3., 140/max(1, crop.height), 1200/max(1, crop.width))
            if factor > 1:
                crop = crop.resize((round(crop.width*factor), round(crop.height*factor)), Image.Resampling.LANCZOS)
            crop = ImageOps.expand(crop, border=10, fill='white')
            if pixels+crop.width*crop.height > 12_000_000:
                return jobs, True
            pixels += crop.width*crop.height
            jobs.append((table, cell, crop))
    return jobs, False


def retain_cell_candidates(jobs, pages):
    differences = 0
    for number, (_, cell, _) in enumerate(jobs, 1):
        value, blocks = line_blocks(pages[number])
        cell['verification'] = 'same_engine_crop_agreement' if value == cell['text'] else 'cell_ocr_candidate'
        if value and value != cell['text']:
            differences += 1
            cell.update(ocr_disagreement=True, ocr_candidates=[cell['text'], value],
                        crop_ocr_confidence=sum(b['confidence'] for b in blocks)/len(blocks))
        # Keep original rows AND cell.text. Agreement is not human approval;
        # differing decimals/signs are review candidates, never silent updates.
    return differences
