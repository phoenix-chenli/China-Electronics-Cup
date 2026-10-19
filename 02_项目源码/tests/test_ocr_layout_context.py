"""OCR layout candidates keep exact source identity and legacy safety gates."""
import copy
import json

import pytest

from backend.table_bindings import check_table_bindings
from backend.table_context import context_for_span, table_text_spans
from backend.table_semantics import build_table_semantics


def layout_source():
    text, cells, blocks = '10\n', [], [{'start': 0, 'end': 2, 'bbox': [.05, .9, .1, .95]}]
    for row, values in enumerate([['对象', '金额'], ['甲', '10'], ['乙', '10']], 1):
        row_start = len(text)
        for col, value in enumerate(values, 1):
            start = len(text)
            box = [(col-1)/2, (row-1)/4, col/2, row/4]
            cells.append({'row': row, 'col': col, 'text': value, 'start': start, 'end': start+len(value),
                          'bbox': box, 'text_bbox': [box[0]+.05, box[1]+.05, box[2]-.05, box[3]-.05],
                          'offset_basis': 'ocr_cell_block', 'verification': 'page_ocr_geometry',
                          'rowspan': 1, 'colspan': 1, 'covered_by': None, 'confidence': .9})
            text += value+'\n'
        # A retained old broad box must not invalidate exact new cell offsets.
        blocks.append({'start': row_start, 'end': len(text)-1, 'bbox': [.01, (row-1)/4+.02, .99, row/4-.02]})
    region = {'table_name': '候选表', 'page': 1, 'bbox': [0, 0, 1, .75], 'cells': cells,
              'row_count': 3, 'column_count': 2, 'extraction': 'ocr_layout',
              'structure_basis': 'aligned_word_layout', 'sql_eligible': False, 'requires_review': True,
              'header_rows': [1], 'header_basis': 'candidate'}
    table = build_table_semantics(region, format='image')
    source = {'format': 'image', 'pages': [{'number': 1, 'text': text, 'blocks': blocks}],
              'metadata': {'table_semantics': [table]}}
    return source, table, region


def test_repeated_values_use_exact_offsets_and_preserve_candidate_provenance():
    source, table, _ = layout_source()
    before = copy.deepcopy(source)
    first, second = table['cells'][3], table['cells'][5]
    context = context_for_span(source, 1, first['start'], first['end'])
    selected = context['tables'][0]
    assert selected['selected_cell_ids'] == ['R2C2']
    assert not context.get('unresolved_structure')
    assert context_for_span(source, 1, second['start'], second['end'])['tables'][0]['selected_cell_ids'] == ['R3C2']
    assert not context_for_span(source, 1, 0, 2)['has_table']
    assert selected['header_basis'] == 'inferred'
    assert selected['requires_review'] and selected['sql_eligible'] is False
    assert selected['extraction'] == 'ocr_layout' and selected['structure_basis'] == 'aligned_word_layout'
    assert source == before


def test_row_spans_are_original_offsets_even_with_old_broad_boxes():
    source, _, _ = layout_source()
    spans = table_text_spans(source, 1)
    assert [(r['row'], source['pages'][0]['text'][r['start']:r['end']]) for r in spans['rows']] == [(2, '甲\n10'), (3, '乙\n10')]
    assert not any(row['row'] is None for row in spans['rows'])


@pytest.mark.parametrize('corruption', ['wrong_text', 'outside', 'boolean', 'duplicate'])
def test_invalid_ocr_offsets_never_fall_back_to_geometric_or_value_guessing(corruption):
    source, table, _ = layout_source()
    cell = table['cells'][3]
    start, end = cell['start'], cell['end']
    if corruption == 'wrong_text':
        cell['text'] = '100'
    elif corruption == 'outside':
        cell['end'] = 999
    elif corruption == 'boolean':
        cell['start'] = True
    else:
        table['cells'][5].update(start=start, end=end)
    result = context_for_span(source, 1, start, end)
    assert result['has_table'] and result['unresolved_structure']
    assert not result['tables'][0]['selected_cell_ids']


def test_legacy_broad_boxes_without_verified_cell_offsets_remain_unknown():
    source, table, _ = layout_source()
    start, end = table['cells'][3]['start'], table['cells'][3]['end']
    for cell in table['cells']:
        for key in ('start', 'end', 'offset_basis'):
            cell.pop(key)
    result = context_for_span(source, 1, start, end)
    assert result['unresolved_structure'] and not result['tables'][0]['selected_cell_ids']


def test_extra_non_cell_text_in_a_broad_box_stays_unresolved():
    source, table, _ = layout_source()
    page = source['pages'][0]
    cell = table['cells'][3]
    # The unassigned prefix has the same number. It is never absorbed by a
    # verified cell or excused merely because one selected offset is valid.
    page['blocks'].append({'start': 0, 'end': cell['end'], 'bbox': [.01, .26, .99, .49]})
    result = context_for_span(source, 1, 0, cell['end'])
    assert result['unresolved_structure']


@pytest.mark.parametrize('budget', [128, 250, 1000])
def test_projection_budget_loss_cannot_turn_layout_candidate_into_plain_evidence(budget):
    source, table, _ = layout_source()
    cell = table['cells'][3]
    result = context_for_span(source, 1, cell['start'], cell['end'], max_chars=budget)
    assert result['has_table'] and result['truncated'] and result['requires_review']
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= budget
    assert all(not t['selected_cell_ids'] for t in result['tables'])
    verdict = check_table_bindings('甲的金额为10。', [], [{'id': 'e1', 'table_context': result}])
    assert verdict['required'] and not verdict['valid']


def test_reread_candidates_are_bounded_audit_fields_and_never_replace_source_text():
    _, _, region = layout_source()
    region['cells'][3].update(ocr_disagreement=True, ocr_candidates=['10', '1.0'], crop_ocr_confidence=.98)
    table = build_table_semantics(region, format='image')
    assert table['cells'][3]['text'] == '10'
    assert table['cells'][3]['ocr_candidates'] == ['10', '1.0']
    assert table['cells'][3]['ocr_disagreement'] and table['cells'][3]['confidence'] == .9
    assert table['cells'][3]['text_bbox'] == region['cells'][3]['text_bbox']
    for cell in region['cells']:
        cell['ocr_candidates'] = ['9'*500]*10
    table = build_table_semantics(region, format='image')
    assert table['candidate_audit_truncated'] and table['requires_review']
    candidates = [value for cell in table['cells'] for value in cell.get('ocr_candidates', [])]
    assert sum(map(len, candidates)) <= 4096
    assert all(len(value) <= 256 for value in candidates)
    assert all(len(cell.get('ocr_candidates', [])) <= 4 for cell in table['cells'])
    assert table['cells'][3]['text'] == '10'


def test_merged_header_uses_real_coverage_and_does_not_duplicate_observed_value():
    region = {'table_name': '多级候选', 'page': 1, 'bbox': [0, 0, 1, 1], 'row_count': 3, 'column_count': 3,
              'extraction': 'ocr_layout', 'header_rows': [1, 2], 'header_basis': 'candidate',
              'cells': [
                  {'row': 1, 'col': 1, 'text': '对象', 'rowspan': 2, 'bbox': [0, 0, .3, .6]},
                  {'row': 1, 'col': 2, 'text': '金额', 'colspan': 2, 'bbox': [.3, 0, 1, .3]},
                  {'row': 1, 'col': 3, 'text': None, 'covered_by': [1, 2], 'bbox': [.65, 0, 1, .3]},
                  {'row': 2, 'col': 1, 'text': None, 'covered_by': [1, 1]},
                  {'row': 2, 'col': 2, 'text': '预算', 'bbox': [.3, .3, .65, .6]},
                  {'row': 2, 'col': 3, 'text': '实际', 'bbox': [.65, .3, 1, .6]},
                  {'row': 3, 'col': 1, 'text': '甲'}, {'row': 3, 'col': 2, 'text': '10'}, {'row': 3, 'col': 3, 'text': '10'}]}
    table = build_table_semantics(region, format='image')
    assert table['header_basis'] == 'inferred'
    assert [c['label'] for c in table['columns']] == ['对象', '金额 / 预算', '金额 / 实际']
    assert table['columns'][2]['header_path'][0]['evidence']['bbox'] == [.3, 0, 1, .3]
    assert next(c for c in table['cells'] if c['cell_id'] == 'R1C3')['text'] is None


def test_nine_column_two_level_ocr_context_fits_default_budget_without_losing_values():
    values = [[f'组{col//3+1}' for col in range(9)], ['对象', '预算（万元）', '实际（万元）']*3,
              ['北区', '-2.75', '1.00', '南区', '0.00', '4.50', '东区', '7.25', '6.50']]
    text, cells, blocks = '', [], []
    for row, row_values in enumerate(values, 1):
        for col, value in enumerate(row_values, 1):
            start = len(text)
            box = [(col-1)/9, (row-1)/4, col/9, row/4]
            cell = {'row': row, 'col': col, 'text': value, 'start': start, 'end': start+len(value),
                    'bbox': box, 'text_bbox': box, 'offset_basis': 'ocr_cell_block',
                    'rowspan': 1, 'colspan': 1, 'covered_by': None,
                    'confidence': .98, 'verification': 'same_engine_crop_agreement', 'requires_review': True}
            cells.append(cell)
            blocks.append({'text': value, 'start': start, 'end': start+len(value), 'bbox': box})
            text += value+'\n'
    table = build_table_semantics({'table_name': '九列表', 'page': 1, 'bbox': [0, 0, 1, .75],
        'row_count': 3, 'column_count': 9, 'cells': cells, 'extraction': 'ocr_grid',
        'structure_basis': 'physical_segment_rules', 'sql_eligible': True, 'requires_review': True},
        format='image', source_position={'page': 1, 'page_unit': 'physical_page'})
    source = {'pages': [{'number': 1, 'text': text, 'blocks': blocks}], 'metadata': {'table_semantics': [table]}}
    before = copy.deepcopy(source)
    cell = next(c for c in table['cells'] if c['cell_id'] == 'R3C2')
    result = context_for_span(source, 1, cell['start'], cell['end'])
    assert not result['truncated'] and result['requires_review']
    projection = result['tables'][0]
    assert projection['selected_cell_ids'] == ['R3C2']
    assert len(projection['cells']) == 27 and len(projection['columns']) == 9
    assert all(type(c.get('row')) is int and type(c.get('col')) is int for c in projection['cells'])
    assert [c['text'] for c in projection['cells'] if c.get('row') == 3] == values[2]
    assert next(c for c in projection['cells'] if c['cell_id'] == 'R3C2')['bbox'] == cell['bbox']
    assert all(path['evidence']['page'] == 1 and 'row' in path['evidence'] and 'col' in path['evidence']
               and text[path['evidence']['start']:path['evidence']['end']] == path['text']
               for column in projection['columns'] for path in column['header_path'])
    assert {unit['value'] for unit in projection['units']} == {'万元'}
    assert source == before
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 6000
    header_binding = {'table_name': '九列表', 'cell_id': 'R1C1', 'value': '组1',
                      'statement': '组1的值为组1。', 'column_label': '组1 / 对象', 'row_label': '组1'}
    malicious = copy.deepcopy(result)
    malicious['tables'][0]['selected_cell_ids'].append('R1C1')
    verdict = check_table_bindings('组1的值为组1。', [header_binding],
                                   [{'id': 'e1', 'table_context': malicious}])
    assert not verdict['valid'] and any('表头' in reason for reason in verdict['errors'])


@pytest.mark.parametrize('hint,expected', [([1, 2], 'inferred'), ([2], 'unknown'), ([True, 2], 'unknown')])
def test_numeric_year_layout_header_hint_is_candidate_and_must_be_contiguous(hint, expected):
    _, _, region = layout_source()
    region['header_rows'] = hint
    region['cells'][2]['text'] = '2023'
    region['cells'][3]['text'] = '2024'
    table = build_table_semantics(region, format='image')
    assert table['header_basis'] == expected and table['requires_review']
    assert table['header_basis'] != 'explicit'
