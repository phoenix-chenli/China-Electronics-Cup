"""Physical partial rules are evidence; aligned prose and broken data are not."""
from PIL import Image, ImageDraw

from backend.ocr_ruled import detect_merged_tables


def _sample(*, broken_body=False, missing_frame=False, draw_rules=True):
    image = Image.new('RGB', (600, 700), 'white')
    draw = ImageDraw.Draw(image)
    xs = [50, 160, 270, 380, 520]
    ys = [100, 150, 200, 245, 290, 335, 380, 425]
    if draw_rules:
        # Page decoration must not extend the table frame to the whole page.
        draw.line((50, 30, 520, 30), fill='black')
        draw.line((50, 650, 520, 650), fill='black')
        for y in ys:
            draw.line((160 if y == 150 else 50, y, 380 if y == 150 else 520, y), fill='black')
        for x in xs:
            if missing_frame and x == 50:
                continue
            draw.line((x, 150 if x == 270 else 100, x, 425), fill='black')
        if broken_body:
            draw.line((270, 292, 270, 333), fill='white')
    words = []

    def word(value, x, y):
        draw.text((x, y), value, fill='black')
        words.append({'text': value, 'bbox': [x/600, y/700, (x+30)/600, (y+10)/700],
                      'confidence': .9, 'line_key': (1, 1, len(words)+1, 1)})

    word('Region', 75, 120)
    word('Metric', 240, 120)
    word('Status', 420, 120)
    word('A', 190, 170)
    word('B', 300, 170)
    for r in range(2, 7):
        y = ys[r]+12
        word(f'Item{r}', 75, y)
        for c in range(1, 4):
            word(f'{r}.{c}', xs[c]+18, y)
    return image, words


def test_physical_partial_header_rules_preserve_merge_anchors():
    image, words = _sample()
    original = image.tobytes()
    tables = detect_merged_tables(image, words)
    assert len(tables) == 1
    table = tables[0]
    assert (table['row_count'], table['column_count']) == (7, 4)
    cells = {(c['row'], c['col']): c for c in table['cells']}
    assert cells[1, 1]['rowspan'] == 2
    assert cells[1, 2]['colspan'] == 2
    assert cells[2, 1]['covered_by'] == [1, 1]
    assert cells[1, 3]['covered_by'] == [1, 2]
    assert cells[2, 1]['text'] is None and table['rows'][1][0] is None
    assert table['rows'][2][1] == '2.1'
    assert table['requires_review'] and table['sql_eligible']
    assert table['structure_basis'] == 'physical_segment_rules'
    assert table['data_row_offset'] == 0
    assert table['bbox'][1] > .1 and table['bbox'][3] < .7
    assert image.tobytes() == original


def test_body_separator_damage_is_not_invented_numeric_merge():
    image, words = _sample(broken_body=True)
    assert detect_merged_tables(image, words) == []


def test_open_frame_does_not_certify_merged_cells():
    image, words = _sample(missing_frame=True)
    assert detect_merged_tables(image, words) == []


def test_aligned_words_without_drawn_rules_are_not_tables():
    image, words = _sample(draw_rules=False)
    assert detect_merged_tables(image, words) == []


def test_crossing_ocr_word_is_retained_outside_cells_without_assignment():
    image, words = _sample()
    words.append({'text': 'uncertain', 'bbox': [258/600, 252/700, 280/600, 264/700],
                  'confidence': .4, 'line_key': (1, 1, 99, 1)})
    table = detect_merged_tables(image, words)[0]
    assert table['unassigned_words'][0]['text'] == 'uncertain'
    assert all('uncertain' not in (c['text'] or '') for c in table['cells'])


def test_empty_or_unlocalized_observations_are_not_table_content():
    image, words = _sample()
    assert detect_merged_tables(image, []) == []
    assert detect_merged_tables(image, [{'text': 'word', 'bbox': [0, 0, .01, .01],
                                        'confidence': .8}]*3) == []
