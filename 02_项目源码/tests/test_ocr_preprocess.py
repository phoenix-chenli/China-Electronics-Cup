"""Bounded local stubs; no Vision, model, service, or source-image writes."""
import copy
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from backend import ocr_native
from backend.ocr_preprocess import comparison, enhance, preprocessing_plan


def page(*, pale=False, small=False):
    image = Image.new('RGB', (240, 180) if small else (600, 400), (250, 250, 250) if pale else 'white')
    ImageDraw.Draw(image).rectangle((10, 10, image.width // 2, image.height // 2), fill=(240, 240, 240) if pale else 'black')
    return image


def result(text='工业 -7.2%', confidence=.7, bbox=None):
    return {'blocks': [{'text': text, 'confidence': confidence, 'bbox': bbox or [.1, .2, .7, .3], 'angle': 0}]}


def fake_ocr(monkeypatch, candidate=None, baseline=None, rotation=0, clock=None, base_elapsed=0):
    calls = []
    baseline = baseline or result()
    monkeypatch.setattr(ocr_native, '_binary', lambda **kw: Path('/unused-ocr'))
    monkeypatch.setattr(ocr_native, 'detect_grid', lambda *a: [])
    def run(args, **kwargs):
        calls.append({'timeout': kwargs['timeout'], 'sizes': [Image.open(p).size for p in args[1:]]})
        if len(calls) == 1:
            if clock is not None:
                clock[0] += base_elapsed
            values = [{'blocks': []} for _ in range(4)]; values[rotation // 90] = baseline
        else:
            if isinstance(candidate, Exception):
                raise candidate
            values = [candidate or result(confidence=.99)]
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(values).encode())
    monkeypatch.setattr(ocr_native.subprocess, 'run', run)
    return calls


def test_normal_page_keeps_four_orientations_and_zero_extra_ocr(monkeypatch):
    calls = fake_ocr(monkeypatch)
    recognized = ocr_native.recognize(page())
    audit = recognized['quality']['preprocessing']
    assert len(calls) == 1 and len(calls[0]['sizes']) == 4
    assert audit['triggers'] == [] and audit['extra_ocr_calls'] == 0 and audit['candidate_count'] == 0
    assert audit['status'] == 'not_triggered' and audit['selected'] == 'original'
    assert not recognized['quality']['visual_review_required']


@pytest.mark.parametrize('pale,small,triggers', [(True, False, ['low_contrast']), (False, True, ['small_image']),
                                               (True, True, ['low_contrast', 'small_image'])])
def test_quality_trigger_adds_only_one_same_size_candidate_and_preserves_input(monkeypatch, pale, small, triggers):
    image = page(pale=pale, small=small); before = image.tobytes()
    calls = fake_ocr(monkeypatch)
    recognized = ocr_native.recognize(image)
    audit = recognized['quality']['preprocessing']
    assert image.tobytes() == before and len(calls) == 2
    assert len(calls[0]['sizes']) == 4 and calls[1]['sizes'] == [image.size]
    assert audit['triggers'] == triggers and audit['extra_ocr_calls'] == audit['candidate_count'] == 1
    assert audit['selected'] == 'enhanced' and not audit['text_disagreement']
    assert recognized['text'] == '工业 -7.2%'
    assert '不代表' in audit['note'] and audit['elapsed_ms'] >= 0


@pytest.mark.parametrize('candidate', ['工业 7.2%', '工业 -7.2', '工业 -72%', '工业 -7.2‰',
                                      '工业 +7.2%', '工业 -7.2% 5', '工业 -7.2% ≥ 5'])
def test_numeric_or_symbol_disagreement_never_replaces_original_even_at_higher_score(monkeypatch, candidate):
    calls = fake_ocr(monkeypatch, result(candidate, .99))
    recognized = ocr_native.recognize(page(pale=True))
    audit = recognized['quality']['preprocessing']
    assert len(calls) == 2 and audit['selected'] == 'original'
    assert audit['text_disagreement'] and audit['numeric_symbol_disagreement'] and audit['visual_review_required']
    assert recognized['quality']['visual_review_required']
    assert recognized['text'] == '工业 -7.2%'
    assert audit['candidate_blocks'][0]['text'] == candidate
    assert any('数字或符号' in warning for warning in recognized['quality']['warnings'])


def test_plain_text_disagreement_is_also_retained_for_review(monkeypatch):
    fake_ocr(monkeypatch, result('农业 -7.2%', .99))
    recognized = ocr_native.recognize(page(pale=True))
    audit = recognized['quality']['preprocessing']
    assert recognized['text'].startswith('工业') and audit['text_disagreement']
    assert not audit['numeric_symbol_disagreement'] and audit['visual_review_required']


def test_candidate_geometry_is_mapped_back_through_original_orientation(monkeypatch):
    image = page(pale=True)
    calls = fake_ocr(monkeypatch, rotation=90)
    recognized = ocr_native.recognize(image)
    audit = recognized['quality']['preprocessing']
    expected = ocr_native.inverse_box([.1, .2, .7, .3], 90)
    assert calls[1]['sizes'] == [(image.height, image.width)]
    assert recognized['blocks'][0]['bbox'] == expected == audit['candidate_blocks'][0]['bbox']
    assert audit['candidate_rotation'] == 90
    assert recognized['text'][recognized['blocks'][0]['start']:recognized['blocks'][0]['end']] == recognized['blocks'][0]['text']


def test_candidate_shares_original_deadline_and_does_not_add_timeout(monkeypatch):
    clock = [100.0]; monkeypatch.setattr(ocr_native.time, 'monotonic', lambda: clock[0])
    calls = fake_ocr(monkeypatch, clock=clock, base_elapsed=32)
    recognized = ocr_native.recognize(page(pale=True), timeout=35)
    assert calls[0]['timeout'] == 35 and calls[1]['timeout'] == 3
    assert recognized['quality']['elapsed_ms'] == 32000


def test_exhausted_budget_skips_extra_candidate_without_losing_original(monkeypatch):
    clock = [100.0]; monkeypatch.setattr(ocr_native.time, 'monotonic', lambda: clock[0])
    calls = fake_ocr(monkeypatch, clock=clock, base_elapsed=34.5)
    recognized = ocr_native.recognize(page(pale=True), timeout=35)
    audit = recognized['quality']['preprocessing']
    assert len(calls) == 1 and audit['status'] == 'skipped_budget'
    assert audit['extra_ocr_calls'] == audit['candidate_count'] == 0 and recognized['text'] == '工业 -7.2%'


@pytest.mark.parametrize('candidate,status', [(subprocess.TimeoutExpired('ocr', 1), 'candidate_timeout'),
                                            ({'error': 'unavailable', 'blocks': []}, 'candidate_failed'),
                                            ({'blocks': [{'text': 'malformed'}]}, 'candidate_failed')])
def test_optional_candidate_failure_is_not_a_page_failure_or_retry(monkeypatch, candidate, status):
    calls = fake_ocr(monkeypatch, candidate)
    recognized = ocr_native.recognize(page(pale=True))
    audit = recognized['quality']['preprocessing']
    assert len(calls) == 2 and audit['extra_ocr_calls'] == 1 and audit['status'] == status
    assert recognized['text'] == '工业 -7.2%' and audit['selected'] == 'original'


def test_blank_page_never_creates_candidate_or_invokes_native_ocr(monkeypatch):
    monkeypatch.setattr(ocr_native, '_binary', lambda **kw: pytest.fail('blank must not invoke native OCR'))
    recognized = ocr_native.recognize(Image.new('RGB', (100, 100), 'white'))
    assert recognized['quality']['preprocessing']['extra_ocr_calls'] == 0
    assert recognized['quality']['preprocessing']['triggers'] == []


def test_review_snapshot_is_bounded_while_comparison_uses_every_block():
    before = {'blocks': [result('文字' * 40 + str(i))['blocks'][0] for i in range(100)]}
    after = copy.deepcopy(before); after['blocks'][-1]['text'] += '-7%'
    audit = comparison(before, after, 0, ocr_native.inverse_box)
    assert audit['text_disagreement'] and audit['numeric_symbol_disagreement'] and audit['truncated']
    assert len(audit['candidate_blocks']) <= 64 and audit['audit_characters'] <= 4000


def test_enhancement_is_geometry_preserving_and_does_not_turn_blur_alone_into_trigger():
    image = page(pale=True, small=True); before = image.tobytes()
    plan = preprocessing_plan(image, ocr_native.quality_metrics(image))
    enhanced = enhance(image, plan['operations'])
    assert image.tobytes() == before and enhanced.size == image.size
    assert preprocessing_plan(page(), {'blank': False, 'contrast': 60, 'sharpness': 1})['triggers'] == []


def test_grid_cell_review_receives_same_deadline_after_candidate(monkeypatch):
    clock = [100.0]; monkeypatch.setattr(ocr_native.time, 'monotonic', lambda: clock[0])
    fake_ocr(monkeypatch, clock=clock, base_elapsed=32)
    monkeypatch.setattr(ocr_native, 'detect_grid', lambda *args: [{'cells': []}])
    deadlines = []
    def cell_review(image, tables, binary, deadline, rotation):
        deadlines.append(deadline); return tables, []
    monkeypatch.setattr(ocr_native, '_recognize_cells', cell_review)
    ocr_native.recognize(page(pale=True), timeout=35)
    assert deadlines == [135.0]


def test_truncated_candidate_never_replaces_original_despite_equal_full_text(monkeypatch):
    original = result('文字' * 2500 + '-7%', .7)
    enhanced = copy.deepcopy(original); enhanced['blocks'][0]['confidence'] = .99
    fake_ocr(monkeypatch, enhanced, baseline=original)
    recognized = ocr_native.recognize(page(pale=True))
    audit = recognized['quality']['preprocessing']
    assert audit['truncated'] and not audit['text_disagreement']
    assert audit['selected'] == 'original' and recognized['blocks'][0]['confidence'] == .7


@pytest.mark.parametrize('suffix', ['‰', '℃', '°', '≤', '±', '￥'])
def test_new_units_or_formula_symbols_are_critical_changes(suffix):
    audit = comparison(result('7'), result('7' + suffix), 0, ocr_native.inverse_box)
    assert audit['numeric_symbol_disagreement'] and audit['visual_review_required']


def test_compile_lock_wait_is_part_of_initialization_timeout(monkeypatch, tmp_path):
    class BusyLock:
        def acquire(self, *, timeout):
            assert 0 <= timeout <= .01
            return False
        def release(self):
            pytest.fail('an unacquired lock must not be released')
    monkeypatch.setattr(ocr_native.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(ocr_native.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(ocr_native, '_COMPILE_LOCK', BusyLock())
    monkeypatch.setattr(ocr_native.subprocess, 'run', lambda *a, **kw: pytest.fail('must not compile after timeout'))
    with pytest.raises(ocr_native.OCRError, match='初始化超时'):
        ocr_native._binary(timeout=.01)


@pytest.mark.parametrize('before,after,critical', [('1 2', '12', True), ('now here', 'nowhere', False)])
def test_whitespace_must_not_merge_numeric_or_word_tokens(monkeypatch, before, after, critical):
    fake_ocr(monkeypatch, result(after, .99), baseline=result(before))
    recognized = ocr_native.recognize(page(pale=True))
    audit = recognized['quality']['preprocessing']
    assert recognized['text'] == before and audit['selected'] == 'original'
    assert audit['visual_review_required'] and audit['numeric_symbol_disagreement'] is critical
