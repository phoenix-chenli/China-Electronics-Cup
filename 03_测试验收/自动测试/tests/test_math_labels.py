"""Real structures and bounded crop-contract fixtures; not model accuracy claims."""
import io
import unittest
from xml.etree import ElementTree as ET
from PIL import Image
from backend import math_structure as math,ocr_structure as spatial,document_native as native

class MathLabelsTest(unittest.TestCase):
    def node(self,xml):return ET.fromstring('<m:oMath xmlns:m="'+math.M[1:-1]+'">'+xml+'</m:oMath>')
    def term(self,text):return '<m:e><m:r><m:t>'+text+'</m:t></m:r></m:e>'
    def test_nested_matrix_and_delimiters(self):
        xml='<m:d><m:dPr><m:begChr m:val="["/><m:endChr m:val="]"/></m:dPr><m:e><m:m><m:mr>'+self.term('1')+self.term('2')+'</m:mr><m:mr>'+self.term('3')+self.term('4')+'</m:mr></m:m></m:e></m:d>'
        self.assertEqual(math.latex(self.node(xml)),r'\left[\begin{matrix}1&amp;2\\3&amp;4\end{matrix}\right]'.replace('&amp;','&'))
    def test_unconfirmed_label_excludes_its_row_not_other_verified_rows(self):
        table={'id':'t','rows':2,'columns':2,'status':'pending','cells':[{'id':'a','row':0,'column':0,'rowspan':1,'text':'Bad','role':'row_header','status':'pending','label_review':'pending'},{'id':'b','row':0,'column':1,'text':'10','status':'confirmed'},{'id':'c','row':1,'column':0,'text':'Good','role':'row_header','status':'confirmed','label_review':'consistent'},{'id':'d','row':1,'column':1,'text':'20','status':'confirmed'}]}
        page=native._page(1);native._table_rows(page,table,'ocr_geometry')
        self.assertIn('Good',page['text']);self.assertNotIn('Bad',page['text']);self.assertNotIn('10',page['text'])
        table['cells'][0].update(role='column_header')
        page=native._page(1);native._table_rows(page,table,'ocr_geometry');self.assertFalse(page['text'])
    def test_lower_bar_is_not_an_upper_bar(self):
        self.assertEqual(math.latex(self.node('<m:bar><m:barPr><m:pos m:val="bot"/></m:barPr>'+self.term('x')+'</m:bar>')),r'\underline{x}')
    def test_docx_formula_structure_retains_source_binding(self):
        import tempfile
        from pathlib import Path
        from tests.test_document_native import docx
        xml=ET.tostring(self.node('<m:d>'+self.term('x')+'</m:d>'),encoding='unicode')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'formula.docx';docx(path,'<w:p>'+xml+'</w:p>')
            page=native.parse_docx(path)['pages'][0]
        formula=page['formulas'][0]
        self.assertEqual(formula['status'],'confirmed');self.assertEqual(formula['latex'],r'\left(x\right)')
        self.assertIn(formula['id'],page['blocks'][0]['formula_ids'])
        self.assertTrue(formula['source_part'].startswith('word/document.xml'))
        self.assertIn('oMath',formula['original_xml'])
    def test_ragged_matrix_rejected(self):
        xml='<m:m><m:mr>'+self.term('1')+'</m:mr><m:mr>'+self.term('2')+self.term('3')+'</m:mr></m:m>'
        with self.assertRaises(ValueError):math.latex(self.node(xml))
    def test_function_limit_and_accent(self):
        xml='<m:func><m:fName><m:r><m:t>sin</m:t></m:r></m:fName>'+self.term('x')+'</m:func>'
        self.assertEqual(math.latex(self.node(xml)),r'\operatorname{sin} x')
        xml='<m:limLow>'+self.term('lim')+'<m:lim><m:r><m:t>x→0</m:t></m:r></m:lim></m:limLow><m:acc><m:accPr><m:chr m:val="⃗"/></m:accPr>'+self.term('v')+'</m:acc>'
        self.assertEqual(math.latex(self.node(xml)),r'{lim}_{x→0}\vec{v}')
    def test_barless_fraction_does_not_turn_into_division(self):
        xml='<m:f><m:fPr><m:type m:val="noBar"/></m:fPr><m:num><m:r><m:t>n</m:t></m:r></m:num><m:den><m:r><m:t>k</m:t></m:r></m:den></m:f>'
        self.assertEqual(math.latex(self.node(xml)),r'\genfrac{}{}{0pt}{}{n}{k}')
    def fixture(self):
        buf=io.BytesIO();Image.new('RGB',(200,100),'white').save(buf,format='PNG')
        lines=[{'text':'BlurryOriginal','box':[.2,.2,.8,.6],'confidence':.65,'status':'pending'}]
        result={'lines':lines,'tables':[{'id':'t','status':'pending','cells':[{'role':'row_header','text':'BlurryOriginal','evidence_lines':[0],'status':'pending'}]}]}
        return buf.getvalue(),result
    def provider(self,text):return {'lines':([] if text is None else [{'text':text,'box':[.1,.1,.9,.9],'confidence':.95,'status':'confirmed'}]),'duration_ms':4,'input_image_sha256':'a'*64}
    def test_label_consistency_preserves_original(self):
        raw,result=self.fixture();calls=[]
        def recognize(data,**kwargs):calls.append(data);return self.provider('ClearLabel')
        spatial.reread_labels(raw,result,recognize)
        self.assertEqual(len(calls),2);self.assertEqual(result['lines'][0]['original_text'],'BlurryOriginal')
        self.assertEqual(result['lines'][0]['text'],'ClearLabel');self.assertEqual(result['tables'][0]['status'],'confirmed')
    def test_missing_or_disagreeing_crop_remains_pending(self):
        for second in [None,'OtherLabel']:
            raw,result=self.fixture();answers=iter(['ClearLabel',second])
            spatial.reread_labels(raw,result,lambda data,**kwargs:self.provider(next(answers)))
            self.assertEqual(result['lines'][0]['text'],'BlurryOriginal');self.assertEqual(result['lines'][0]['status'],'pending')
    def test_same_engine_agreement_does_not_certify_complex_typography(self):
        raw,result=self.fixture()
        spatial.reread_labels(raw,result,lambda data,**kwargs:self.provider('ClearLabel↑'))
        self.assertEqual(result['lines'][0]['status'],'pending')
        self.assertEqual(result['lines'][0]['text'],'BlurryOriginal')
    def test_diagram_words_are_not_compact_fraction_seeds(self):
        lines=[{'text':'source','box':[.4,.3,.45,.32],'status':'confirmed'}, {'text':'memory','box':[.4,.34,.45,.36],'status':'confirmed'}]
        self.assertFalse(spatial.formulas(lines))
    def test_crop_failure_is_recorded_and_cancel_propagates(self):
        raw,result=self.fixture()
        def failed(*args,**kwargs):raise ValueError('OCR failed')
        spatial.reread_labels(raw,result,failed)
        self.assertEqual(len(result['label_reviews'][0]['calls']),2)
        self.assertTrue(all(c['status']=='failed' for c in result['label_reviews'][0]['calls']))
        def cancelled(*args,**kwargs):raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):spatial.reread_labels(raw,result,cancelled)
    def test_sum_bounds_are_candidates_not_certified_latex(self):
        lines=[{'text':'∑','box':[.4,.4,.5,.5]},{'text':'N','box':[.42,.32,.48,.38]},{'text':'i=1','box':[.42,.52,.48,.58]}]
        nodes=spatial.math_relations(lines)
        self.assertEqual(nodes[0]['upper_candidates'],[1]);self.assertEqual(nodes[0]['lower_candidates'],[2]);self.assertEqual(nodes[0]['status'],'pending')

if __name__=='__main__':unittest.main()
