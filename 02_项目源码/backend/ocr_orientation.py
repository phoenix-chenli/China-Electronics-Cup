"""Bounded OCR orientation diagnostics; engine scores are not accuracy claims."""
from __future__ import annotations

import math

from .ocr_native import inverse_box


def reading_quality(words, size=None):
    characters = sum(len(w['text']) for w in words)
    weights = [min(30, sum(c.isalnum() for c in w['text'])) for w in words]
    total = sum(weights)
    mean = sum(n*w['confidence'] for n, w in zip(weights, words))/max(1, total)
    reliable = sum(n for n, w in zip(weights, words) if w['confidence'] >= .6)/max(1, total)
    lexical = sum(sum(c.isalnum() for c in w['text']) for w in words)/max(1, characters)
    directional = []
    if size:
        width, height = size
        for w in words:
            # Single CJK characters and numeric tables do not establish writing
            # direction. Long alphabetic/CJK observations are only an adjunct.
            letters = sum(c.isalpha() for c in w['text'])
            if letters >= 3 and w['confidence'] >= .5:
                b = w['bbox']
                directional.append((min(30, letters), (b[2]-b[0])*width >= (b[3]-b[1])*height))
    horizontal = (sum(n for n, aligned in directional if aligned)/sum(n for n, _ in directional)
                  if len(directional) >= 5 else None)
    geometry = 1. if horizontal is None else .2+.8*horizontal
    strength = sum(n*w['confidence']**3 for n, w in zip(weights, words))/max(1, total)
    coverage = .25+.75*min(1., math.log1p(total)/math.log1p(120))
    usable = bool(total and lexical >= .45 and mean >= .55 and reliable >= .4
                  and (horizontal is None or horizontal >= .45))
    strong = usable and mean >= .82 and reliable >= .8 and lexical >= .6
    return dict(score=round(strength*geometry*coverage, 6), mean_confidence=round(mean, 6),
                reliable_fraction=round(reliable, 6), alnum_fraction=round(lexical, 6),
                horizontal_fraction=round(horizontal, 6) if horizontal is not None else None,
                directional_words=len(directional), text_characters=characters,
                word_count=len(words), usable=usable, strong=bool(strong))


def choose_orientation(candidates):
    usable = [c for c in candidates if c['quality']['usable']]
    if not usable:
        return None, False
    usable.sort(key=lambda c: (-c['quality']['score'], c['rotation'] != 0, c['rotation']))
    best = usable[0]
    close = [c for c in usable if best['quality']['score']-c['quality']['score'] <= max(.025, best['quality']['score']*.08)]
    original = next((c for c in close if c['rotation'] == 0), None)
    if original is not None:
        best = original
    return best, len(close) > 1


def remap_result(blocks, tables, rotation):
    """Keep corrected reading order and table indices, map only source anchors."""
    if rotation == 0:
        return
    for block in blocks:
        block['bbox'] = inverse_box(block['bbox'], rotation)
    for table in tables:
        table['bbox'] = inverse_box(table['bbox'], rotation)
        for cell in table.get('cells', []):
            cell['bbox'] = inverse_box(cell['bbox'], rotation)
