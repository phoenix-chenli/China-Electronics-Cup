"""Hierarchy coordinates and quality contracts, not model accuracy tests."""
import copy
import hashlib
import zipfile
from xml.etree import ElementTree as ET

import pymupdf
import pytest

from backend import document_structure as ds, parsers

W = ds.W
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'


def write_docx(path, body, styles=''):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}" xmlns:m="{M}"><w:body>{body}</w:body></w:document>')
        if styles:
            archive.writestr('word/styles.xml', f'<w:styles xmlns:w="{W}">{styles}</w:styles>')


def paragraph(text, style=''):
    return '<w:p>' + (f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else '') + f'<w:r><w:t>{text}</w:t></w:r></w:p>'


def parse(path, tmp_path):
    return parsers.parse(path, path.suffix[1:], tmp_path / (path.stem + '.db'), lambda *a: None)


def test_custom_word_heading_styles_toc_and_formula_original_offsets(tmp_path):
    path = tmp_path / '结构.docx'
    styles = '<w:style w:styleId="chapter"><w:name w:val="章标题"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style><w:style w:styleId="sub"><w:basedOn w:val="chapter"/><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
    formula = '<w:p><m:oMath><m:f><m:num><m:r><m:t>x</m:t></m:r></m:num><m:den><m:r><m:t>y</m:t></m:r></m:den></m:f></m:oMath></w:p>'
    body = paragraph('目录') + paragraph('第一章 费用……1', 'TOC1') + paragraph('第一章 费用', 'chapter') + paragraph('参数 x=12万元，y=3人。') + paragraph('（一）分摊', 'sub') + formula
    write_docx(path, body, styles)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = parse(path, tmp_path)
    structure = result['metadata']['document_structure']
    assert [h['title'] for h in structure['headings']] == ['第一章 费用', '（一）分摊']
    assert structure['headings'][1]['parent_id'] == 'H1'
    assert structure['headings'][1]['level_gap']
    assert structure['toc_entries'][0]['printed_page_label'] == '1'
    assert structure['toc_entries'][0]['target_physical_page'] is None
    assert structure['page_labels'] == []
    record = result['metadata']['formula_structures'][0]
    p = record['source_position']
    assert result['pages'][p['page'] - 1]['text'][p['start']:p['end']] == '[公式结构待核对] x y'
    assert record['tree']['type'] == 'fraction' and not record['verified']
    for page in result['pages']:
        for block in page['blocks']:
            assert page['text'][block['start']:block['end']] == block['text']
    for h in structure['headings']:
        for span in h['body_spans']:
            assert 0 <= span['start'] < span['end'] <= len(result['pages'][span['page'] - 1]['text'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert result['metadata']['document_profiles']['quality']['pages'][0]['status'] == 'not_assessed'


def test_docx_content_control_toc_is_preserved_but_not_body_heading(tmp_path):
    path = tmp_path / 'toc.docx'
    body = '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr><w:sdtContent>' + paragraph('1 总则……3', 'Heading1') + '</w:sdtContent></w:sdt>' + paragraph('1 总则', 'Heading1') + paragraph('原始正文 99.94。')
    write_docx(path, body)
    result = parse(path, tmp_path)
    structure = result['metadata']['document_structure']
    assert '1 总则……3' in result['pages'][0]['text']
    assert len(structure['headings']) == 1
    assert structure['toc_entries'][0]['title'] == '1 总则'
    assert result['metadata']['paragraphs'][0]['source_position']['block_path'] == [0, 1, 0]


def test_explicit_body_outline_level_overrides_inherited_heading_name():
    node = ET.fromstring(f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="Heading1"/><w:outlineLvl w:val="9"/></w:pPr><w:r><w:t>Actual body</w:t></w:r></w:p>')
    assert ds.paragraph_properties(node)['heading_level'] is None


def test_pdf_printed_labels_bookmarks_multiline_title_and_exact_text(tmp_path):
    path = tmp_path / 'labels.pdf'
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page(width=600, height=700)
        page.insert_text((40, 25), 'Recurring report', fontsize=9)
        if i == 0:
            page.insert_text((40, 100), 'Contents', fontsize=20)
            page.insert_text((40, 150), '1 Overview .......... 1', fontsize=12)
        elif i == 1:
            page.insert_text((40, 100), '1 Overview', fontsize=20)
            page.insert_text((40, 128), 'of costs', fontsize=20)
            page.insert_text((40, 170), 'This is the exact unchanged body paragraph with cost 99.94.', fontsize=12)
        else:
            page.insert_text((40, 100), '2 Follow-up', fontsize=20)
            page.insert_text((40, 170), 'This is another exact original body paragraph with cost 88.25.', fontsize=12)
    doc.set_page_labels([{'startpage': 0, 'prefix': '', 'style': 'r', 'firstpagenum': 1}, {'startpage': 1, 'prefix': '', 'style': 'D', 'firstpagenum': 1}])
    doc.set_toc([[1, '1 Overview of costs', 2], [1, '2 Follow-up', 3], [2, 'Nonexistent title', 3]])
    doc.save(path)
    doc.close()
    result = parse(path, tmp_path)
    structure = result['metadata']['document_structure']
    assert structure['page_labels'][1] == {'physical_page': 2, 'printed_page_label': '1', 'basis': 'pdf_page_label'}
    assert structure['toc_entries'][0]['target_physical_page'] == 2
    header = structure['headings'][0]
    assert header['title'] == '1 Overview of costs'
    assert len(structure['headings']) == 2  # No duplicate inferred first line of a multiline bookmark.
    p = header['source_position']
    assert result['pages'][1]['text'][p['start']:p['end']] == '1 Overview\nof costs'
    assert any(i['kind'] == 'bookmark_not_anchored' for i in structure['issues'])
    assert len([s for s in structure['excluded_spans'] if s['role'] == 'repeated_header']) == 3
    assert 'Recurring report' in result['pages'][1]['text']  # Annotation never erases source.
    assert all('Recurring report' not in result['pages'][s['page'] - 1]['text'][s['start']:s['end']] for h in structure['headings'] for s in h['body_spans'])
    assert result['pages'][1]['blocks'][1]['font_size'] == 20


def test_numbered_list_and_mixed_numbered_bold_titles_are_distinguished():
    text = '一、总则\n（一）范围\n1.1 Details\n1 ordinary list item'
    blocks, offset = [], 0
    for index, line in enumerate(text.split('\n')):
        blocks.append({'text': line, 'start': offset, 'end': offset + len(line), 'bold': index < 3, 'font_size': 12})
        offset += len(line) + 1
    result = {'metadata': {'page_unit': 'physical_page'}, 'pages': [{'number': 1, 'text': text, 'blocks': blocks}]}
    structure = ds.build(result, format='pdf')
    assert [h['level'] for h in structure['headings']] == [1, 2, 2]
    assert all(h['structure_status'] == 'candidate_requires_review' for h in structure['headings'])


def test_complexity_and_quality_do_not_contaminate_each_other_or_source():
    base = {'metadata': {'page_unit': 'physical_page', 'formula_structures': [], 'complexity': {}}, 'tables': [],
            'pages': [{'number': 1, 'text': 'x', 'blocks': [], 'extraction': 'native', 'quality': {'contrast': 40, 'sharpness': 100, 'blank': False}}]}
    complex_doc, poor_doc = copy.deepcopy(base), copy.deepcopy(base)
    complex_doc['metadata']['formula_structures'] = [{'id': str(i)} for i in range(10)]
    poor_doc['pages'][0]['quality'].update(contrast=5, sharpness=2)
    original = copy.deepcopy(poor_doc)
    a, b, c = ds.profiles(base), ds.profiles(complex_doc), ds.profiles(poor_doc)
    assert a['quality'] == b['quality'] and a['complexity']['score'] < b['complexity']['score']
    assert a['complexity'] == c['complexity']
    assert c['quality']['pages'][0]['flags'] == ['low_contrast', 'possible_blur']
    assert c['quality']['accuracy_estimate'] is None
    assert c['processing_plan'][0]['suggested_action'] == 'targeted_high_resolution_review'
    assert c['processing_plan'][0]['executed'] is False
    assert poor_doc == original


def test_unknown_pdf_or_scan_formula_remains_unknown_and_source_is_not_corrected():
    text = 'R = l0.O / 2'
    result = {'metadata': {'page_unit': 'physical_page'}, 'tables': [], 'pages': [{'number': 1, 'text': text, 'extraction': 'ocr', 'blocks': [{'kind': 'formula_candidate', 'text': text, 'start': 0, 'end': len(text), 'bbox': [.1, .2, .8, .3]}]}]}
    before = copy.deepcopy(result['pages'])
    ds.enrich(result, format='pdf')
    f = result['metadata']['formula_structures'][0]
    assert f['origin'] == 'ocr_text' and f['structure_status'] == 'unknown'
    assert f['computation_ast'] is None
    assert result['pages'] == before


def test_ordinary_aligned_numeric_text_is_not_excluded_as_toc():
    text = 'Revenue    12\nCost    5\nMargin .......... 7'
    blocks, offset = [], 0
    for line in text.split('\n'):
        blocks.append({'text': line, 'start': offset, 'end': offset + len(line)})
        offset += len(line) + 1
    result = {'metadata': {}, 'pages': [{'number': 1, 'text': text, 'blocks': blocks}]}
    structure = ds.build(result, format='pdf')
    assert structure['toc_entries'] == [] and structure['excluded_spans'] == []


def test_two_dotted_price_lines_are_only_candidates_without_toc_title():
    lines = ['Product A .......... 12', 'Product B .......... 15']
    text, offset, blocks = '\n'.join(lines), 0, []
    for line in lines:
        blocks.append({'text': line, 'start': offset, 'end': offset + len(line)})
        offset += len(line) + 1
    structure = ds.build({'metadata': {}, 'pages': [{'number': 1, 'text': text, 'blocks': blocks}]}, format='pdf')
    assert len(structure['toc_entries']) == 2 and not structure['excluded_spans']
    assert all(t['classification'] == 'candidate_requires_review' for t in structure['toc_entries'])


def test_overdeep_content_control_cannot_silently_drop_source_text():
    body = ET.fromstring(f'<w:body xmlns:w="{W}">' + '<w:sdtContent>' * 40 + paragraph('Never silently drop') + '</w:sdtContent>' * 40 + '</w:body>')
    with pytest.raises(ValueError, match='无法完整读取'):
        list(ds.docx_body_blocks(body))


def test_existing_ocr_skew_observation_is_reported_with_its_actual_field():
    report = ds.profiles({'pages': [{'number': 1, 'extraction': 'ocr', 'quality': {'skew_degrees': -8.5, 'ocr_mean_confidence': .7}}]})
    p = report['quality']['pages'][0]
    assert p['observations']['skew_degrees'] == -8.5 and 'skew_detected' in p['flags']
    assert 'skew_degrees' not in p['not_assessed'] and report['quality']['accuracy_estimate'] is None
