"""Public synthetic OOXML/PDF observations; no model-accuracy claims."""
import copy
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
from backend import document_native as native, rag_index


def docx(path,body,extra=None):
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="'+native.NS['w']+'" xmlns:a="'+native.NS['a']+'" xmlns:r="'+native.NS['r']+'" xmlns:c="'+native.NS['c']+'"><w:body>'+body+'</w:body></w:document>')
        for name,value in (extra or {}).items():archive.writestr(name,value)


class NativeDocumentTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native-public-');self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_docx_original_merge_and_unique_vertical_value(self):
        path=self.root/'merged.docx'
        docx(path,'<w:p><w:r><w:t>收入说明</w:t></w:r><w:del><w:r><w:t>删除值999</w:t></w:r></w:del></w:p>'
            '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>'
            '<w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>收入（万元）</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>华北</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p><w:r><w:t>200</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        data=native.parse_docx(path);page=data['pages'][0];table=page['tables'][0]
        self.assertEqual(table['cells'][0]['colspan'],2)
        region=next(c for c in table['cells'] if c['text']=='华北')
        self.assertEqual(region['rowspan'],2);self.assertEqual(sum(c['text']=='华北' for c in table['cells']),1)
        self.assertEqual(page['text'].count('华北'),2)  # derived row views retain the actual merged-cell identity
        self.assertNotIn('删除值',page['text']);self.assertIn('收入（万元）：200',page['text'])
        self.assertEqual(page['page_kind'],'logical_section')
        for block in page['blocks']:
            self.assertEqual(page['text'][block['start']:block['end']],block['text'])
            self.assertEqual(block['chars'],[]);self.assertEqual(block['location_precision'],'package_structure')
    def test_chart_uses_cache_not_pixels_and_media_bytes(self):
        image=Image.new('RGB',(12,8),'white');buf=io.BytesIO();image.save(buf,format='PNG')
        chart='<c:chartSpace xmlns:c="'+native.NS['c']+'"><c:chart><c:plotArea><c:barChart><c:ser><c:tx><c:v>收入</c:v></c:tx><c:cat><c:strCache><c:pt idx="0"><c:v>2026</c:v></c:pt></c:strCache></c:cat><c:val><c:numCache><c:pt idx="0"><c:v>123.50</c:v></c:pt></c:numCache></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>'
        path=self.root/'chart.docx';docx(path,'<w:p><w:r><w:drawing><a:blip r:embed="img"/><c:chart r:id="chart"/></w:drawing></w:r></w:p>',
            {'word/charts/chart1.xml':chart,'word/media/image.png':buf.getvalue(),
             'word/_rels/document.xml.rels':'<Relationships><Relationship Id="img" Target="media/image.png"/><Relationship Id="chart" Target="charts/chart1.xml"/><Relationship Id="external" TargetMode="External" Target="https://example.invalid/private"/></Relationships>'})
        page=native.parse_docx(path)['pages'][0]
        self.assertIn('123.50',page['text']);self.assertEqual(page['charts'][0]['series'][0]['points'][0]['value_source'],'native_cache')
        self.assertIn('单位未标明',page['text'])
        raw,mime=native.read_docx_media(path,page['images'][0]['id']);self.assertEqual(raw,buf.getvalue());self.assertEqual(mime,'image/png')
    def test_invalid_package_entity_and_merge_rejected(self):
        path=self.root/'bad.docx'
        docx(path,'<w:p/>',{'../escape':'x'})
        with self.assertRaises(ValueError):native.parse_docx(path)
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('word/document.xml','<!DOCTYPE x [<!ENTITY y "expanded">]><x/>')
        with self.assertRaises(ValueError):native.parse_docx(path)
        docx(path,'<w:tbl><w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr></w:tbl>')
        with self.assertRaises(ValueError):native.parse_docx(path)
    def test_native_pdf_grid_cites_actual_cells(self):
        path=self.root/'table.pdf';pdf=pymupdf.open();page=pdf.new_page(width=500,height=500)
        for x in (50,200,350):page.draw_line((x,50),(x,170))
        for y in (50,90,130,170):page.draw_line((50,y),(350,y))
        for x,y,text in [(65,75,'Region'),(215,75,'Revenue'),(65,115,'North'),(215,115,'100'),(65,155,'South'),(215,155,'200')]:page.insert_text((x,y),text)
        pdf.save(path);pdf.close()
        base={'number':1,'text':'','blocks':[],'images':[],'headings':[],'text_available':False,'ocr_required':False}
        result=native.enhance_pdf_page(path,1,base)
        self.assertEqual(len(result['tables']),1)
        table=result['tables'][0];self.assertEqual((table['rows'],table['columns']),(3,2))
        self.assertEqual([c['text'] for c in table['cells']],['Region','Revenue','North','100','South','200'])
        self.assertTrue(all(c['box'] and c['status']=='confirmed' for c in table['cells']))
        self.assertIn('Revenue：100',result['text'])
    def test_pending_ocr_and_estimated_chart_are_display_only(self):
        base={'number':1,'text':'old native','blocks':[],'headings':[],'text_available':True,'ocr_required':False,'location_precision':'character'}
        result=native.apply_ocr_page(base,{'lines':[{'text':'金额100万元','box':[.1,.1,.5,.2],'confidence':.98}, {'text':'不可靠999万元','box':[.1,.3,.5,.4],'confidence':.2}]})
        self.assertIn('100',result['text']);self.assertNotIn('999',result['text'])
        self.assertEqual(len(result['recognition_pending']),1);self.assertEqual(result['location_precision'],'line')
        self.assertEqual(base['text'],'old native')
        visual={'charts':[{'status':'confirmed','series':[{'name':'收入','points':[{'label':'A','value':'200','status':'confirmed','value_source':'estimated'}]}]}]}
        after=native.apply_visual_page(result,visual)
        self.assertNotIn('200',after['text']);self.assertEqual(len(after['charts']),1)
    def test_defensive_rag_ranges_never_include_pending_observation(self):
        page={'text':'确认100\n待核999\n确认200','blocks':[{'start':6,'end':11,'status':'pending'}]}
        ranges=list(rag_index._confirmed_ranges(page))
        self.assertEqual(ranges,[(0,6),(11,len(page['text']))])
        self.assertTrue(all('999' not in page['text'][a:b] for a,b in ranges))
    def test_embedded_image_reparse_replaces_only_same_media_offsets(self):
        base=native._page(1);native._append(base,'正文原件',source_method='docx_native',location_precision='package_structure')
        def result(text):return {'lines':[{'text':text,'box':[.1,.1,.5,.2],'confidence':.99}]}
        first=native.apply_ocr_page(base,result('图一旧金额100'),'image-a')
        second=native.apply_ocr_page(first,result('图二金额200'),'image-b')
        final=native.apply_ocr_page(second,result('图一新金额300'),'image-a')
        self.assertNotIn('旧金额100',final['text']);self.assertIn('正文原件',final['text']);self.assertIn('图二金额200',final['text'])
        self.assertEqual(len({b['id'] for b in final['blocks']}),len(final['blocks']))
        for block in final['blocks']:self.assertEqual(final['text'][block['start']:block['end']],block['text'])
    def test_real_grid_geometry_preserves_merged_cells_and_unread_ink(self):
        image=Image.new('RGB',(400,240),'white');draw=ImageDraw.Draw(image)
        for x in (20,380):draw.line((x,20,x,220),fill='black',width=2)
        draw.line((200,90,200,220),fill='black',width=2)
        for y in (20,90,150,220):draw.line((20,y,380,y),fill='black',width=2)
        draw.text((40,40),'Header',fill='black');draw.text((40,110),'North',fill='black')
        draw.text((230,110),'100',fill='black');draw.text((40,180),'Unread',fill='black')
        encoded=io.BytesIO();image.save(encoded,format='PNG')
        lines=[{'text':text,'box':[x/400,y/240,(x+65)/400,(y+15)/240],'confidence':.99} for x,y,text in [(40,40,'Header'),(40,110,'North'),(230,110,'100')]]
        tables=native.detect_ruled_tables(encoded.getvalue(),lines)
        self.assertEqual(len(tables),1);table=tables[0]
        self.assertEqual(table['cells'][0]['colspan'],2)
        self.assertEqual(table['status'],'pending')
        unread=next(c for c in table['cells'] if c['row']==2 and c['column']==0)
        self.assertEqual(unread['status'],'pending');self.assertEqual(unread['text'],'')
        blank=next(c for c in table['cells'] if c['row']==2 and c['column']==1)
        self.assertTrue(blank['blank']);self.assertEqual(blank['status'],'confirmed')
        draw.text((230,180),'0',fill='black');encoded=io.BytesIO();image.save(encoded,format='PNG')
        changed=native.detect_ruled_tables(encoded.getvalue(),lines)[0]
        unread_zero=next(c for c in changed['cells'] if c['row']==2 and c['column']==1)
        self.assertFalse(unread_zero['blank']);self.assertEqual(unread_zero['status'],'pending')
        weak=copy.deepcopy(lines);weak[2].update(confidence=.77,status='pending')
        self.assertEqual(next(c for c in native.detect_ruled_tables(encoded.getvalue(),weak)[0]['cells'] if c['row']==1 and c['column']==1)['status'],'pending')
    def test_pending_chart_cannot_reenter_rag_through_raw_ocr_numbers(self):
        page=native.apply_ocr_page(native._page(1),{'lines':[{'text':'数值120','box':[.2,.2,.4,.3],'confidence':.99},{'text':'文字说明','box':[.1,.8,.5,.9],'confidence':.99}]})
        after=native.apply_visual_page(page,{'charts':[{'box':[.1,.1,.7,.7],'status':'pending','series':[]}]})
        ranges=list(rag_index._confirmed_ranges(after))
        self.assertTrue(all('120' not in after['text'][a:b] for a,b in ranges))
        self.assertIn('文字说明',''.join(after['text'][a:b] for a,b in ranges))
    def test_cross_page_relation_only_candidate_and_not_different_headers(self):
        def table(tid,header,box):return {'id':tid,'columns':2,'status':'confirmed','box':box,'cells':[{'column':0,'text':header,'role':'header'}]}
        pages=[{'number':1,'tables':[table('a','收入',[.1,.5,.9,.9])]}, {'number':2,'tables':[table('b','收入',[.1,.1,.9,.4])]}]
        self.assertEqual(native.link_continuations(pages)[0]['status'],'candidate')
        changed=copy.deepcopy(pages);changed[1]['tables'][0]['cells'][0]['text']='成本'
        self.assertEqual(native.link_continuations(changed),[])


if __name__=='__main__':unittest.main()
