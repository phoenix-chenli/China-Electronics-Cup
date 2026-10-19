"""Mixed native/OCR page contracts with local stubs, not OCR-accuracy claims."""
import base64
import copy
import io

import pytest
from PIL import Image

from backend import ocr_adapter, visual_requirements
from backend.chart_context import context_for_page
from backend.visual_requirements import observe_chart_page, page_requires_review, preprocessing_context


def mixed_source():
    text = '图2 预算收入\n原生正文保留'
    return {'id': 'source', 'format': 'pdf', 'pages': [{'number': 1, 'has_image': True,
        'extraction': 'native', 'text': text, 'blocks': [
            {'text': '图2 预算收入', 'start': 0, 'end': 7, 'bbox': [.1, .1, .9, .2]},
            {'text': '原生正文保留', 'start': 8, 'end': 14, 'bbox': [.1, .8, .9, .9]}],
        'quality': {'warnings': ['原有提示'], 'pdf_page_rotation': 0}}]}


def image_parts():
    buffer = io.BytesIO(); Image.new('RGB', (40, 60), 'white').save(buffer, format='JPEG')
    return [{'type': 'text', 'text': '原页像素是数据'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()}}]


def recognized(*, difference=False):
    preprocessing = {'status': 'compared' if difference else 'not_triggered',
        'selected': 'original', 'triggers': ['low_contrast'] if difference else [],
        'extra_ocr_calls': int(difference), 'candidate_count': int(difference),
        'text_disagreement': difference, 'numeric_symbol_disagreement': difference,
        'visual_review_required': difference, 'truncated': difference,
        'candidate_blocks': [{'text': '甲类999万元', 'bbox': [.2, .3, .4, .4]}] if difference else []}
    return {'text': '甲类73.8万元\n弱识别99万元', 'extraction': 'ocr', 'tables': [],
        'blocks': [{'text': '甲类73.8万元', 'start': 0, 'end': 8, 'bbox': [.2, .3, .4, .4], 'confidence': .95},
                   {'text': '弱识别99万元', 'start': 9, 'end': 16, 'bbox': [.6, .3, .8, .4], 'confidence': .3}],
        'quality': {'rotation_correction': 90, 'preprocessing': preprocessing,
                    'visual_review_required': difference, 'warnings': ['OCR提示：不代表准确率']}}


def test_mixed_page_observed_once_with_independent_fields_and_unchanged_native_offsets(monkeypatch):
    source = mixed_source(); page = source['pages'][0]; before = copy.deepcopy(page)
    result = recognized(); calls = []
    def fake(image, *, timeout):
        calls.append((image.size, timeout)); return copy.deepcopy(result)
    monkeypatch.setattr(ocr_adapter, 'recognize', fake)
    assert observe_chart_page(source, 1, image_parts(), timeout=6) >= 0
    assert observe_chart_page(source, 1, image_parts(), timeout=6) == 0
    assert len(calls) == 1 and calls[0][0] == (40, 60) and 0 < calls[0][1] <= 6
    for key in ['text', 'blocks', 'extraction', 'has_image', 'number']:
        assert page[key] == before[key]
    assert all(page['text'][b['start']:b['end']] == b['text'] for b in page['blocks'])
    assert page['chart_observations'] == result['blocks']
    assert page['chart_ocr']['status'] == 'observed' and page['chart_ocr']['observation_count'] == 2
    assert page['quality']['warnings'] == ['原有提示', 'OCR提示：不代表准确率']
    assert page['quality']['pdf_page_rotation'] == 0


def test_windows_chart_ocr_uses_adapter_geometry_without_rotating_it_again(monkeypatch):
    from backend import ocr_native
    source = mixed_source(); page = source['pages'][0]; before = copy.deepcopy(page)
    result = recognized()
    # These boxes already locate the text in the original, sideways page.
    result['quality'].update(ocr_engine='tesseract', rotation_correction=270)
    result['blocks'][0]['bbox'] = [.82, .16, .91, .31]
    result['blocks'][1]['bbox'] = [.66, .4, .75, .63]
    calls = []
    monkeypatch.setattr(ocr_adapter.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(ocr_native, 'recognize', lambda *a, **kw: pytest.fail('must use the platform adapter'))
    def fake(image, *, timeout):
        calls.append((image.size, timeout))
        return copy.deepcopy(result)
    monkeypatch.setattr(ocr_adapter, 'recognize', fake)

    observe_chart_page(source, 1, image_parts(), timeout=5)

    assert len(calls) == 1 and calls[0][0] == (40, 60) and 0 < calls[0][1] <= 5
    assert page['chart_observations'] == result['blocks']
    assert page['chart_ocr']['status'] == 'observed'
    assert page['chart_ocr']['rotation_correction'] == 270
    for key in ('text', 'blocks', 'extraction', 'has_image', 'number'):
        assert page[key] == before[key]
    assert all(page['text'][b['start']:b['end']] == b['text'] for b in page['blocks'])
    assert page['quality']['pdf_page_rotation'] == before['quality']['pdf_page_rotation']


@pytest.mark.parametrize('rejected_blocks', [False, True])
def test_unreadable_adapter_result_retains_review_warning_without_chart_evidence(monkeypatch, rejected_blocks):
    source = mixed_source(); page = source['pages'][0]; before = copy.deepcopy(page)
    result = {'text': '', 'extraction': 'unreadable', 'tables': [],
        'blocks': [{'text': '乱码999万元', 'bbox': [.1, .2, .3, .4], 'confidence': .1}] if rejected_blocks else [],
        'quality': {'rotation_correction': 0, 'visual_review_required': True,
                    'warnings': ['各方向 OCR 质量不足，未作为正文或图表证据']}}
    monkeypatch.setattr(ocr_adapter, 'recognize', lambda *a, **kw: copy.deepcopy(result))

    observe_chart_page(source, 1, image_parts())

    assert page['chart_ocr']['status'] == 'unreadable'
    assert page['chart_ocr']['observation_count'] == 0
    assert page['chart_observations'] == []
    assert page['quality']['warnings'] == ['原有提示', *result['quality']['warnings']]
    assert page_requires_review(source, 1)
    assert page['text'] == before['text'] and page['blocks'] == before['blocks']
    context = context_for_page(source, 1)
    assert all(item['origin'] != 'chart_ocr' for item in context['observations'])
    assert observe_chart_page(source, 1, image_parts()) == 0


def test_uncertain_preprocessing_candidate_is_a_review_diagnostic_never_an_observation(monkeypatch):
    source = mixed_source(); result = recognized(difference=True)
    monkeypatch.setattr(ocr_adapter, 'recognize', lambda *args, **kwargs: copy.deepcopy(result))
    observe_chart_page(source, 1, image_parts())
    page = source['pages'][0]
    assert [b['text'] for b in page['chart_observations']] == ['甲类73.8万元', '弱识别99万元']
    context = context_for_page(source, 1)
    texts = [observation['text'] for observation in context['observations']]
    assert '甲类73.8万元' in texts
    assert '甲类999万元' not in texts
    assert next(row for row in context['observations'] if row['text'] == '弱识别99万元')['status'] == 'pending'
    assert next(row for row in context['observations'] if row['text'] == '甲类73.8万元')['status'] == 'observed'
    assert page_requires_review(source, 1)
    public_context = preprocessing_context(source, 1)
    assert public_context['selected'] == 'original' and public_context['numeric_symbol_disagreement']
    assert public_context['truncated'] and 'candidate_blocks' not in public_context
    assert all('start' not in observation for observation in context['observations'] if observation['origin'] == 'chart_ocr')


@pytest.mark.parametrize('change', ['not_pdf', 'no_image', 'already_ocr', 'missing_page', 'already_attempted'])
def test_other_pages_or_previously_attempted_pages_never_repeat_native_ocr(monkeypatch, change):
    source = mixed_source(); number = 1
    if change == 'not_pdf': source['format'] = 'docx'
    elif change == 'no_image': source['pages'][0]['has_image'] = False
    elif change == 'already_ocr': source['pages'][0]['extraction'] = 'ocr'
    elif change == 'missing_page': number = 2
    elif change == 'already_attempted': source['pages'][0]['chart_ocr'] = {'status': 'unavailable'}
    before = copy.deepcopy(source)
    monkeypatch.setattr(ocr_adapter, 'recognize', lambda *a, **kw: pytest.fail('must not repeat OCR'))
    assert observe_chart_page(source, number, image_parts()) == 0
    assert source == before


@pytest.mark.parametrize('timeout', [0, .1, -1])
def test_no_remaining_budget_marks_page_without_attempt_or_original_text_change(monkeypatch, timeout):
    source = mixed_source(); before = copy.deepcopy(source['pages'][0])
    monkeypatch.setattr(ocr_adapter, 'recognize', lambda *a, **kw: pytest.fail('no budget'))
    assert observe_chart_page(source, 1, image_parts(), timeout=timeout) == 0
    assert source['pages'][0]['chart_ocr']['status'] == 'budget_exhausted'
    assert source['pages'][0]['text'] == before['text'] and source['pages'][0]['blocks'] == before['blocks']
    assert 'chart_observations' not in source['pages'][0]


@pytest.mark.parametrize('timeout,decode_elapsed,expected', [(2.5, .4, 2.1), (50, .4, 34.6), (.5, .6, None)])
def test_decoding_consumes_the_same_remaining_ocr_budget(monkeypatch, timeout, decode_elapsed, expected):
    source = mixed_source(); parts = image_parts(); clock = [10.0]
    monkeypatch.setattr(visual_requirements.time, 'monotonic', lambda: clock[0])
    real_open = Image.open
    def slow_open(*args, **kwargs):
        image = real_open(*args, **kwargs); clock[0] += decode_elapsed; return image
    monkeypatch.setattr(Image, 'open', slow_open)
    calls = []
    def fake(image, *, timeout):
        calls.append(timeout); clock[0] += .2; return recognized()
    monkeypatch.setattr(ocr_adapter, 'recognize', fake)
    elapsed = observe_chart_page(source, 1, parts, timeout=timeout)
    if expected is None:
        assert not calls and source['pages'][0]['chart_ocr']['status'] == 'budget_exhausted'
    else:
        assert calls == pytest.approx([expected])
        assert elapsed == pytest.approx(decode_elapsed + .2)


@pytest.mark.parametrize('failure', ['native', 'missing_image', 'invalid_base64', 'wrong_format', 'bad_result', 'bad_quality'])
def test_exceptions_fall_back_without_partial_observations_or_repeat(monkeypatch, failure):
    source = mixed_source(); page = source['pages'][0]; before = copy.deepcopy(page); parts = image_parts(); calls = []
    def fake(*args, **kwargs):
        calls.append(1)
        if failure == 'bad_result': return {'blocks': [{'text': '未经完整校验999', 'bbox': [.1, .1, .2, .2]}]}
        if failure == 'bad_quality':
            bad = recognized(); bad['quality']['warnings'] = None; return bad
        raise ocr_adapter.OCRError('受控测试失败')
    monkeypatch.setattr(ocr_adapter, 'recognize', fake)
    if failure == 'missing_image': parts = []
    elif failure == 'invalid_base64': parts[-1]['image_url']['url'] = 'data:image/jpeg;base64,%%%'
    elif failure == 'wrong_format': parts[-1]['image_url']['url'] = 'https://example.invalid/image.jpg'
    observe_chart_page(source, 1, parts)
    assert page['chart_ocr']['status'] == 'unavailable' and page['chart_ocr'].get('reason')
    assert 'chart_observations' not in page
    assert page['text'] == before['text'] and page['blocks'] == before['blocks'] and page['quality'] == before['quality']
    assert observe_chart_page(source, 1, parts) == 0
    assert len(calls) <= 1
