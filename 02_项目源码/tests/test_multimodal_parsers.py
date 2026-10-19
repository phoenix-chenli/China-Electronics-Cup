"""Real native PDF and Apple Vision round trips; synthetic fixtures are test-only."""
import hashlib
import io
import platform
import sqlite3
import subprocess
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from backend import parsers
from backend.ocr_native import OCRError, detect_grid, inverse_box, quality_metrics, recognize


def _font(size=32):
    return ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', size)


def native_pdf(path, *, merged=False, rotation=0):
    doc = pymupdf.open()
    p = doc.new_page(width=600, height=500)
    p.insert_text((40, 40), 'Quarterly sales 2026', fontsize=15)
    p.insert_text((40, 70), 'Revenue = Price x Quantity', fontsize=12)
    xs, ys = [40, 210, 380, 550], [120, 180, 240, 300]
    for y in ys:
        p.draw_line((xs[0], y), (xs[-1], y), width=1)
    for i, x in enumerate(xs):
        p.draw_line((x, ys[1] if merged and i == 1 else ys[0]), (x, ys[-1]), width=1)
    rows = [['Product', 'Units', 'Revenue'], ['Alpha', '2', '1373.00'], ['Beta', '4', '20.00']]
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            if merged and r == 0 and c == 1:
                continue
            p.insert_text((xs[c] + 10, ys[r] + 35), value, fontsize=13)
    p.set_rotation(rotation)
    doc.set_toc([[1, 'Quarterly sales', 1]])
    doc.save(path)
    doc.close()


def image_fixture():
    image = Image.new('RGB', (1100, 700), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((70, 40), 'Quarterly sales 2026', font=_font(38), fill='black')
    xs, ys = [70, 370, 670, 1030], [150, 270, 390, 510]
    for x in xs:
        draw.line((x, ys[0], x, ys[-1]), fill='black', width=3)
    for y in ys:
        draw.line((xs[0], y, xs[-1], y), fill='black', width=3)
    for r, row in enumerate([['Product', 'Units', 'Revenue'], ['Alpha', '2', '1373.00'], ['Beta', '4', '20.00']]):
        for c, text in enumerate(row):
            draw.text((xs[c] + 20, ys[r] + 40), text, font=_font(), fill='black')
    return image


def parse(path, tmp_path):
    return parsers.parse(path, path.suffix[1:], tmp_path / (path.stem + '.db'), lambda *a: None)


def test_native_pdf_actual_grid_bookmarks_formulas_and_original_hash(tmp_path):
    path = tmp_path / 'native.pdf'
    native_pdf(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = parse(path, tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert result['metadata']['ocr_performed'] is True
    assert result['pages'][0]['adaptive_processing']['requested_route'] == 'native_with_ocr_comparison'
    assert result['pages'][0]['ocr_comparison']['selected'] == 'native'
    assert result['pages'][0]['extraction'] == 'native'
    assert result['metadata']['outline'][0]['title'] == 'Quarterly sales'
    assert result['metadata']['complexity']['formula_candidates'] == 1
    region = result['metadata']['table_regions'][0]
    assert (region['row_count'], region['column_count'], region['data_row_offset']) == (3, 3, 0)
    assert region['cells'][5]['text'] == '1373.00'
    assert region['cells'][5]['row'] == 2
    assert result['tables'][0]['columns'][0]['name'] == '列1'
    with sqlite3.connect(result['data_path']) as db:
        rows = db.execute(f'SELECT * FROM "{region["table_name"]}"').fetchall()
    assert rows[0] == ('Product', 'Units', 'Revenue')
    assert rows[1] == ('Alpha', '2', '1373.00')
    for block in result['pages'][0]['blocks']:
        assert result['pages'][0]['text'][block['start']:block['end']] == block['text']
        assert all(0 <= v <= 1 for v in block['bbox'])


def test_native_pdf_merged_cells_preserve_coverage_not_duplicate_value(tmp_path):
    path = tmp_path / 'merged.pdf'
    native_pdf(path, merged=True)
    result = parse(path, tmp_path)
    region = result['metadata']['table_regions'][0]
    assert region['has_merged_cells']
    assert region['cells'][0]['colspan'] == 2
    assert region['cells'][1]['text'] is None
    assert region['cells'][1]['covered_by'] == [1, 1]
    assert region['requires_review']


def test_pdf_rotation_coordinates_match_original_preview(tmp_path):
    path = tmp_path / 'rotated.pdf'
    native_pdf(path, rotation=90)
    result = parse(path, tmp_path)
    block = next(b for b in result['pages'][0]['blocks'] if 'Quarterly' in b['text'])
    assert block['bbox'][0] > .85  # Original top heading is right edge of rotated page.
    assert block['bbox'][1] < .2
    assert result['pages'][0]['quality']['pdf_page_rotation'] == 90
    table = result['metadata']['table_regions'][0]
    assert table['cells'][0]['text'] == 'Product'
    assert table['bbox'] == pytest.approx([.4, 40 / 600, .76, 550 / 600], abs=.001)


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Apple Vision is macOS only')
def test_real_vision_scanned_table_and_rotation_inverse(tmp_path):
    image = image_fixture()
    for rotation in (0, 90):
        path = tmp_path / f'scan-{rotation}.png'
        image.rotate(rotation, expand=True).save(path)
        result = parse(path, tmp_path)
        page = result['pages'][0]
        assert page['extraction'] == 'ocr'
        assert '1373.00' in page['text']
        assert page['quality']['rotation_correction'] == (360 - rotation) % 360
        assert result['metadata']['table_regions'], page['text']
        region = result['metadata']['table_regions'][0]
        assert region['extraction'] == 'ocr_grid' and region['requires_review']
        assert region['cells'][5]['text'] == '1373.00'
        assert region['cells'][5]['confidence'] is not None
        for block in page['blocks']:
            assert page['text'][block['start']:block['end']] == block['text']
        number = next(b for b in page['blocks'] if b['text'] == '1373.00')
        expected = inverse_box([.63, .43, .85, .55], rotation)
        assert abs(number['bbox'][0] - expected[0]) < .12


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Apple Vision is macOS only')
def test_real_vision_scanned_pdf(tmp_path):
    image = image_fixture()
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    path = tmp_path / 'scan.pdf'
    doc = pymupdf.open()
    p = doc.new_page(width=550, height=350)
    p.insert_image(p.rect, stream=stream.getvalue())
    doc.save(path)
    doc.close()
    result = parse(path, tmp_path)
    assert result['metadata']['ocr_performed'] is True
    assert '1373.00' in result['pages'][0]['text']
    assert result['metadata']['table_regions'][0]['page'] == 1


def test_blur_and_low_contrast_diagnostics_never_claim_accuracy():
    image = image_fixture()
    clear = quality_metrics(image)
    blurred = quality_metrics(image.filter(ImageFilter.GaussianBlur(5)))
    assert blurred['sharpness'] < clear['sharpness'] / 10
    assert any('模糊' in w for w in blurred['warnings'])
    pale = Image.eval(image.convert('L'), lambda v: 240 + int(v / 255 * 15))
    quality = quality_metrics(pale)
    assert any('对比度' in w for w in quality['warnings'])
    assert not quality['blank']


def test_blank_image_and_pdf_have_visible_warning_without_running_vision(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.ocr_native._binary', lambda **kw: pytest.fail('blank image must not invoke OCR'))
    image = tmp_path / 'blank.png'
    Image.new('RGB', (500, 500), 'white').save(image)
    result = parse(image, tmp_path)
    assert result['pages'][0]['extraction'] == 'unreadable'
    assert result['pages'][0]['quality']['blank']
    assert not result['metadata']['ocr_performed']
    assert '原稿是否缺页' in result['metadata']['quality_summary']['missing_page_note']
    path = tmp_path / 'blank.pdf'
    doc = pymupdf.open(); doc.new_page(); doc.save(path); doc.close()
    result = parse(path, tmp_path)
    assert result['pages'][0]['quality']['blank'] and result['warnings']


def test_no_table_from_aligned_text_or_ambiguous_spanning_ocr():
    im = Image.new('RGB', (1000, 800), 'white')
    assert detect_grid(im, []) == []
    grid = image_fixture()
    block = {'text': 'spans two cells', 'bbox': [.1, .24, .6, .29], 'confidence': .9}
    assert detect_grid(grid, [block]) == []  # Fewer than three independent occupied cells.


def test_ocr_timeout_is_retryable_and_bounded(monkeypatch):
    monkeypatch.setattr('backend.ocr_native._binary', lambda **kw: Path('/not-executed'))
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] <= .1
        raise subprocess.TimeoutExpired('ocr', kwargs['timeout'])
    monkeypatch.setattr('backend.ocr_native.subprocess.run', timeout)
    with pytest.raises(OCRError, match='超时'):
        recognize(image_fixture(), timeout=.1)


def test_pdf_processing_deadline_and_ocr_page_bound_visible(tmp_path, monkeypatch):
    path = tmp_path / 'bounded.pdf'
    native_pdf(path)
    monkeypatch.setattr(parsers, 'MAX_DOCUMENT_SECONDS', 0)
    result = parse(path, tmp_path)
    assert result['metadata']['quality_summary']['processing_truncated']
    assert result['pages'] == [] and result['page_count'] == 1
    assert any('尚未处理' in n for n in result['warnings'])


@pytest.mark.parametrize('suffix,content', [('pdf', b'bad pdf'), ('png', b'bad png')])
def test_corrupt_files_do_not_return_fabricated_text(tmp_path, suffix, content):
    path = tmp_path / ('bad.' + suffix)
    path.write_bytes(content)
    with pytest.raises((parsers.ParseError, OSError)):
        parse(path, tmp_path)


def test_colored_grid_uses_actual_edges_not_text_spacing():
    image = Image.new('RGB', (900, 600), 'white')
    draw = ImageDraw.Draw(image)
    blocks = []
    for r in range(3):
        for c in range(3):
            draw.rectangle((50 + c * 250, 70 + r * 120, 300 + c * 250, 190 + r * 120), fill=('#d0c5b8' if r % 2 else '#eee6df'), outline='white', width=2)
            blocks.append(dict(text=f'{r}-{c}', bbox=[(70 + c * 250) / 900, (90 + r * 120) / 600, (130 + c * 250) / 900, (115 + r * 120) / 600], confidence=.9))
    regions = detect_grid(image, blocks)
    assert regions and regions[0]['grid_detection'] == 'intensity_edges'
    assert regions[0]['row_count'] == 3 and regions[0]['column_count'] == 3


def test_cell_ocr_disagreement_is_preserved_not_silently_certified(monkeypatch):
    import json
    from backend.ocr_native import _recognize_cells
    import time
    table = dict(cells=[dict(row=1, col=1, text='99.94', bbox=[0, 0, 1, 1])], rows=[['99.94']], _requires_cell_ocr=False)
    response = [{'blocks': [{'text': '9.94', 'bbox': [.1, .1, .8, .5], 'confidence': 1}]}]
    monkeypatch.setattr('backend.ocr_native.subprocess.run', lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(response).encode()))
    tables, warnings = _recognize_cells(Image.new('RGB', (200, 100), 'white'), [table], '/unused', time.monotonic() + 5, 0)
    cell = tables[0]['cells'][0]
    assert cell['text'] == '9.94'
    assert cell['ocr_candidates'] == ['99.94', '9.94']
    assert cell['ocr_disagreement'] is True
    assert '计算前需' in warnings[0]


def test_docx_heading_outline_and_math_tokens_are_not_verified_equations(tmp_path):
    import zipfile
    path = tmp_path / 'outline.docx'
    body = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>1. Overview</w:t></w:r></w:p>
    <w:p><w:r><w:t>Source paragraph</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>1.1 Formula</w:t></w:r></w:p>
    <w:p><m:oMath><m:f><m:num><m:r><m:t>x</m:t></m:r></m:num><m:den><m:r><m:t>y</m:t></m:r></m:den></m:f></m:oMath></w:p>
    </w:body></w:document>"""
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', body)
    result = parse(path, tmp_path)
    assert [e['level'] for e in result['metadata']['outline']] == [1, 2]
    assert result['metadata']['page_unit'] == 'logical_section'
    formulas = [b for page in result['pages'] for b in page['blocks'] if b['kind'] == 'formula_candidate']
    assert formulas[0]['bbox'] is None
    assert formulas[0]['text'] == '[公式结构待核对] x y'
    assert any('结构仍需对照原件' in w for w in result['warnings'])


def test_transparent_image_preserves_visible_black_text():
    from backend.ocr_native import normalized_image
    image = Image.new('RGBA', (200, 120), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((20, 20), '2026', fill='black', font=_font())
    normalized = normalized_image(image)
    assert normalized.getpixel((0, 0)) == (255, 255, 255)
    assert not quality_metrics(normalized)['blank']
