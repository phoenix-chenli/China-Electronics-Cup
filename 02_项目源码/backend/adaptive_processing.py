"""Measured PDF page routing and separate, unverified OCR comparisons.

Geometry is from the original page. Routing never uses a complexity score to
replace native business text; sparse marginal text over a scanned page is an
explicit, separately recorded exception. No model calls or numeric correction.
"""
from __future__ import annotations
from collections import Counter
from itertools import islice
import copy
import hashlib
import json
import math
import re

VERSION = 'pdf-adaptive-routing-1'
MAX_BOXES = 3000
MAX_OBSERVATION_CHARS = 24000
MAX_OBSERVATION_BLOCKS = 300
MAX_COMPARISON_CHARS = 36000
_NUMERIC = re.compile(r'(?<![\d.])[+-]?\d+(?:\.\d+)?(?:%|％)?')


def valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4 and
            all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box) and
            box[0] < box[2] and box[1] < box[3])


def union_area(boxes):
    """Exact rectangle union; overlapping/nested images do not inflate coverage."""
    boxes = [tuple(b) for b in boxes if valid_box(b)][:MAX_BOXES]
    events = []
    for i, b in enumerate(boxes):
        events.extend([(b[0], 1, i), (b[2], -1, i)])
    active, last, total = set(), 0., 0.
    for x, kind, i in sorted(events):
        if x > last and active:
            intervals = sorted((boxes[j][1], boxes[j][3]) for j in active)
            low, high = intervals[0]; length = 0.
            for a, b in intervals[1:]:
                if a > high:
                    length += high - low; low, high = a, b
                else:
                    high = max(high, b)
            total += (x - last) * (length + high - low)
        if kind == 1: active.add(i)
        else: active.discard(i)
        last = x
    return round(min(1., max(0., total)), 6)


def geometry(blocks, image_boxes, tables, *, geometry_truncated=False):
    usable = [b for b in blocks if isinstance(b, dict) and valid_box(b.get('bbox'))]
    left = [b for b in usable if b['bbox'][2] < .53 and len(b.get('text', '')) > 15]
    right = [b for b in usable if b['bbox'][0] > .47 and len(b.get('text', '')) > 15]
    boxes = [list(b) for b in image_boxes if valid_box(b)]
    return {
        'bbox_source': 'pymupdf_original_page_geometry',
        'coordinate_system': 'normalized_original_page_after_declared_rotation',
        'native_bbox_coverage': union_area([b['bbox'] for b in usable]),
        'image_bbox_coverage': union_area(boxes),
        'image_boxes': boxes[:MAX_BOXES],
        'geometry_truncated': bool(geometry_truncated or len(blocks) > MAX_BOXES or len(boxes) > MAX_BOXES),
        'multi_column_candidate': len(left) >= 3 and len(right) >= 3,
        'native_table_count': len(tables),
        'merged_table_count': sum(bool(t.get('has_merged_cells')) for t in tables),
        'formula_candidate_count': sum(b.get('kind') == 'formula_candidate' for b in blocks),
    }


def _marginal_native(text, blocks, features):
    """Geometry/explicit watermark only, not guessed text reliability."""
    if (features.get('geometry_truncated') or features.get('image_bbox_coverage', 0) < .60 or
            features.get('native_bbox_coverage', 1) > .035 or len(text) > 120 or not blocks or len(blocks) > 3):
        return None
    if not all(valid_box(b.get('bbox')) for b in blocks):return None
    if all(b['bbox'][3] <= .12 or b['bbox'][1] >= .88 for b in blocks):return 'marginal_native_over_page_image'
    if len(blocks) <= 2 and all(b['bbox'][3] <= .18 for b in blocks):return 'isolated_header_over_page_image'
    if len(text) <= 40 and re.fullmatch(r'\s*(?:水印|机密|内部资料|CONFIDENTIAL|DRAFT)\s*', text, re.I):
        return 'explicit_watermark_over_page_image'
    return None


def plan(extracted, text, quality):
    blocks = extracted.get('blocks', [])
    f = copy.deepcopy(extracted.get('page_features') or geometry(blocks, [], extracted.get('tables', [])))
    f.update(native_characters=len(text), native_block_count=len(blocks), has_image=bool(extracted.get('has_image')))
    f['quality'] = {k: quality[k] for k in ('blank','contrast','sharpness','grayscale_range','dark_pixel_ratio') if k in quality}
    reasons, primary_basis = [], None
    if quality.get('blank'):
        route = 'native_only' if text else 'blank_no_ocr'; reasons = ['measured_near_uniform_page']
    elif not text:
        route = 'ocr_primary'; reasons = ['no_native_text_nonblank_page']
    elif (primary_basis := _marginal_native(text, blocks, f)):
        route = 'ocr_primary'; reasons = [primary_basis]
    else:
        if f.get('image_bbox_coverage', 0) >= .08:reasons.append('substantial_embedded_image')
        elif f['has_image'] and (len(text) < 150 or f.get('geometry_truncated') or not f.get('image_boxes')):
            reasons.append('image_coverage_uncertain_or_sparse_native')
        if f.get('multi_column_candidate'):reasons.append('measured_two_column_candidate')
        if f.get('native_table_count'):reasons.append('native_table_relationship_check')
        if f.get('formula_candidate_count'):reasons.append('native_formula_structure_check')
        if quality.get('contrast') is not None and quality['contrast'] < 15:reasons.append('measured_low_contrast')
        if quality.get('sharpness') is not None and quality['sharpness'] < 45:reasons.append('measured_low_sharpness')
        route = 'native_with_ocr_comparison' if reasons else 'native_only'
        if not reasons:reasons = ['native_text_without_observed_ocr_trigger']
    return dict(schema_version=VERSION, requested_route=route, reasons=reasons, features=f,
                native_primary_exception=primary_basis, executed=False, status='planned',
                ocr_attempted=False, verification_status='not_verified',
                note='规则决定处理路径，不是OCR正确率或已核验业务语义；原页始终保留。')


def _bounded_blocks(blocks, *, max_chars=MAX_OBSERVATION_CHARS):
    selected, used = [], 0
    for b in blocks:
        text = b.get('text') if isinstance(b, dict) else None
        if not isinstance(text, str) or not text or not valid_box(b.get('bbox')):continue
        if len(selected) >= MAX_OBSERVATION_BLOCKS or used + len(text) > max_chars:break
        # Retain OCR engine confidence exactly; never turn it into correctness.
        item = {k: copy.deepcopy(b[k]) for k in ('text','bbox','confidence','kind') if k in b}
        item.update(provenance='local_ocr_original_page', verification_status='unverified')
        selected.append(item); used += len(text)
    return selected, len(selected) < len(blocks)


def comparison(native_text, native_blocks, recognized, routing):
    ocr_text = recognized.get('text', '')
    observations, truncated = _bounded_blocks(recognized.get('blocks', []))
    native_excerpt = native_text[:MAX_COMPARISON_CHARS//2]
    ocr_excerpt = ocr_text[:MAX_COMPARISON_CHARS//2]
    numeric_native, numeric_ocr = Counter(_NUMERIC.findall(native_text)), Counter(_NUMERIC.findall(ocr_text))
    text_diff = re.sub(r'\s+', '', native_text) != re.sub(r'\s+', '', ocr_text)
    numeric_diff = numeric_native != numeric_ocr
    return dict(schema_version=VERSION, native_text=native_excerpt, ocr_text=ocr_excerpt,
                native_text_sha256=hashlib.sha256(native_text.encode()).hexdigest(),
                ocr_text_sha256=hashlib.sha256(ocr_text.encode()).hexdigest(),
                text_disagreement=text_diff, numeric_token_disagreement=numeric_diff,
                native_numeric_tokens=list(islice(numeric_native.elements(),256)),
                ocr_numeric_tokens=list(islice(numeric_ocr.elements(),256)),
                numeric_tokens_truncated=sum(numeric_native.values())>256 or sum(numeric_ocr.values())>256,
                observations=observations, native_blocks=copy.deepcopy(native_blocks[:3]) if routing.get('native_primary_exception') else [],
                selected='ocr' if routing['requested_route'] == 'ocr_primary' and ocr_text else 'native',
                truncated=truncated or len(native_text) > len(native_excerpt) or len(ocr_text) > len(ocr_excerpt),
                requires_original_page_review=bool(text_diff or numeric_diff),
                provenance='separate_local_ocr_comparison_not_independent_source',
                evidence_status='unverified_ocr_candidate',
                note='不同包含新增图内文字或识别错误的可能；不据长度/置信度换原生数值，不断言原件冲突。')


def chart_observations(compare):
    """Existing chart reader can inspect original OCR blocks under its own gates."""
    return copy.deepcopy(compare['observations'])
