"""Independent, public synthetic DOCX/PDF goldens for exact table relations."""
from collections import Counter
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape

import pymupdf

from backend import document_native as native


# These expectations describe the authored source, not an implementation result.
# Row 3 / column 5 is deliberately empty; row 3 / column 4 is printed zero.
GOLDEN_VALUES = (
    (2, 2, '120', ('2024', 'Revenue (kCNY)'), ('North', 'A')),
    (2, 3, '80', ('2024', 'Cost (kCNY)'), ('North', 'A')),
    (2, 4, '150', ('2025', 'Revenue (kCNY)'), ('North', 'A')),
    (2, 5, '90', ('2025', 'Cost (kCNY)'), ('North', 'A')),
    (3, 2, '210', ('2024', 'Revenue (kCNY)'), ('North', 'B')),
    (3, 3, '140', ('2024', 'Cost (kCNY)'), ('North', 'B')),
    (3, 4, '0', ('2025', 'Revenue (kCNY)'), ('North', 'B')),
    (4, 2, '110', ('2024', 'Revenue (kCNY)'), ('South', 'C')),
    (4, 3, '70', ('2024', 'Cost (kCNY)'), ('South', 'C')),
    (4, 4, '130', ('2025', 'Revenue (kCNY)'), ('South', 'C')),
    (4, 5, '75', ('2025', 'Cost (kCNY)'), ('South', 'C')),
    (5, 2, '140', ('2024', 'Revenue (kCNY)'), ('South', 'D')),
    (5, 3, '100', ('2024', 'Cost (kCNY)'), ('South', 'D')),
    (5, 4, '155', ('2025', 'Revenue (kCNY)'), ('South', 'D')),
    (5, 5, '105', ('2025', 'Cost (kCNY)'), ('South', 'D')),
)


def _cell(text, *, colspan=1, merge=None):
    props = ''
    if colspan != 1:
        props += f'<w:gridSpan w:val="{colspan}"/>'
    if merge is not None:
        props += '<w:vMerge/>' if merge == 'continue' else '<w:vMerge w:val="restart"/>'
    return '<w:tc><w:tcPr>' + props + '</w:tcPr><w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p></w:tc>'


def _write_docx(path):
    rows = [
        (True, [_cell('Region', merge='restart'), _cell('Item', merge='restart'),
                _cell('2024', colspan=2), _cell('2025', colspan=2)]),
        (True, [_cell('', merge='continue'), _cell('', merge='continue'),
                _cell('Revenue (kCNY)'), _cell('Cost (kCNY)'),
                _cell('Revenue (kCNY)'), _cell('Cost (kCNY)')]),
        (False, [_cell('North', merge='restart'), _cell('A'), _cell('120'), _cell('80'), _cell('150'), _cell('90')]),
        (False, [_cell('', merge='continue'), _cell('B'), _cell('210'), _cell('140'), _cell('0'), _cell('')]),
        (False, [_cell('South', merge='restart'), _cell('C'), _cell('110'), _cell('70'), _cell('130'), _cell('75')]),
        (False, [_cell('', merge='continue'), _cell('D'), _cell('140'), _cell('100'), _cell('155'), _cell('105')]),
    ]
    body = '<w:tbl><w:tblGrid>' + '<w:gridCol/>' * 6 + '</w:tblGrid>'
    for header, cells in rows:
        body += '<w:tr>' + ('<w:trPr><w:tblHeader/></w:trPr>' if header else '') + ''.join(cells) + '</w:tr>'
    body += '</w:tbl>'
    xml = '<w:document xmlns:w="' + native.NS['w'] + '"><w:body>' + body + '</w:body></w:document>'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', xml)


def _write_pdf(path):
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=360)
    xs = [40, 110, 180, 270, 360, 450, 540]
    ys = [50, 90, 130, 170, 210, 250, 290]
    for index in (0, 1, 2, 4, 6):
        page.draw_line((xs[index], ys[0]), (xs[index], ys[6]))
    for index in (3, 5):
        page.draw_line((xs[index], ys[1]), (xs[index], ys[6]))
    for index in (0, 2, 4, 6):
        page.draw_line((xs[0], ys[index]), (xs[6], ys[index]))
    page.draw_line((xs[2], ys[1]), (xs[6], ys[1]))
    for index in (3, 5):
        page.draw_line((xs[1], ys[index]), (xs[6], ys[index]))
    labels = [
        (0, 0, 'Region', 2), (0, 1, 'Item', 2),
        (0, 2, '2024', 1), (0, 4, '2025', 1),
        (1, 2, 'Revenue (kCNY)', 1), (1, 3, 'Cost (kCNY)', 1),
        (1, 4, 'Revenue (kCNY)', 1), (1, 5, 'Cost (kCNY)', 1),
        (2, 0, 'North', 2), (4, 0, 'South', 2),
        (2, 1, 'A', 1), (3, 1, 'B', 1), (4, 1, 'C', 1), (5, 1, 'D', 1),
    ]
    for row, column, text, span in labels:
        page.insert_text((xs[column] + 6, (ys[row] + ys[row + span]) / 2 + 3), text, fontsize=9)
    # Author data separately from the expected relation tuples above.
    for row, values in [(2, ['120', '80', '150', '90']), (3, ['210', '140', '0', '']),
                        (4, ['110', '70', '130', '75']), (5, ['140', '100', '155', '105'])]:
        for column, value in enumerate(values, 2):
            if value:
                page.insert_text((xs[column] + 8, ys[row] + 24), value, fontsize=10)
    pdf.save(path)
    pdf.close()


class NativeTableGoldenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='table-native-goldens-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _assert_golden(self, page):
        self.assertEqual(len(page['tables']), 1)
        table = page['tables'][0]
        self.assertEqual((table['rows'], table['columns']), (6, 6))
        self.assertEqual(len(table['cells']), len({cell['id'] for cell in table['cells']}))
        by_id = {cell['id']: cell for cell in table['cells']}
        by_origin = {(cell['row'], cell['column']): cell for cell in table['cells']}
        self.assertEqual(len(by_origin), len(table['cells']))
        blank = by_origin[3, 5]
        self.assertEqual(blank['text'], '')
        self.assertEqual(by_origin[3, 4]['text'], '0')
        self.assertEqual(by_origin[2, 0]['rowspan'], 2)
        self.assertEqual(by_origin[4, 0]['rowspan'], 2)
        self.assertEqual(by_origin[0, 2]['colspan'], 2)
        self.assertEqual(by_origin[0, 4]['colspan'], 2)
        blocks = [block for block in page['blocks'] if block.get('table_id') == table['id']]
        self.assertEqual(len(blocks), 4)
        bindings = [binding for block in blocks for binding in block['cell_bindings']]
        self.assertEqual(len(bindings), 15)
        self.assertEqual(len({binding['value_cell_id'] for binding in bindings}), 15)
        self.assertNotIn(blank['id'], {binding['value_cell_id'] for binding in bindings})
        self.assertEqual(Counter(by_id[binding['value_cell_id']]['text'] for binding in bindings),
                         Counter(value for _, _, value, _, _ in GOLDEN_VALUES))
        self.assertEqual(sum(by_id[binding['value_cell_id']]['text'] == '0' for binding in bindings), 1)
        for row, column, text, column_path, row_path in GOLDEN_VALUES:
            with self.subTest(row=row, column=column, value=text):
                value = by_origin[row, column]
                self.assertEqual(value['text'], text)
                binding = next(binding for binding in bindings if binding['value_cell_id'] == value['id'])
                self.assertEqual(value['column_path'], list(column_path))
                self.assertEqual(binding['column_path'], list(column_path))
                self.assertEqual(binding['row_path'], list(row_path))
                year_column = 2 if column in (2, 3) else 4
                group_row = 2 if row in (2, 3) else 4
                self.assertEqual(binding['column_header_ids'], [by_origin[0, year_column]['id'], by_origin[1, column]['id']])
                self.assertEqual(binding['row_header_ids'], [by_origin[group_row, 0]['id'], by_origin[row, 1]['id']])
                self.assertEqual(binding['row_header_label_ids'], [by_origin[0, 0]['id'], by_origin[0, 1]['id']])
                self.assertEqual([by_id[item]['text'] for item in binding['column_header_ids']], list(column_path))
                self.assertEqual([by_id[item]['text'] for item in binding['row_header_ids']], list(row_path))
                for ancestor in binding['column_header_ids']:
                    source = by_id[ancestor]
                    self.assertLessEqual(source['row'] + source.get('rowspan', 1), row)
                    self.assertLessEqual(source['column'], column)
                    self.assertLess(column, source['column'] + source.get('colspan', 1))
                for ancestor in binding['row_header_ids']:
                    source = by_id[ancestor]
                    self.assertLessEqual(source['row'], row)
                    self.assertLess(row, source['row'] + source.get('rowspan', 1))
                block = next(block for block in blocks if binding in block['cell_bindings'])
                self.assertEqual(page['text'][block['start']:block['end']], block['text'])
                self.assertTrue(set(binding['column_header_ids'] + binding['row_header_ids'] + [value['id']]) <= set(block['cell_ids']))

    def test_real_docx_parser_preserves_all_15_independently_authored_bindings(self):
        path = self.root / 'public-complex.docx'
        _write_docx(path)
        result = native.parse_docx(path)
        self.assertEqual(len(result['pages']), 1)
        self._assert_golden(result['pages'][0])

    def test_real_pdf_parser_preserves_all_15_independently_authored_bindings(self):
        path = self.root / 'public-complex.pdf'
        _write_pdf(path)
        result = native.enhance_pdf_page(path, 1, native._page(1))
        self._assert_golden(result)


if __name__ == '__main__':
    unittest.main()
