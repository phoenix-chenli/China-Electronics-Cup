"""One geometry-preserving OCR candidate; diagnostics are not accuracy scores."""
from __future__ import annotations

import re

from PIL import ImageFilter, ImageOps

MAX_AUDIT_BLOCKS = 64
MAX_AUDIT_CHARACTERS = 4000
_CRITICAL = re.compile(r'[-+−]?\d+(?:[.,]\d+)*(?:\s*[%％])?|[=<>≤≥≠±×÷∑∫√^%％‰‱°℃$¥￥€£/／()\[\]{}（）+−–—-]')


def preprocessing_plan(image, quality):
    """Trigger only on measured image problems, never on engine confidence."""
    triggers, operations = [], []
    if not quality.get('blank'):
        # A solid black ink crop has zero standard deviation despite clear
        # black/white contrast. Require low full-image dynamic range as well.
        if quality.get('contrast', 255) < 15 and quality.get('grayscale_range', 255) < 80:
            triggers.append('low_contrast'); operations.append('autocontrast')
        if min(image.size) < 350:
            triggers.append('small_image'); operations.append('mild_unsharp_mask')
    return {'triggers': triggers, 'operations': operations, 'candidate_count': 0,
            'extra_ocr_calls': 0, 'selected': 'original',
            'status': 'pending' if triggers else 'not_triggered',
            'elapsed_ms': 0, 'text_disagreement': False, 'numeric_symbol_disagreement': False,
            'visual_review_required': False, 'truncated': False,
            'trigger_thresholds': {'contrast_below': 15, 'grayscale_range_below': 80, 'short_side_below': 350},
            'geometry': 'same_size_no_crop_or_warp',
            'note': '候选只用于有界对照；图像诊断和引擎评分均不代表文字正确率。'}


def enhance(image, operations):
    """Keep dimensions and normalized coordinates exactly unchanged."""
    result = image.copy()
    if 'autocontrast' in operations:
        result = ImageOps.autocontrast(result, cutoff=0)
    if 'mild_unsharp_mask' in operations:
        result = result.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=3))
    return result


def ordered_blocks(result):
    return sorted(result.get('blocks', []), key=lambda b: (round(b['bbox'][1] / .008), b['bbox'][0]))


def comparison(original, candidate, rotation, box_mapper):
    """Compare full outputs in memory; retain only a bounded review snapshot.

    No fuzzy number equivalence: signs, decimal punctuation, percentages and
    formula symbols remain significant. Whitespace alone is not a new value.
    """
    before = '\n'.join(str(b['text']).strip() for b in ordered_blocks(original))
    after = '\n'.join(str(b['text']).strip() for b in ordered_blocks(candidate))
    original_tokens = _CRITICAL.findall(before)
    candidate_tokens = _CRITICAL.findall(after)
    # Keep word/token boundaries: "1 2" is not "12", nor "now here" "nowhere".
    differs = re.sub(r'\s+', ' ', before).strip() != re.sub(r'\s+', ' ', after).strip()
    critical = [re.sub(r'\s+', '', t) for t in original_tokens] != [re.sub(r'\s+', '', t) for t in candidate_tokens]
    audit = {'text_disagreement': differs, 'numeric_symbol_disagreement': critical,
             'visual_review_required': differs or critical, 'truncated': False,
             'candidate_blocks': [], 'candidate_rotation': rotation,
             'candidate_coordinate_space': 'original_preview_normalized'}
    remaining = MAX_AUDIT_CHARACTERS
    # Retain small exact symbol/token sequences before spending the audit budget
    # on prose. Any truncation is visible and can never authorize replacement.
    for key, tokens in [('original_critical_tokens', original_tokens), ('candidate_critical_tokens', candidate_tokens)]:
        kept, spent = [], 0
        for token in tokens:
            if len(kept) >= 32 or spent + len(token) > 512:
                audit['truncated'] = True; break
            kept.append(token); spent += len(token)
        audit[key] = kept; remaining -= spent
    audit['original_excerpt'] = before[:min(800, remaining)]
    remaining -= len(audit['original_excerpt'])
    audit['truncated'] |= len(audit['original_excerpt']) < len(before)
    blocks = ordered_blocks(candidate)
    for block in blocks[:MAX_AUDIT_BLOCKS]:
        value = str(block['text']).strip()
        if len(value) > remaining:
            audit['truncated'] = True
            break
        audit['candidate_blocks'].append({'text': value, 'bbox': box_mapper(block['bbox'], rotation)})
        remaining -= len(value)
    audit['truncated'] |= len(audit['candidate_blocks']) < len(blocks)
    audit['audit_characters'] = MAX_AUDIT_CHARACTERS - remaining
    return audit
