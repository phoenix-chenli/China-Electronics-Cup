"""Real-layout regressions; no model calls or OCR required."""
from backend import document_structure as ds
from backend.document_engine import _chunks


BODY = ('This is an unchanged explanatory body paragraph with a real value.', 12, False, [.1, .3, .8, .32])


def page(number, lines):
    text, blocks, offset = '\n'.join(row[0] for row in lines), [], 0
    for line, font, bold, box in lines:
        blocks.append({'text': line, 'font_size': font, 'bold': bold, 'bbox': box,
                       'start': offset, 'end': offset + len(line)})
        offset += len(line) + 1
    return {'number': number, 'text': text, 'blocks': blocks}


def build(pages, **metadata):
    source = {'pages': pages, 'metadata': {'page_unit': 'physical_page', **metadata}}
    source['metadata']['document_structure'] = ds.build(source, format='pdf')
    chunks, truncated = _chunks(source, lambda: False)
    assert not truncated
    for chunk in chunks:
        original = next(p for p in pages if p['number'] == chunk.page)
        assert chunk.text == original['text'][chunk.start:chunk.end]
        assert chunk.id == f'p{chunk.page}:{chunk.start}-{chunk.end}'
    assert [(c.page, c.start) for c in chunks] == sorted((c.page, c.start) for c in chunks)
    return source['metadata']['document_structure'], chunks


def test_manual_after_old_10000_record_prefix_keeps_actual_heading():
    pages = [page(1, [('4.2 Previous section', 12, True, [.1, .1, .6, .12])] + [BODY] * 10000),
             page(339, [('5.11.2 Point-Load Strength Test', 12, True, [.147, .109, .424, .126]), BODY]),
             page(340, [BODY])]
    structure, chunks = build(pages)
    assert structure['scanned_records'] > 10000 and not structure['truncated']
    assert all(c.heading_path == ('5.11.2 Point-Load Strength Test',) for c in chunks if c.page >= 339)


def test_budget_limit_stops_inheritance_at_last_inspected_offset(monkeypatch):
    monkeypatch.setattr(ds, 'MAX_STRUCTURE_RECORDS', 2)
    structure, chunks = build([page(1, [('1.1 Earlier section', 12, True, [.1, .1, .6, .12]), BODY]), page(2, [BODY])])
    assert structure['truncated']
    assert any(x['kind'] == 'structure_scan_truncated' for x in structure['issues'])
    assert all(not c.heading_path for c in chunks if c.page == 2)


def test_split_number_title_and_section_reference_are_not_confused():
    pages = [page(197, [('6. Start-up and Operation of UV Facilities', 14, True, [.27, .069, .73, .089]), BODY,
                       ('Chapter 6 covers:', 12, True, [.1, .4, .4, .42])]),
             page(212, [('6.3', 13.02, True, [.118, .094, .153, .113]),
                        ('Maintenance of UV Reactors', 13.02, True, [.176, .094, .469, .113]), BODY,
                        ('6.3.1 Summary of Recommended Maintenance Tasks', 12, True, [.118, .252, .626, .269])]),
             page(213, [('Table 6.4. Recommended Maintenance Tasks', 12, True, [.28, .08, .71, .095]),
                        ('Section 6.3.2.1', 10.02, False, [.278, .177, .392, .191]), BODY]),
             page(214, [('6.3.2 General Guidelines for UV Reactor Maintenance', 12, True, [.118, .094, .628, .111]),
                        ('6.3.2.1', 12, True, [.118, .199, .183, .216]),
                        ('Fouling', 12, True, [.206, .199, .282, .216]), BODY])]
    structure, chunks = build(pages)
    assert not ds._number_level('6.3')
    assert ds._number_level('6.3\nMaintenance of UV Reactors') == 2
    expected = ('6. Start-up and Operation of UV Facilities', '6.3\nMaintenance of UV Reactors',
                '6.3.1 Summary of Recommended Maintenance Tasks')
    assert all(c.heading_path == expected for c in chunks if c.page == 213)
    assert any(c.heading_path[-1] == '6.3.2.1\nFouling' for c in chunks if c.page == 214)
    assert not any(h['title'] == 'Section 6.3.2.1' for h in structure['headings'])
    assert not next(h for h in structure['headings'] if h['title'] == 'Chapter 6 covers:')['inheritance_allowed']


def test_chapter_local_running_headers_do_not_label_new_body():
    pages = []
    for number in range(40, 51):
        running = 'CONVECTION HEAT TRANSFER' if number < 48 else 'RADIATION HEAT TRANSFER'
        rows = [(running, 10, False, [.059, .062, .29, .077])]
        if number in (40, 48):
            title = 'CONVECTION HEAT TRANSFER' if number == 40 else 'RADIANT HEAT TRANSFER'
            rows.append((title, 16, True, [.264, .111, .618, .139]))
        pages.append(page(number, rows + [BODY]))
    structure, chunks = build(pages)
    assert all(c.heading_path == ('RADIANT HEAT TRANSFER',) for c in chunks if c.page >= 48)
    assert not any('RADIATION HEAT TRANSFER' in c.text for c in chunks if c.page >= 48)
    assert len([e for e in structure['excluded_spans'] if e['role'] == 'repeated_header']) == 11


def test_appendix_multiline_bookmark_resets_chapter_and_list_not_root():
    pages = [page(1, [('CHAPTER 5.0', 12, True, [.4, .1, .6, .12]), BODY,
                     ('5.', 12, True, [.1, .4, .12, .42]), ('Coefficient', 12, True, [.15, .4, .3, .42])]),
             page(267, [('Appendix A', 16, True, [.4, .4, .6, .42]),
                        ('Preparing and Assaying', 16, True, [.3, .44, .7, .46]),
                        ('Challenge Microorganisms', 16, True, [.3, .48, .7, .5])]),
             page(269, [('A.1', 13, True, [.1, .1, .15, .12]),
                        ('MS2 Phage Stock Preparation', 13, True, [.17, .1, .5, .12]), BODY])]
    outline = [{'level': 1, 'title': 'Appendix A. Preparing and Assaying Challenge Microorganisms',
                'page': 267, 'source': 'pdf_bookmark'}]
    structure, chunks = build(pages, outline=outline)
    assert not next(h for h in structure['headings'] if h['title'] == '5.\nCoefficient')['inheritance_allowed']
    assert len([h for h in structure['headings'] if h['source_position']['page'] == 267]) == 1
    assert all(c.heading_path[0].startswith('Appendix A\n') for c in chunks if c.page == 269)
    assert all('CHAPTER' not in ' '.join(c.heading_path) for c in chunks if c.page >= 267)


def test_toc_continued_and_caption_lists_never_supply_body_paths():
    pages = [page(1, [('List of Figures (Continued)', 14, True, [.3, .1, .7, .12]),
                     ('5.11 Result .......... 339', 12, True, [.1, .2, .8, .22])]),
             page(339, [('5.11 Result', 12, True, [.1, .1, .4, .12]), BODY])]
    structure, chunks = build(pages)
    assert all(h['source_position']['page'] != 1 for h in structure['headings'])
    assert all(c.page == 339 for c in chunks)


def test_legacy_unreviewed_candidate_is_not_a_retrieval_hint():
    source = {'pages': [{'number': 1, 'text': 'Uncertain\nOriginal body'}], 'metadata': {'document_structure': {
        'headings': [{'id': 'H1', 'structure_status': 'candidate_requires_review',
                      'source_position': {'page': 1, 'start': 0, 'end': 9},
                      'body_spans': [{'page': 1, 'start': 0, 'end': 23}]}]}}}
    chunks, _ = _chunks(source, lambda: False)
    assert chunks and all(not c.heading_path and not c.retrieval_hint for c in chunks)


def test_rotated_chart_fragments_do_not_replace_chapter():
    structure, chunks = build([page(1, [('CHAPTER 3.0', 12, True, [.4, .1, .6, .12]), BODY]),
                               page(2, [('str', 15.3, True, [.209, .58, .244, .602]),
                                        ('tation of the variation of overbu', 15.3, True, [.35, .135, .763, .159]), BODY]),
                               page(3, [BODY])])
    assert all(c.heading_path == ('CHAPTER 3.0',) for c in chunks if c.page == 3)
