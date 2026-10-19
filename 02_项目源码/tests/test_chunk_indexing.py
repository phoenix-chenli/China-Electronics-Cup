"""Indexing work must scale with pages, without changing source coordinates."""
import copy

import pytest

from backend import document_engine as engine
from backend.table_context import table_text_spans


class CountedPages(list):
    visits = 0

    def __iter__(self):
        for value in super().__iter__():
            self.visits += 1
            yield value


def test_many_pages_are_not_rescanned_for_every_chunk():
    # Count visits rather than asserting a machine-dependent time threshold.
    pages = CountedPages({'number': i, 'text': f'记录{i}原值12。'} for i in range(1, 5001))
    source = {'pages': pages, 'metadata': {}}
    chunks, truncated = engine._chunks(source, lambda: False)
    assert not truncated and len(chunks) == 5000
    assert pages.visits <= 4 * len(pages)
    assert chunks[-1].page == 5000 and chunks[-1].text == '记录5000原值12。'


def test_hierarchy_exclusions_and_exact_offsets_survive_indexing():
    source = {'pages': [{'number': 1, 'text': '目录\n第一章\n原句12。\n小节\n原句34。'}], 'metadata': {
        'document_structure': {'excluded_spans': [{'page': 1, 'start': 0, 'end': 3}], 'headings': [
            {'id': 'H1', 'source_position': {'page': 1, 'start': 3, 'end': 6},
             'body_spans': [{'page': 1, 'start': 7, 'end': 22}]},
            {'id': 'H2', 'parent_id': 'H1', 'source_position': {'page': 1, 'start': 13, 'end': 15},
             'body_spans': [{'page': 1, 'start': 16, 'end': 21}]},
            {'id': 'bad', 'source_position': {'page': 1, 'start': -1, 'end': 2},
             'body_spans': [{'page': 1, 'start': 0, 'end': 22}]},
        ]}}}
    # Adjust only fixture spans to actual raw characters; no normalization.
    text = source['pages'][0]['text']
    headings = source['metadata']['document_structure']['headings']
    headings[0]['body_spans'][0]['end'] = len(text)
    headings[1]['source_position'].update(start=text.index('小节'), end=text.index('小节')+2)
    headings[1]['body_spans'][0].update(start=text.index('原句34'), end=len(text))
    original = copy.deepcopy(source)
    chunks, _ = engine._chunks(source, lambda: False)
    assert source == original
    assert all(chunk.text == text[chunk.start:chunk.end] for chunk in chunks)
    assert not any('目录' in chunk.text for chunk in chunks)
    tail = next(chunk for chunk in chunks if '原句34' in chunk.text)
    assert tail.heading_path == ('第一章', '小节') and tail.parent_id == 'H2'
    assert engine._structure_spans(source, 1, len(text)) == engine._structure_spans(
        source, 1, len(text), index=engine._structure_index(source))


def test_table_lookup_keeps_first_duplicate_page_and_heading_lookup_keeps_last():
    first = {'number': 1, 'text': '项目\n甲12\n', 'blocks': []}
    last = {'number': 1, 'text': '末章\n乙34\n', 'blocks': []}
    table = {'table_name': 'T', 'source_position': {'page': 1, 'start': 0, 'end': 6},
             'header_rows': [1], 'cells': [{'row': 1, 'col': 1, 'start': 0, 'end': 2, 'text': '项目'},
                                        {'row': 2, 'col': 1, 'start': 3, 'end': 6, 'text': '甲12'}]}
    source = {'pages': [first, last], 'metadata': {'table_semantics': [table], 'document_structure': {
        'headings': [{'id': 'H', 'source_position': {'page': 1, 'start': 0, 'end': 2},
                      'body_spans': [{'page': 1, 'start': 3, 'end': 6}]}]}}}
    assert table_text_spans(source, 1) == table_text_spans(source, 1, current_page=first)
    assert engine._structure_spans(source, 1, 7)[1][0][1] == ('末章',)
    chunks, _ = engine._chunks(source, lambda: False)
    assert [c.text for c in chunks] == ['甲12', '乙34']
    assert all(c.heading_path == ('末章',) for c in chunks)


def test_scan_limit_remains_exact_and_is_disclosed(monkeypatch):
    monkeypatch.setattr(engine, 'MAX_SCAN_CHARACTERS', 11)
    source = {'pages': [{'number': 1, 'text': '原文123。'}, {'number': 2, 'text': '后文456789。'}]}
    chunks, truncated = engine._chunks(source, lambda: False)
    assert truncated
    assert [(c.page, c.start, c.end, c.text) for c in chunks] == [(1, 0, 6, '原文123。'), (2, 0, 5, '后文456')]


def test_preexisting_cancellation_does_not_scan_page_index():
    pages = CountedPages([{'number': 1, 'text': '原文'}])
    with pytest.raises(engine.DocumentCancelled):
        engine._chunks({'pages': pages}, lambda: True)
    assert pages.visits == 0


def test_invalid_physical_page_does_not_become_a_valid_position():
    with pytest.raises(engine.DocumentEngineError, match='原文位置'):
        engine._chunks({'pages': [{'number': True, 'text': '原文'}]}, lambda: False)
