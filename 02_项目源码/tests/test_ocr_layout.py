"""Synthetic word-layout contracts; these tests do not measure OCR accuracy.

No fixture values or wording come from the evaluation answers. The detector may
propose source-local table structure but must never certify business semantics
or silently repair an OCR value.
"""
from copy import deepcopy

import pytest
from PIL import Image

from backend.ocr_layout import detect_layout_tables


def word(text, left, top, right, bottom, *, line=1, block=1):
    return {'text': text, 'bbox': [left, top, right, bottom],
            'confidence': .93, 'line_key': (1, block, 1, line)}


def simple_words(*, top=.10, step=.065, rows=None, separate_lines=False):
    """A label column and two numeric columns, all without ruling pixels."""
    rows = rows if rows is not None else [
        ['北区', '−2.75', '12.5%'],
        ['中区', '0.00', '8.25'],
        ['南区', '103.4', '7.50'],
    ]
    values = [['区域', '产出', '增长率'], *rows]
    result = []
    for r, row in enumerate(values):
        y = top + r * step
        for c, value in enumerate(row):
            if value is None:
                continue
            left, right = [( .10, .24), (.48, .59), (.76, .88)][c]
            result.append(word(value, left, y, right, y + .025,
                               line=100*r+c+1 if separate_lines else r+1,
                               block=c+1 if separate_lines else 1))
    return result


def detect(words, ruled_tables=()):
    return detect_layout_tables(Image.new('RGB', (1200, 1600), 'white'),
                                words, ruled_tables=ruled_tables)


def by_position(table):
    return {(cell['row'], cell['col']): cell for cell in table['cells']}


def assert_review_only(table):
    assert table['extraction'] == 'ocr_layout'
    assert table['structure_basis'] == 'aligned_word_layout'
    assert table['requires_review'] is True
    assert table['sql_eligible'] is False
    assert table['header_basis'] == 'inferred'
    assert table['data_row_offset'] == 0
    for cell in table['cells']:
        assert cell['requires_review'] is True
        assert len(cell['bbox']) == 4
        assert all(0 <= coordinate <= 1 for coordinate in cell['bbox'])


@pytest.mark.parametrize('separate_lines', [False, True])
def test_unruled_three_column_candidate_uses_geometry_and_preserves_literals(separate_lines):
    words = simple_words(separate_lines=separate_lines)
    original = deepcopy(words)
    tables = detect(list(reversed(words)))
    assert len(tables) == 1
    table = tables[0]
    assert_review_only(table)
    assert (table['row_count'], table['column_count']) == (4, 3)
    assert table['header_rows'] == [1]
    assert table['rows'] == [
        ['区域', '产出', '增长率'],
        ['北区', '−2.75', '12.5%'],
        ['中区', '0.00', '8.25'],
        ['南区', '103.4', '7.50'],
    ]
    cells = by_position(table)
    assert cells[2, 2]['text'] == '−2.75'
    assert cells[3, 2]['text'] == '0.00'
    assert words == original


def test_missing_numeric_observation_remains_none_without_shifting_columns():
    tables = detect(simple_words(rows=[
        ['甲区', '1.25', '2.50'], ['乙区', '3.75', None],
        ['丙区', '5.25', '6.50'], ['丁区', '7.75', '8.50'],
    ]))
    assert len(tables) == 1
    table = tables[0]
    assert_review_only(table)
    assert table['rows'][2] == ['乙区', '3.75', None]
    missing = by_position(table)[3, 3]
    assert missing['text'] is None
    assert missing.get('covered_by') is None
    assert table['rows'][3] == ['丙区', '5.25', '6.50']


def test_fragments_inside_label_cell_join_without_combining_neighboring_columns():
    words = simple_words()
    fragmented = []
    for observation in words:
        if observation['bbox'][0] == .10:
            left, top, right, bottom = observation['bbox']
            middle = (left + right) / 2
            for text, x1, x2 in [(observation['text'][0], left, middle-.005),
                                 (observation['text'][1:], middle+.005, right)]:
                fragmented.append({**observation, 'text': text, 'bbox': [x1, top, x2, bottom]})
        else:
            fragmented.append(observation)
    tables = detect(fragmented)
    assert len(tables) == 1
    assert tables[0]['rows'][0] == ['区域', '产出', '增长率']
    assert tables[0]['rows'][1] == ['北区', '−2.75', '12.5%']


def multilevel_words():
    result = [
        word('本期', .365, .10, .495, .125, line=1),
        word('同期', .685, .10, .815, .125, line=1),
    ]
    positions = [(.08, .22), (.31, .40), (.47, .56), (.63, .72), (.79, .88)]
    rows = [
        ['类别', '数量', '变化', '数量', '变化'],
        ['港口', '12.25', '−1.50', '11.75', '0.25'],
        ['陆运', '23.50', '2.75', '21.25', '−0.50'],
        ['航空', '34.75', '0.00', '30.25', '3.50'],
    ]
    for r, row in enumerate(rows):
        y = .165 + r * .065
        for c, text in enumerate(row):
            left, right = positions[c]
            result.append(word(text, left, y, right, y+.025, line=r+2))
    return result


def test_two_level_headers_are_merged_candidates_without_duplicate_source_values():
    tables = detect(multilevel_words())
    assert len(tables) == 1
    table = tables[0]
    assert_review_only(table)
    assert (table['row_count'], table['column_count']) == (5, 5)
    assert table['header_rows'] == [1, 2]
    assert table['has_merged_cells'] is True
    cells = by_position(table)
    for anchor_col, covered_col, text in [(2, 3, '本期'), (4, 5, '同期')]:
        assert cells[1, anchor_col]['text'] == text
        assert cells[1, anchor_col]['colspan'] == 2
        assert cells[1, covered_col]['text'] is None
        assert cells[1, covered_col]['covered_by'] == [1, anchor_col]
        assert table['rows'][0][covered_col-1] is None
    assert table['rows'][2] == ['港口', '12.25', '−1.50', '11.75', '0.25']
    assert table['rows'][4] == ['航空', '34.75', '0.00', '30.25', '3.50']


def test_less_than_three_data_rows_is_insufficient_to_infer_an_unruled_table():
    assert detect(simple_words(rows=[['甲', '1.5', '2.5'], ['乙', '3.5', '4.5']])) == []


def test_aligned_numbers_without_a_reliable_text_header_do_not_create_table():
    words = simple_words()[3:]
    assert detect(words) == []


def test_numeric_year_headers_abstain_instead_of_becoming_certified_body_columns():
    words = simple_words()
    words[1]['text'], words[2]['text'] = '2023', '2024'
    # A bare year cannot prove whether it denotes a header or a data value.
    assert detect(words) == []


@pytest.mark.parametrize('kind', ['prose', 'contents', 'prices', 'formula'])
def test_non_table_layouts_do_not_become_numeric_evidence(kind):
    words = []
    if kind == 'prose':
        rows = [
            ['区域发展持续推进。', '生产体系逐步完善。', '后续观察仍在开展。'],
            ['各地做好基础工作。', '分工协作不断加强。', '相关事项另行说明。'],
            ['产业布局稳步调整。', '服务能力持续提高。', '资料用于情况介绍。'],
            ['各方讨论下一阶段。', '项目建设有序开展。', '现有材料尚待核实。'],
        ]
        positions = [(.05, .30), (.37, .62), (.69, .94)]
    elif kind == 'contents':
        rows = [['章节', '页码'], ['概况', '12'], ['运行情况', '27'], ['附录', '58']]
        positions = [(.10, .45), (.82, .87)]
    elif kind == 'prices':
        rows = [['商品', '价格'], ['配件甲', '12.50'], ['配件乙', '23.75'], ['配件丙', '8.00']]
        positions = [(.10, .45), (.72, .87)]
    else:
        rows = [['变量', '等式', '结果'], ['x', '=', '1.25'], ['y', '=', '2.50'], ['z', '=', '3.75']]
        positions = [(.10, .24), (.48, .59), (.76, .88)]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            left, right = positions[c]
            y = .10 + r * .065
            words.append(word(text, left, y, right, y+.025, line=r+1))
    assert detect(words) == []


def test_separate_tables_cannot_be_joined_across_vertical_gap():
    first = simple_words(top=.08, step=.05)
    second = simple_words(top=.58, step=.05, rows=[
        ['东区', '21.5', '6.25'], ['西区', '23.5', '7.25'], ['远区', '25.5', '8.25'],
    ])
    tables = detect(first + second)
    assert len(tables) == 2
    assert all(table['row_count'] == 4 and table['column_count'] == 3 for table in tables)
    assert {table['rows'][1][0] for table in tables} == {'北区', '东区'}
    assert tables[0]['bbox'][3] < tables[1]['bbox'][1]


def test_side_by_side_tables_cannot_be_joined_across_a_persistent_gutter():
    rows = [
        ['Region', 'Value', 'Rate', 'Zone', 'Count', 'Growth'],
        ['Alpha', '12.5', '1.5', 'East', '21.5', '3.5'],
        ['Beta', '13.5', '2.5', 'West', '22.5', '4.5'],
        ['Gamma', '14.5', '3.5', 'South', '23.5', '5.5'],
    ]
    words = []
    for r, row in enumerate(rows):
        for left, text in zip([.04, .16, .28, .62, .74, .86], row):
            top = .10 + r*.06
            words.append(word(text, left, top, left+.055, top+.02, line=r+1))
    tables = detect(words)
    # Either source-local tables or abstention are safe. A fabricated six-column
    # relationship joining the two independently labelled regions is not.
    assert not any(t['bbox'][0] < .35 and t['bbox'][2] > .60 for t in tables)
    assert all(t['column_count'] == 3 for t in tables)


@pytest.mark.parametrize('left_width,right_width', [(2, 2), (2, 3), (3, 2)])
def test_two_column_panel_cannot_be_absorbed_into_neighboring_table(left_width, right_width):
    left_x = [.04, .16, .28][:left_width]
    right_x = ([.74, .86] if right_width == 2 else [.62, .74, .86])
    header = ['Left', 'Value', 'Rate'][:left_width] + ['Right', 'Count', 'Growth'][:right_width]
    rows = [header]
    for r in range(3):
        rows.append([f'L{r+1}', '12.5', '1.5'][:left_width]
                    + [f'R{r+1}', '21.5', '3.5'][:right_width])
    words = []
    for r, row in enumerate(rows):
        for left, text in zip(left_x + right_x, row):
            top = .10 + r*.06
            words.append(word(text, left, top, left+.055, top+.02, line=r+1))
    assert detect(words) == []


def test_wide_numeric_table_is_not_rejected_only_because_of_large_column_gap():
    positions = [.04, .16, .62, .74, .86]
    rows = [
        ['Region', 'ValueA', 'ValueB', 'RateA', 'RateB'],
        ['Alpha', '12.5', '21.5', '3.5', '1.5'],
        ['Beta', '13.5', '22.5', '4.5', '2.5'],
        ['Gamma', '14.5', '23.5', '5.5', '3.5'],
    ]
    words = []
    for r, row in enumerate(rows):
        for left, text in zip(positions, row):
            top = .10 + r*.06
            words.append(word(text, left, top, left+.055, top+.02, line=r+1))
    tables = detect(words)
    assert len(tables) == 1
    assert tables[0]['rows'] == rows
    assert tables[0]['column_count'] == 5
    assert_review_only(tables[0])


def test_existing_ruled_region_is_not_duplicated_as_a_layout_candidate():
    words = simple_words()
    ruled = {'bbox': [.05, .05, .95, .40], 'extraction': 'ocr_grid'}
    assert detect(words, [ruled]) == []


def test_cross_column_ocr_box_is_not_split_or_assigned_by_numeric_guess():
    words = simple_words()
    # One engine observation crosses both numeric columns. There is no literal
    # coordinate evidence for where the first value stops and the second starts.
    words = [w for w in words if w['line_key'][-1] != 3 or w['bbox'][0] < .3]
    words.append(word('0.00 8.25', .47, .23, .89, .255, line=3))
    assert detect(words) == []


def test_empty_or_unusable_word_boxes_do_not_create_table():
    assert detect([]) == []
    assert detect([word('孤立标题', .1, .1, .3, .13)]) == []
