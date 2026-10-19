"""Orientation regressions use authored observations, never benchmark answers.

The fake executable reads the physical PNGs (or Tesseract image list) so the
tests exercise rotation, normalized source geometry and the public adapter.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from PIL import Image, ImageDraw

from backend import ocr_adapter as adapter
from backend import ocr_orientation as orientation
from backend import ocr_tesseract
from backend.ocr_native import inverse_box


HEADER = 'level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
READABLE = [('年度经营情况统计', [.10, .15, .65, .20], .95),
            ('营业收入 -12.50%', [.10, .60, .80, .65], .94)]
WEAK = [('|| / : ~', [.10, .15, .40, .20], .08),
        ('j i | ~', [.10, .60, .40, .65], .05)]


def words(observations=READABLE):
    return [dict(text=text, bbox=box[:], confidence=confidence,
                 line_key=(1, 1, 1, number))
            for number, (text, box, confidence) in enumerate(observations, 1)]


def candidate(rotation, observations=READABLE):
    current = words(observations)
    return dict(rotation=rotation, words=current, records=len(current),
                quality=orientation.reading_quality(current))


def source_image():
    image = Image.new('RGB', (400, 240), 'white')
    draw = ImageDraw.Draw(image)
    for box, color in [((0, 0, 12, 12), 'red'), ((387, 0, 399, 12), 'green'),
                       ((387, 227, 399, 239), 'blue'), ((0, 227, 12, 239), 'black')]:
        draw.rectangle(box, fill=color)
    draw.text((90, 95), 'orientation sample', fill='black')
    return image


def configure(monkeypatch, recognizer):
    """Support both bounded sequential calls and Tesseract image-list calls."""
    monkeypatch.setattr(adapter.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(adapter, 'capabilities', lambda: {
        'selected': 'tesseract', 'tesseract': {'languages': ['chi_sim', 'eng']},
        'chinese_available': True})
    monkeypatch.setattr(adapter.shutil, 'which', lambda _: 'tesseract')
    calls = []
    corner_rotation = {(255, 0, 0): 0, (0, 128, 0): 90,
                       (0, 0, 255): 180, (0, 0, 0): 270}

    def run(arguments, **kwargs):
        input_path = Path(arguments[1])
        paths = [input_path] if input_path.suffix.lower() == '.png' else [
            Path(line) for line in input_path.read_text(encoding='utf-8').splitlines() if line]
        raw = HEADER
        rotations = []
        for number, path in enumerate(paths, 1):
            with Image.open(path) as current:
                rotation = corner_rotation.get(current.convert('RGB').getpixel((4, 4)))
                rotations.append(rotation)
                raw += f'1\t{number}\t0\t0\t0\t0\t0\t{current.width}\t{current.height}\t-1\t\n'
                observations = recognizer(rotation, arguments, kwargs)
                for line, (text, box, confidence) in enumerate(observations, 1):
                    left, top, right, bottom = [round(value * extent) for value, extent in
                                               zip(box, (current.width, current.height) * 2)]
                    raw += (f'5\t{number}\t1\t1\t{line}\t{left}\t{top}\t{right-left}\t'
                            f'{bottom-top}\t{confidence*100}\t{text}\n')
        calls.append(dict(rotations=rotations, timeout=kwargs['timeout']))
        return SimpleNamespace(returncode=0, stdout=raw.encode('utf-8'), stderr=b'')

    monkeypatch.setattr(adapter.subprocess, 'run', run)
    return calls


def test_reading_quality_rejects_empty_and_punctuation_even_at_high_confidence():
    empty = orientation.reading_quality([])
    punctuation = orientation.reading_quality(words([('| / : ~ ! ?' * 20, [.1, .1, .8, .2], .99)]))
    readable = orientation.reading_quality(words())
    assert not empty['usable'] and not empty['strong']
    assert not punctuation['usable']
    assert readable['usable'] and readable['score'] > punctuation['score']
    assert readable['word_count'] == 2 and readable['text_characters'] > 0
    assert 0 <= readable['reliable_fraction'] <= 1
    assert 0 <= readable['mean_confidence'] <= 1


def test_reading_quality_does_not_make_confidence_an_accuracy_claim():
    low = words([(text, box, .02) for text, box, _ in READABLE])
    assert not orientation.reading_quality(low)['usable']
    assert not orientation.reading_quality(low)['strong']


def test_orientation_tie_keeps_original_and_requires_review():
    original, turned = candidate(0), candidate(90)
    selected, ambiguous = orientation.choose_orientation([turned, original])
    assert selected['rotation'] == 0 and ambiguous


def test_coherent_lower_confidence_text_beats_many_weak_tokens():
    garbage = candidate(0, WEAK * 100)
    readable = candidate(270)
    selected, _ = orientation.choose_orientation([garbage, readable])
    assert selected['rotation'] == 270 and selected['quality']['usable']


def test_many_sideways_high_confidence_long_words_do_not_establish_readability():
    tokens = ['revenue', 'expenses', 'operating', 'liabilities', 'customer', 'statistics']
    sideways_words = words([(token, [.1, .1, .12, .4], .98) for token in tokens])
    upright = candidate(270)
    sideways = dict(rotation=0, words=sideways_words, records=len(sideways_words),
                    quality=orientation.reading_quality(sideways_words, (400, 240)))
    diagnostics = sideways['quality']
    assert diagnostics['directional_words'] == len(tokens)
    assert diagnostics['horizontal_fraction'] == 0
    assert not diagnostics['usable'] and not diagnostics['strong']
    selected, _ = orientation.choose_orientation([sideways, upright])
    assert selected['rotation'] == 270


@pytest.mark.parametrize('tokens', [('中', '文'), ('2024', '-12.50%', '0', '88.8%'), ('Revenue',)])
def test_single_characters_numbers_and_sparse_words_do_not_force_a_direction(tokens):
    observations = words([(token, [.1, .1, .12, .4], .97) for token in tokens])
    without_geometry = orientation.reading_quality(observations)
    with_geometry = orientation.reading_quality(observations, (400, 240))
    assert with_geometry['horizontal_fraction'] is None
    assert with_geometry['usable'] == without_geometry['usable']
    assert with_geometry['strong'] == without_geometry['strong']
    assert with_geometry['score'] == without_geometry['score']


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_remapping_preserves_corrected_reading_order_and_all_source_boxes(rotation):
    blocks = [dict(text='first', bbox=[.1, .15, .65, .2], start=0, end=5),
              dict(text='second', bbox=[.1, .6, .8, .65], start=6, end=12)]
    tables = [dict(bbox=[.05, .1, .9, .8], rows=[['-12.50%']], cells=[
        dict(row=1, col=1, bbox=[.1, .2, .4, .4], text='-12.50%', requires_review=True)])]
    expected_blocks = [inverse_box(block['bbox'], rotation) for block in blocks]
    expected_table = inverse_box(tables[0]['bbox'], rotation)
    expected_cell = inverse_box(tables[0]['cells'][0]['bbox'], rotation)
    orientation.remap_result(blocks, tables, rotation)
    assert [block['text'] for block in blocks] == ['first', 'second']
    assert [block['bbox'] for block in blocks] == expected_blocks
    assert [(block['start'], block['end']) for block in blocks] == [(0, 5), (6, 12)]
    assert tables[0]['bbox'] == expected_table
    assert tables[0]['cells'][0]['bbox'] == expected_cell
    assert tables[0]['rows'] == [['-12.50%']]
    assert tables[0]['cells'][0]['requires_review']


@pytest.mark.parametrize('rotation', [90, 180, 270])
def test_windows_recovers_cardinal_direction_and_maps_boxes_to_original(monkeypatch, rotation):
    calls = configure(monkeypatch, lambda turn, *_: READABLE if turn == rotation else WEAK)
    image = source_image()
    before = image.tobytes()
    result = adapter.recognize(image)
    assert image.tobytes() == before and image.size == (400, 240)
    assert result['extraction'] == 'ocr' and result['quality']['rotation_correction'] == rotation
    assert result['text'] == '\n'.join(observation[0] for observation in READABLE)
    assert [block['bbox'] for block in result['blocks']] == [
        inverse_box(observation[1], rotation) for observation in READABLE]
    for block in result['blocks']:
        assert result['text'][block['start']:block['end']] == block['text']
    audit = result['quality']['orientation']
    assert audit['selected_rotation'] == rotation
    assert any(attempt['rotation'] == rotation for attempt in audit['attempts'])
    assert sum(len(call['rotations']) for call in calls) <= 4


def test_strong_original_does_not_change_orientation_or_numeric_text(monkeypatch):
    observations = [('完整页面文字及经营统计指标说明，报告记录本年度收入、成本、利润与人员变化，所有数值均须对照原始凭证核验。', [.1, .1, .9, .2], .99),
                    ('利润同比 -12.50%，不作数值格式修补', [.1, .4, .9, .5], .99)]
    calls = configure(monkeypatch, lambda *_: observations)
    result = adapter.recognize(source_image())
    assert result['quality']['rotation_correction'] == 0
    assert '-12.50%' in result['text']
    assert sum(len(call['rotations']) for call in calls) == 1


def test_all_weak_orientations_are_not_published_as_indexable_text(monkeypatch):
    calls = configure(monkeypatch, lambda *_: WEAK)
    result = adapter.recognize(source_image())
    assert result['extraction'] == 'unreadable'
    assert result['text'] == '' and result['blocks'] == [] and result['tables'] == []
    assert result['quality']['visual_review_required']
    assert result['quality']['orientation']['attempts']
    assert sum(len(call['rotations']) for call in calls) <= 4


def test_table_review_uses_corrected_image_before_source_remapping(monkeypatch):
    rotation = 90
    configure(monkeypatch, lambda turn, *_: READABLE if turn == rotation else WEAK)
    table = dict(bbox=[.05, .1, .9, .8], rows=[['-12.50%']], row_count=1, column_count=1,
                 cells=[dict(row=1, col=1, bbox=[.1, .2, .4, .4], text='-12.50%', requires_review=True)],
                 requires_review=True)
    observed = []
    def grid(image, current_words):
        assert image.size == (240, 400)
        assert current_words[0]['bbox'] == READABLE[0][1]
        return [deepcopy(table)]
    def crops(image, current_tables):
        observed.append((image.size, current_tables[0]['cells'][0]['bbox'][:]))
        return [], False
    monkeypatch.setattr(ocr_tesseract, 'table_candidates', grid)
    monkeypatch.setattr(ocr_tesseract, 'cell_crops', crops)
    result = adapter.recognize(source_image())
    assert observed == [((240, 400), [.1, .2, .4, .4])]
    assert result['tables'][0]['bbox'] == inverse_box(table['bbox'], rotation)
    assert result['tables'][0]['cells'][0]['bbox'] == inverse_box(table['cells'][0]['bbox'], rotation)
    assert result['tables'][0]['rows'] == [['-12.50%']]


@pytest.mark.parametrize('timeout', [9, 35])
def test_orientation_attempts_share_original_deadline(monkeypatch, timeout):
    clock = [100.0]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    def recognize(turn, arguments, kwargs):
        assert kwargs['timeout'] <= timeout - (clock[0] - 100)
        clock[0] += min(10, kwargs['timeout'])
        return WEAK
    calls = configure(monkeypatch, recognize)
    result = adapter.recognize(source_image(), timeout=timeout)
    assert result['extraction'] == 'unreadable'
    assert all(0 < call['timeout'] <= timeout for call in calls)
    assert clock[0] <= 100 + timeout
    assert len(calls) <= 4


@pytest.mark.parametrize('rotation', [90, 270])
def test_quarter_turn_swaps_anisotropic_dpi_without_changing_source_metadata(monkeypatch, rotation):
    observed = []
    def recognize(turn, arguments, kwargs):
        with Image.open(arguments[1]) as current:
            observed.append((turn, current.info['dpi'], arguments[arguments.index('--dpi')+1]))
        return READABLE if turn == rotation else WEAK
    configure(monkeypatch, recognize)
    image = source_image()
    image.info['dpi'] = (137, 211)
    result = adapter.recognize(image)
    assert result['quality']['rotation_correction'] == rotation
    assert result['quality']['input_dpi'] == [137, 211]
    assert image.info['dpi'] == (137, 211)
    for turn, dpi, argument in observed:
        expected = (211, 137) if turn in (90, 270) else (137, 211)
        assert dpi == pytest.approx(expected, abs=.1)
        assert argument == str(expected[0])


@pytest.mark.parametrize('failure', ['invalid_tsv', 'timeout'])
def test_optional_direction_failure_allows_other_directions_to_recover(monkeypatch, failure):
    configure(monkeypatch, lambda turn, *_: READABLE if turn == 270 else WEAK)
    original = adapter.subprocess.run
    def run(arguments, **kwargs):
        with Image.open(arguments[1]) as current:
            if current.convert('RGB').getpixel((4, 4)) == (0, 128, 0):
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(arguments, kwargs['timeout'])
                return SimpleNamespace(returncode=0, stdout=(HEADER+'malformed\trow\n').encode(), stderr=b'')
        return original(arguments, **kwargs)
    monkeypatch.setattr(adapter.subprocess, 'run', run)
    result = adapter.recognize(source_image())
    assert result['quality']['rotation_correction'] == 270
    assert result['extraction'] == 'ocr'
    assert result['text'] == '\n'.join(observation[0] for observation in READABLE)
    audit = result['quality']['orientation']
    assert any(attempt['rotation'] == 90 and attempt['status'] in ('failed', 'timeout')
               for attempt in audit['attempts'])


def test_cancel_during_direction_search_propagates_without_partial_output(monkeypatch):
    from backend import reliability
    configure(monkeypatch, lambda *_: WEAK)
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        if len(calls) > 1:
            raise InterruptedError('cancelled during orientation')
        return adapter.subprocess.run(arguments, timeout=kwargs['timeout'])
    monkeypatch.setattr(reliability, 'run_cancellable', run)
    with pytest.raises(InterruptedError):
        adapter.recognize(source_image(), cancelled=lambda: len(calls) > 1)
