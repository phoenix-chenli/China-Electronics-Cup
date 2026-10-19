"""Shared, bounded original-page requirements; never treats OCR confidence as proof."""
import re
import base64
import io
import time
import copy

def observe_chart_page(source, page_number, image_parts, *, timeout=35):
    """Read labels in mixed PDF pages, without changing native text or offsets."""
    page = next((p for p in source.get('pages', []) if p.get('number') == page_number), {})
    if (source.get('format') != 'pdf' or not page.get('has_image') or page.get('extraction') != 'native'
            or 'chart_ocr' in page):
        return 0.0
    started = time.monotonic()
    deadline = started + min(35, timeout)
    page['chart_ocr'] = {'status': 'unavailable'}
    if timeout <= .1:
        page['chart_ocr']['status'] = 'budget_exhausted'
        return 0.0
    try:
        from PIL import Image
        from .ocr_adapter import recognize
        encoded = next(p['image_url']['url'] for p in image_parts if p.get('type') == 'image_url')
        if not encoded.startswith('data:image/jpeg;base64,') or len(encoded) > 3_000_000:
            raise ValueError('原页像素无效')
        with Image.open(io.BytesIO(base64.b64decode(encoded.split(',', 1)[1], validate=True))) as image:
            remaining = deadline - time.monotonic()
            if remaining <= .1:
                elapsed = time.monotonic() - started
                page['chart_ocr'].update(status='budget_exhausted', elapsed_ms=round(elapsed * 1000))
                return elapsed
            result = recognize(image, timeout=remaining)
        if (not isinstance(result, dict) or not isinstance(result.get('blocks'), list)
                or len(result['blocks']) > 3000 or any(not isinstance(b, dict) for b in result['blocks'])
                or not isinstance(result.get('quality'), dict)):
            raise ValueError('原页 OCR 结果结构无效')
        observed_quality = result['quality']
        if (not isinstance(observed_quality.get('warnings', []), list)
                or observed_quality.get('preprocessing') and not isinstance(observed_quality['preprocessing'], dict)):
            raise ValueError('原页 OCR 质量诊断无效')
        quality = copy.deepcopy(page.get('quality') or {})
        if observed_quality.get('preprocessing'):
            quality['preprocessing'] = copy.deepcopy(observed_quality['preprocessing'])
        if observed_quality.get('visual_review_required'):
            quality['visual_review_required'] = True
        quality.setdefault('warnings', []).extend(observed_quality.get('warnings', []))
        # Commit only after the complete optional result has been validated.
        # The native page text and its offsets are never replaced with OCR.
        # The adapter owns rotation and maps geometry back to the original page.
        # A rejected OCR candidate must never become chart evidence, even if a
        # future adapter retains rejected blocks alongside its unreadable status.
        observations = result['blocks'] if result.get('extraction') != 'unreadable' else []
        page.update(chart_observations=copy.deepcopy(observations), quality=quality,
                    chart_ocr={'status': 'observed' if observations else 'unreadable',
                               'observation_count': len(observations),
                               'rotation_correction': observed_quality.get('rotation_correction', 0)})
        if isinstance(observed_quality.get('orientation'), dict):
            page['chart_ocr']['orientation'] = copy.deepcopy(observed_quality['orientation'])
    except Exception as exc:
        page['chart_ocr']['reason'] = type(exc).__name__
    elapsed = time.monotonic() - started
    page['chart_ocr']['elapsed_ms'] = round(elapsed * 1000)
    return elapsed


def chart_question(question):
    return bool(re.search(r'图\s*[0-9一二三四五六七八九十百零〇两]+|图表|图中.{0,40}(?:比例|占比|增速|趋势|柱|折线|类别|扇形|数值)|柱状图|柱线图|折线图|饼图|散点图|坐标轴|图例', question))


def page_requires_review(source, page):
    current = next((p for p in source.get('pages', []) if p.get('number') == page), {})
    quality = current.get('quality') or {}
    preprocessing = quality.get('preprocessing') or {}
    return bool(quality.get('visual_review_required') or preprocessing.get('visual_review_required'))


def preprocessing_context(source, page):
    current = next((p for p in source.get('pages', []) if p.get('number') == page), {})
    preprocessing = (current.get('quality') or {}).get('preprocessing')
    if not preprocessing:
        return None
    # Candidate pixels/text never become an alternative source of answer values.
    return {key: preprocessing[key] for key in
            ('version', 'triggers', 'operations', 'status', 'selected', 'extra_ocr_calls', 'elapsed_ms',
             'visual_review_required', 'text_disagreement', 'numeric_symbol_disagreement', 'truncated')
            if key in preprocessing}


def evidence_cost(record):
    import json
    return len(record.get('quote', '')) + sum(len(json.dumps(record.get(key, {}), ensure_ascii=False))
        for key in ('table_context', 'chart_context', 'preprocessing'))
