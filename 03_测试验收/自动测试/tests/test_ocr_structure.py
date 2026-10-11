"""Geometry and source-boundary regressions; no model accuracy claims."""
import unittest
from xml.etree import ElementTree as ET
from PIL import Image, ImageDraw
from backend import ocr_structure as spatial, document_native as native, math_structure


def line(text,x,y,width=.04,height=.02,status='confirmed'):
    return {'text':text,'box':[x,y,x+width,y+height],'status':status,'confidence':.95}


class StructureTest(unittest.TestCase):
    def test_repeated_columns_keep_headers_and_row_names(self):
        lines=[line('Model',.1,.1,.1),line('Dev',.4,.1),line('Test',.6,.1)]
        for y,name,a,b in [(.15,'Alpha','10','20'),(.2,'Beta','30','40'),(.25,'Gamma','50','60')]:
            lines += [line(name,.1,y,.1),line(a,.4,y),line(b,.6,y)]
        tables=spatial.borderless_tables(lines)
        self.assertEqual(len(tables),1)
        page=native.apply_ocr_page(native._page(1),{'lines':lines,'tables':tables})
        self.assertIn('Test：40',page['text'])
        self.assertIn('Beta',next(b['text'] for b in page['blocks'] if b['kind']=='table_row' and 'Test：40' in b['text']))
        for block in page['blocks']:
            self.assertEqual(block['text'],page['text'][block['start']:block['end']])

    def test_paragraph_numbers_are_not_a_table(self):
        self.assertFalse(spatial.borderless_tables([line('In 2020 revenue 20',.1,.1,.6),line('In 2021 revenue 30',.1,.2,.6)]))
    def test_rotated_grid_uses_reading_order_but_original_boxes(self):
        lines=[]
        for y,name in [(.1,'A'),(.15,'B'),(.2,'C')]:lines += [line(name,.1,y,.1),line('10',.4,y),line('20',.6,y)]
        rotated=[]
        for observation in lines:
            x0,y0,x1,y1=observation['box']
            rotated.append({**observation,'box':[y0,1-x1,y1,1-x0]})
        found=spatial.detect(rotated,90)['tables']
        self.assertEqual(len(found),1);self.assertEqual(found[0]['columns'],3)
        self.assertEqual(next(c for c in found[0]['cells'] if c['text']=='A')['box'],rotated[0]['box'])
    def test_short_following_section_reuses_observed_grid(self):
        lines=[line('Model',.1,.1,.1),line('Dev',.4,.1),line('Test',.6,.1)]
        for y in [.15,.2,.25]:lines += [line('Old',.1,y,.1),line('10',.4,y),line('20',.6,y)]
        lines.append(line('Next group',.1,.3,.1))
        for y in [.35,.4]:lines += [line('New',.1,y,.1),line('30',.4,y),line('40',.6,y)]
        tables=spatial.borderless_tables(lines)
        self.assertEqual(len(tables),2)
        self.assertIn('Next group',tables[1]['title'])
        page=native.apply_ocr_page(native._page(1),{'lines':lines,'tables':tables})
        self.assertIn('Test：40',page['text'])

    def test_previous_data_row_never_becomes_a_column_header(self):
        lines=[line('Model',.1,.1,.1),line('Dev',.4,.1),line('Test',.6,.1),
               line('Earlier',.1,.15,.1),line('999',.4,.15),line('888',.6,.15),line('Section',.1,.19,.1)]
        for y in [.23,.27,.31]:lines += [line('Later',.1,y,.1),line('10',.4,y),line('20',.6,y)]
        table=spatial.borderless_tables(lines)[0]
        self.assertFalse(any(c['text'] in {'999','888'} and c['role']=='column_header' for c in table['cells']))

    def test_uncertain_cells_do_not_become_supported_rows(self):
        lines=[]
        for y in [.1,.15,.2]:lines += [line('Model',.1,y,.1),line('10',.4,y,status='pending'),line('20',.6,y)]
        tables=spatial.borderless_tables(lines)
        self.assertEqual(tables[0]['status'],'pending')
        page=native.apply_ocr_page(native._page(1),{'lines':lines,'tables':tables})
        self.assertFalse(any(b['kind']=='table_row' for b in page['blocks']))

    def test_fraction_requires_printed_bar_and_both_terms(self):
        image=Image.new('RGB',(200,120),'white');ImageDraw.Draw(image).line((85,60,115,60),fill='black',width=2)
        lines=[line('1',.46,.25,.08,.2),line('|A|',.44,.55,.12,.2)]
        found=spatial._fractions(image,lines)
        self.assertEqual(found[0]['latex'],r'\frac{1}{|A|}')
        self.assertFalse(spatial._fractions(image,lines[:1]))

    def test_incomplete_display_formula_does_not_enter_confirmed_text(self):
        lines=[line('ψ() =',.4,.4,.06),line('1',.47,.39,.01),line('∑',.5,.4),line('yi∈A',.5,.425)]
        formulas=spatial.formulas(lines)
        self.assertTrue(formulas)
        page=native.apply_ocr_page(native._page(1),{'lines':lines,'formulas':formulas})
        self.assertTrue(page['formulas'])
        self.assertFalse(page['text_available'])
        self.assertTrue(all(b['status']=='pending' for b in page['blocks']))

    def test_office_fraction_and_sum_keep_hierarchy(self):
        ns=math_structure.M[1:-1]
        xml=f'<m:oMath xmlns:m="{ns}"><m:f><m:num><m:r><m:t>1</m:t></m:r></m:num><m:den><m:r><m:t>N</m:t></m:r></m:den></m:f><m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr><m:sub><m:r><m:t>i=1</m:t></m:r></m:sub><m:sup><m:r><m:t>N</m:t></m:r></m:sup><m:e><m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e><m:sub><m:r><m:t>i</m:t></m:r></m:sub></m:sSub></m:e></m:nary></m:oMath>'
        node=ET.fromstring(xml)
        self.assertEqual(math_structure.latex(node),r'\frac{1}{N}\sum_{i=1}^{N} {x}_{i}')
        paragraph=ET.fromstring(f'<w:p xmlns:w="{native.NS["w"]}">{xml}</w:p>')
        self.assertIn(r'\frac{1}{N}',native._paragraph(paragraph))

    def test_unsupported_office_math_is_explicit(self):
        with self.assertRaises(ValueError):math_structure.latex(ET.fromstring('<m:unknown xmlns:m="'+math_structure.M[1:-1]+'"/>'))


if __name__=='__main__':unittest.main()
