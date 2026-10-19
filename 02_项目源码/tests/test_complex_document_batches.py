"""Offline import state and OOXML fidelity; no model accuracy claims."""
import copy
import sqlite3
import zipfile
from collections import Counter

import pytest
from PIL import Image

from backend import parsers


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def local_pdf(monkeypatch, tmp_path, count=5, *, scanned=False, fail=()):
    """Synthetic page decoder to isolate resume logic from OCR quality."""
    original = tmp_path / 'fixture.pdf'
    original.write_bytes(b'controlled parser fixture, not a real PDF')
    calls = Counter()
    def worker(path, index=None, image_path=None, timeout=None, **kwargs):
        if index is None:
            return {'page_count': count, 'outline': []}
        calls[index + 1] += 1
        if index + 1 in fail:
            raise parsers.ParseError('permanent page failure')
        Image.new('RGB', (50, 70), 'white').save(image_path)
        text = f'Original page {index + 1} value 12.50'
        return {'blocks': [] if scanned else [{'text': text, 'bbox': [.1, .2, .8, .3], 'confidence': None, 'kind': 'text'}],
                'tables': [], 'has_image': scanned, 'page_rotation': 0, 'warnings': [], 'printed_page_label': str(index + 1)}
    monkeypatch.setattr(parsers, '_pdf_worker', worker)
    monkeypatch.setattr('backend.ocr_native.quality_metrics', lambda image: {'blank': not scanned, 'warnings': []})
    def recognize(*args, **kwargs):
        text = '本地模拟识别 12.50'
        return {'text': text, 'blocks': [{'text': text, 'bbox': [0, 0, 1, 1], 'kind': 'text', 'confidence': .9}],
                'extraction': 'ocr', 'quality': {'blank': False, 'warnings': []}, 'tables': []}
    monkeypatch.setattr('backend.ocr_adapter.recognize', recognize)
    return original, calls


def test_205_pages_auto_continue_ocr_budget_without_reextracting_completed_pages(tmp_path, monkeypatch):
    path, calls = local_pdf(monkeypatch, tmp_path, 205, scanned=True)
    result = parsers.parse(path, 'pdf', tmp_path / 'result.sqlite', lambda *a: None, pdf_batch_limit=16)
    state = result['metadata']['import_batches']
    assert state['status'] == 'complete' and state['batch_stop_reason'] == 'complete'
    assert state['batches_run'] == 6 and state['pending_pages'] == []
    assert [p['number'] for p in result['pages']] == list(range(1, 206))
    assert set(calls.values()) == {1}
    assert sum(b['ocr_attempts'] for b in state['automatic_batches']) == 205
    assert result['metadata']['ocr_performed']
    assert not result['metadata']['quality_summary']['processing_truncated']


def test_failed_early_page_stops_after_two_attempts_keeps_later_pages_and_error(tmp_path, monkeypatch):
    path, calls = local_pdf(monkeypatch, tmp_path, fail={1})
    result = parsers.parse(path, 'pdf', tmp_path / 'result.sqlite', lambda *a: None, pdf_batch_limit=16)
    state = result['metadata']['import_batches']
    assert state['status'] == 'partial' and state['pending_pages'] == [1]
    assert state['exhausted_pages'] == [1] and state['batches_run'] == 2
    assert state['batch_stop_reason'] == 'retry_exhausted'
    assert calls == Counter({1: 2, 2: 1, 3: 1, 4: 1, 5: 1})
    assert result['pages'][0]['quality']['warnings'] == ['permanent page failure']
    assert result['metadata']['quality_summary']['processing_truncated']
    # A later zero-budget retry still returns all persisted successful pages
    # and the failed page diagnostic, not an empty or falsely complete import.
    monkeypatch.setattr(parsers, 'MAX_DOCUMENT_SECONDS', 0)
    resumed = parsers.parse(path, 'pdf', tmp_path / 'again.sqlite', lambda *a: None)
    assert [p['number'] for p in resumed['pages']] == [1, 2, 3, 4, 5]
    assert resumed['metadata']['import_batches']['pending_pages'] == [1]
    assert calls[1] == 2


def test_cross_batch_tables_are_materialized_once_with_source_page_identity(tmp_path, monkeypatch):
    path, calls = local_pdf(monkeypatch, tmp_path, 3, scanned=True)
    monkeypatch.setattr(parsers, 'MAX_OCR_PAGES', 1)
    from backend import ocr_adapter
    recognize = ocr_adapter.recognize
    def with_table(*args, **kwargs):
        value = recognize(*args, **kwargs)
        value['tables'] = [{'bbox': [0, 0, 1, 1], 'rows': [['对象', '值'], ['A', '12.50']],
                            'cells': [], 'row_count': 2, 'column_count': 2, 'has_merged_cells': False,
                            'extraction': 'ocr_grid', 'requires_review': True}]
        return value
    monkeypatch.setattr(ocr_adapter, 'recognize', with_table)
    output = tmp_path / 'tables.sqlite'
    result = parsers.parse(path, 'pdf', output, lambda *a: None, pdf_batch_limit=16)
    assert result['metadata']['import_batches']['status'] == 'complete'
    assert len(result['tables']) == 3 and len(result['metadata']['table_regions']) == 3
    with sqlite3.connect(output) as db:
        for page_number in (1, 2, 3):
            assert db.execute(f'SELECT * FROM "第{page_number}页_表1"').fetchall() == [('对象', '值'), ('A', '12.50')]
    assert set(calls.values()) == {1}


def test_zero_budget_and_batch_limit_are_visible_without_busy_loop(tmp_path, monkeypatch):
    path, calls = local_pdf(monkeypatch, tmp_path, scanned=True)
    monkeypatch.setattr(parsers, 'MAX_DOCUMENT_SECONDS', 0)
    result = parsers.parse(path, 'pdf', tmp_path / 'zero.sqlite', lambda *a: None, pdf_batch_limit=16)
    assert result['metadata']['import_batches']['batches_run'] == 1
    assert result['metadata']['import_batches']['batch_stop_reason'] == 'no_progress'
    assert not calls
    monkeypatch.setattr(parsers, 'MAX_DOCUMENT_SECONDS', 180)
    monkeypatch.setattr(parsers, 'MAX_OCR_PAGES', 1)
    result = parsers.parse(path, 'pdf', tmp_path / 'limited.sqlite', lambda *a: None, pdf_batch_limit=2)
    state = result['metadata']['import_batches']
    assert state['pending_pages'] == [3, 4, 5] and state['batch_stop_reason'] == 'batch_limit'
    assert state['status'] == 'partial'


def test_time_budget_batches_progress_with_cached_later_pages(tmp_path, monkeypatch):
    path, calls = local_pdf(monkeypatch, tmp_path, 7)
    worker = parsers._pdf_worker
    clock = [0.0]
    monkeypatch.setattr(parsers.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(parsers, 'MAX_DOCUMENT_SECONDS', 2)
    def timed_worker(path, index=None, *args, **kwargs):
        value = worker(path, index, *args, **kwargs)
        if index is not None:
            clock[0] += 1
        return value
    monkeypatch.setattr(parsers, '_pdf_worker', timed_worker)
    result = parsers.parse(path, 'pdf', tmp_path / 'timed.sqlite', lambda *a: None, pdf_batch_limit=16)
    state = result['metadata']['import_batches']
    assert state['status'] == 'complete' and state['batches_run'] == 4
    assert [len(b['new_completed_pages']) for b in state['automatic_batches']] == [2, 2, 2, 1]
    assert set(calls.values()) == {1}


def test_retry_failure_retains_previous_exact_text_and_pending_status(tmp_path, monkeypatch):
    from backend.ocr_native import OCRError
    path, calls = local_pdf(monkeypatch, tmp_path, 1)
    monkeypatch.setattr('backend.ocr_native.quality_metrics', lambda image: {'blank': False, 'warnings': [], 'contrast': 1})
    def unavailable(*args, **kwargs):
        raise OCRError('OCR unavailable')
    monkeypatch.setattr('backend.ocr_adapter.recognize', unavailable)
    original = parsers.parse(path, 'pdf', tmp_path / 'first.sqlite', lambda *a: None)
    exact = copy.deepcopy(original['pages'][0])
    worker = parsers._pdf_worker
    def failed_decode(path, index=None, *args, **kwargs):
        if index is not None:
            raise parsers.ParseError('decoder timed out')
        return worker(path, index, *args, **kwargs)
    monkeypatch.setattr(parsers, '_pdf_worker', failed_decode)
    retry = parsers.parse(path, 'pdf', tmp_path / 'retry.sqlite', lambda *a: None)
    assert retry['pages'][0]['text'] == exact['text']
    assert retry['pages'][0]['blocks'] == exact['blocks']
    assert retry['pages'][0]['retry_status'] == 'failed_previous_observation_retained'
    assert retry['metadata']['import_batches']['pending_pages'] == [1]
    assert retry['metadata']['text_characters'] == len(exact['text'])


def test_text_policy_stops_and_never_publishes_a_partial_page(tmp_path, monkeypatch):
    path, calls = local_pdf(monkeypatch, tmp_path)
    monkeypatch.setattr(parsers, 'MAX_TEXT', 10)
    result = parsers.parse(path, 'pdf', tmp_path / 'limited.sqlite', lambda *a: None, pdf_batch_limit=16)
    state = result['metadata']['import_batches']
    assert state['batches_run'] == 1 and state['batch_stop_reason'] == 'text_budget'
    assert state['pending_pages'] == [1, 2, 3, 4, 5]
    assert result['pages'] == [] and calls == Counter({1: 1})


def p(text, level=None):
    style = f'<w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>' if level else ''
    return f'<w:p>{style}<w:r><w:t>{text}</w:t></w:r></w:p>'


def test_docx_five_levels_marginal_parts_and_business_tags_keep_exact_positions(tmp_path):
    path = tmp_path / '工程记录.docx'
    inline = '<w:p><w:r><w:t>检查记录：</w:t></w:r><w:sdt><w:sdtPr><w:tag w:val="inspection.status"/><w:alias w:val="验收状态"/></w:sdtPr><w:sdtContent><w:r><w:t>待复核</w:t></w:r></w:sdtContent></w:sdt><w:r><w:t>，不得直接算合格。</w:t></w:r></w:p>'
    container = '<w:sdt><w:sdtPr><w:tag w:val="工序编号"/></w:sdtPr><w:sdtContent>' + p('PROC-017') + '</w:sdtContent></w:sdt>'
    body = ''.join(p(f'{i}级标题', i) for i in range(1, 6)) + inline + container + '<w:sectPr><w:headerReference w:type="default" r:id="rH"/><w:footerReference w:type="first" r:id="rF"/></w:sectPr>'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship Id="rH" Target="header1.xml"/><Relationship Id="rF" Target="footer1.xml"/></Relationships>')
        archive.writestr('word/header1.xml', f'<w:hdr xmlns:w="{W}">{p("工程代号 QW-27")}</w:hdr>')
        archive.writestr('word/footer1.xml', f'<w:ftr xmlns:w="{W}">{p("受控文件，请核对版本")}</w:ftr>')
    before = path.read_bytes()
    result = parsers.parse(path, 'docx', tmp_path / 'document.sqlite', lambda *a: None)
    assert path.read_bytes() == before
    headings = result['metadata']['document_structure']['headings']
    assert [h['level'] for h in headings] == [1, 2, 3, 4, 5]
    assert [h['parent_id'] for h in headings] == [None, 'H1', 'H2', 'H3', 'H4']
    controls = result['metadata']['content_controls']
    assert [c['tag'] for c in controls] == ['inspection.status', '工序编号']
    for control in controls:
        assert control['span_verified'] and not control['semantic_verified']
        position = control['source_position']
        source = result['pages'][position['page'] - 1]['text']
        assert source[position['start']:position['end']] == control['text']
    assert controls[0]['text'] == '待复核' and controls[0]['alias'] == '验收状态'
    parts = result['metadata']['docx_marginal_parts']['parts']
    assert {part['kind'] for part in parts} == {'header', 'footer'}
    for part in parts:
        assert part['page_unit'] == 'archive_part' and part['section_references'][0]['section_index'] == 1
        for block in part['blocks']:
            assert part['text'][block['start']:block['end']] == block['text']
            assert 'page' not in block['source_position']
    assert '工程代号 QW-27' not in ''.join(section['text'] for section in result['pages'])


def test_unknown_docx_relationship_is_reported_and_not_fetched(tmp_path):
    path = tmp_path / '外部关系.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{p("正文")}<w:sectPr><w:headerReference w:type="default" r:id="remote"/></w:sectPr></w:body></w:document>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship Id="remote" Target="https://example.invalid/header.xml" TargetMode="External"/></Relationships>')
    result = parsers.parse(path, 'docx', tmp_path / 'result.sqlite', lambda *a: None)
    metadata = result['metadata']['docx_marginal_parts']
    assert metadata['parts'] == [] and len(metadata['unresolved_references']) == 1
