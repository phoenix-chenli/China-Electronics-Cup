"""Stage 6: actual HTTP, source files, PDF geometry and historical references."""
import hashlib
import io
import json
import time
import unittest
import uuid
import urllib.request
import urllib.parse
import zipfile
from PIL import Image
import pymupdf
from tests.test_conversations import LocalClient,TEMP,conv,s,engine
from backend import documents as docs
from backend import docx_reader as docx


def pdf_bytes(rotation=0,scan=False):
    pdf=pymupdf.open()
    p=pdf.new_page(width=600,height=800)
    if scan:
        img=Image.new('RGB',(200,120),'#bdc8d7');buf=io.BytesIO();img.save(buf,format='PNG')
        p.insert_image(pymupdf.Rect(60,80,540,480),stream=buf.getvalue())
    else:
        p.insert_text((60,90),'Revenue: 1,240,000 CNY',fontsize=18)
        p.insert_text((60,130),'Growth: 18.7%',fontsize=14)
    p.set_rotation(rotation)
    p=pdf.new_page(width=600,height=800)
    p.insert_text((60,90),'Repeated value: 42',fontsize=14)
    p.insert_text((60,180),'Repeated value: 42',fontsize=14)
    pdf.set_toc([[1,'Results',1],[2,'Repeated values',2]])
    result=pdf.tobytes();pdf.close();return result


class DocumentReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.client=LocalClient()
    @classmethod
    def tearDownClass(cls):conv.manager.pool.shutdown(wait=True);cls.client.close();TEMP.cleanup()
    def source(self,text,fmt='md'):
        p=s.UPLOADS/(uuid.uuid4().hex+'.'+fmt)
        p.write_bytes(text if isinstance(text,bytes) else text.encode())
        return s.index_document(p),p
    def get(self,doc,path='',**params):
        url='/api/documents/'+doc['id']+path+'?version='+doc['sha256']
        url+=''.join('&'+k+'='+str(v) for k,v in params.items())
        return self.client.get(url)
    def bind(self,doc,quote,page=1,**extra):
        return self.client.post('/api/evidence/resolve',json={'locator':{'document_id':doc['id'],'sha256':doc['sha256'],'page':page,**extra},'quote':quote})
    def test_unicode_and_real_heading_offsets(self):
        text='# 经营\n\n## 收入\n😀 营业收入：100万元。\n\n### 口径\n净额。\n'
        d,_=self.source(text);meta=self.get(d,'/view').json();p=self.get(d,'/pages/1').json()
        self.assertEqual(p['text'],text);self.assertEqual([h['level'] for h in meta['outline']],[1,2,3])
        r=self.bind(d,'😀 营业收入：100万元。').json();span=r['locator']['text_range']
        self.assertEqual(r['status'],'located');self.assertEqual(text[span['start']:span['end']],r['quote'])
        self.assertEqual(span['start'],text.index('😀'))
    def test_ambiguous_quote_requires_explicit_occurrence(self):
        d,_=self.source('重复值42\n\n重复值42\n')
        r=self.bind(d,'重复值42').json();self.assertEqual(r['status'],'ambiguous');self.assertNotIn('text_range',r['locator'])
        p=self.get(d,'/pages/1').json();start=p['text'].rindex('重复值42')
        r=self.bind(d,'重复值42',text_range={'start':start,'end':start+5}).json()
        self.assertEqual(r['status'],'located');self.assertEqual(r['locator']['text_range']['start'],start)
    def test_wrong_range_does_not_fall_back(self):
        d,_=self.source('安全的来源\n相同文字\n')
        r=self.bind(d,'相同文字',text_range={'start':0,'end':4}).json()
        self.assertEqual(r['status'],'unlocated')
        r=self.bind(d,'安全的来源',text_range={'start':0,'end':5,'text_sha256':'bad'}).json()
        self.assertEqual(r['status'],'unlocated')
    def test_historical_file_and_parse_stay_pinned(self):
        old,path=self.source('# 规则\n\n营业收入：100万元。\n')
        bound=self.bind(old,'营业收入：100万元。').json();parse=bound['locator']['parse_id']
        path.write_text('# 规则\n\n营业收入：125万元。\n');new=s.index_document(path)
        self.assertNotEqual(old['sha256'],new['sha256'])
        meta=self.get(old,'/view',parse_id=parse).json();self.assertFalse(meta['document']['is_current'])
        self.assertIn('100万元',self.get(old,'/pages/1',parse_id=parse).json()['text'])
        self.assertIn('125万元',self.get(new,'/pages/1').json()['text'])
        self.assertEqual(self.bind(old,'营业收入：100万元。',parse_id=parse).json()['status'],'located')
        self.assertIn('100万元',self.get(old,'/original').text)
    def test_foreign_version_and_parse_are_rejected(self):
        a,_=self.source('来源甲');b,_=self.source('来源乙')
        parse=self.get(a,'/view').json()['parse_id']
        self.assertEqual(self.client.get('/api/documents/'+b['id']+'/original?version='+a['sha256']).status_code,400)
        self.assertEqual(self.get(b,'/view',parse_id=parse).status_code,400)
    def test_tampered_original_cannot_be_reported_located(self):
        d,_=self.source('原始金额：100元')
        path=s.VERSIONS/(d['sha256']+'.md');original=path.read_bytes();path.write_text('原始金额：200元')
        try:self.assertEqual(self.bind(d,'原始金额：200元').status_code,400)
        finally:path.write_bytes(original)
    def test_pdf_rotation_geometry_matches_independent_search(self):
        for rotation in (0,90,180,270):
            blob=pdf_bytes(rotation);d,_=self.source(blob,'pdf')
            r=self.bind(d,'Revenue: 1,240,000 CNY').json();self.assertEqual(r['status'],'located',r)
            p=self.get(d,'/pages/1').json();self.assertEqual(p['rotation'],rotation)
            with pymupdf.open(stream=blob,filetype='pdf') as pdf:
                page=pdf[0];rect=page.search_for('Revenue: 1,240,000 CNY')[0]*page.rotation_matrix
                expected=[rect.x0/page.rect.width,rect.y0/page.rect.height,rect.x1/page.rect.width,rect.y1/page.rect.height]
            boxes=r['locator']['rects'];union=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
            for a,b in zip(expected,union):self.assertAlmostEqual(a,b,places=4)
            self.assertEqual([x['level'] for x in self.get(d,'/view').json()['outline']],[1,2])
    def test_render_aspect_ratio_and_page_validation(self):
        d,_=self.source(pdf_bytes(90),'pdf')
        with urllib.request.urlopen(self.client.url+'/api/documents/'+d['id']+'/pages/1/image?version='+d['sha256']) as response:
            with Image.open(io.BytesIO(response.read())) as im:self.assertAlmostEqual(im.width/im.height,800/600,places=3)
        for n in (0,-1,3):self.assertEqual(self.get(d,'/pages/'+str(n)).status_code,400)
        self.assertEqual(self.get(d,'/pages/wrong').status_code,422)
    def test_scan_is_not_text_or_vision_verified(self):
        d,_=self.source(pdf_bytes(scan=True),'pdf');p=self.get(d,'/pages/1').json()
        self.assertFalse(p['text_available']);self.assertFalse(p['vision_verified']);self.assertTrue(p['ocr_required']);self.assertTrue(p['images'])
        self.assertEqual(self.bind(d,'不存在的文字').json()['status'],'unlocated')
        r=self.client.post('/api/documents/'+d['id']+'/reparse',json={'page':1,'version':d['sha256']});self.assertEqual(r.status_code,400)
    def test_local_reparse_keeps_previous_parse_and_other_pages(self):
        d,_=self.source(pdf_bytes(),'pdf');meta=self.get(d,'/view').json();old=self.get(d,'/pages/2',parse_id=meta['parse_id']).json()
        r=self.client.post('/api/documents/'+d['id']+'/reparse',json={'page':1,'version':d['sha256'],'parse_id':meta['parse_id']}).json()
        self.assertEqual(r['before_parse_id'],meta['parse_id']);self.assertNotEqual(r['after_parse_id'],meta['parse_id']);self.assertFalse(r['changed'])
        after=self.get(d,'/pages/2',parse_id=r['after_parse_id']).json();self.assertEqual(old['blocks'],after['blocks'])
        self.assertEqual(self.get(d,'/pages/1',parse_id=meta['parse_id']).status_code,200)
    def test_page_search_counts_occurrences_not_files(self):
        d,_=self.source(pdf_bytes(),'pdf');r=self.get(d,'/search',q='Repeated%20value').json()
        self.assertEqual(len(r['matches']),2);self.assertTrue(all(x['page']==2 for x in r['matches']))
        self.assertTrue(all(x['rects'] for x in r['matches']))
    def test_scoped_question_stays_in_page_and_version(self):
        d,path=self.source('# 版本甲\n\n营业收入：100万元。\n');a=self.bind(d,'营业收入：100万元。').json()
        path.write_text('# 版本乙\n\n营业收入：200万元。');s.index_document(path)
        other,_=self.source('# 其他文档\n\n营业收入：900万元。\n')
        sid=self.client.post('/api/projects/default/sessions').json()['id']
        task=self.client.post('/api/tasks',json={'question':'解释选中文字','session_id':sid,'document_context':a['locator']|{'quote':a['quote']}}).json()
        for _ in range(300):
            task=self.client.get('/api/tasks/'+task['id']).json()
            if task['status'] not in conv.ACTIVE:break
            time.sleep(.01)
        self.assertEqual(task['status'],'completed',task)
        result=task['result'];self.assertIn('100万元',result['answer']);self.assertNotIn('200万元',result['answer']);self.assertNotIn('900万元',result['answer'])
        self.assertEqual(result['evidence'][0]['locator']['document_id'],d['id'])
        self.assertEqual(len(self.client.get('/api/sessions/'+sid).json()['messages']),2)
    def test_broken_original_is_retained_but_not_reported_readable(self):
        d,_=self.source(b'%PDF-1.7\ninvalid source fixture\n','pdf')
        self.assertEqual(d['status'],'failed')
        self.assertEqual(self.get(d,'/view').status_code,400)
        self.assertEqual(self.get(d,'/pages/1/image').status_code,400)
        self.assertEqual(self.get(d,'/original').status_code,200)

    def test_followup_preserves_document_scope_but_new_sales_query_leaves_it(self):
        d,_=self.source('# 范围测试\n\n营业收入：100万元。\n')
        scoped=self.bind(d,'营业收入：100万元。').json()
        a=engine.execute('解释这段文字',document_context=scoped['locator']|{'quote':scoped['quote']})
        b=engine.execute('继续解释收入',previous=a['query_spec'])
        self.assertEqual(b['evidence'][0]['locator']['sha256'],d['sha256'])
        self.assertEqual(b['query_spec']['document_context']['document_id'],d['id'])
        c=engine.execute('2024 年华东净销售额是多少？',previous=b['query_spec'])
        self.assertNotIn('document_context',c['query_spec'])
        self.assertTrue(c['sql'])

    def test_claim_checks_are_scoped_and_formula_recomputed(self):
        r=engine.execute('2024 年华东销售冠军是谁？结合复盘说明方法和奖金。')
        formula=next(c for c in r['claims'] if c['kind']=='formula')
        check=self.client.post('/api/evidence/verify',json={'run_id':r['run_id'],'claim_id':formula['id']}).json()
        self.assertEqual(check['checks'][0]['status'],'passed');self.assertEqual(check['checks'][1]['status'],'passed')
        self.assertFalse(check['visual_verified']);self.assertEqual(check['claim_id'],formula['id'])
        excerpt=next(c for c in r['claims'] if c['kind']=='excerpt')
        check=docs.verify_claim(r['run_id'],excerpt['id']);self.assertEqual(check['checks'][1]['status'],'not_checked')
    def test_comparable_units_and_conflicts_are_explicit(self):
        for second,status in [('营业收入：1000000元。','passed'),('营业收入：1100000元。','needs_review')]:
            a,_=self.source('营业收入：100万元。');b,_=self.source(second)
            evs=[]
            for n,(d,t) in enumerate([(a,'营业收入：100万元。'),(b,second)]):
                bound=self.bind(d,t).json();evs.append({'id':str(n),'type':'document','snippet':t,'source':d['name'],'locator':bound['locator']})
            r={'run_id':str(uuid.uuid4()),'question':'比较收入','status':'completed','claims':[{'id':'c','text':'收入核验','evidence_ids':['0','1']}],'evidence':evs}
            s.snapshot_run(r);check=docs.verify_claim(r['run_id'],'c');self.assertEqual(check['checks'][2]['status'],status)

    def docx_source(self,elements):
        return self.source(docx.build(elements),'docx')
    def test_docx_headings_table_and_exact_location(self):
        d,_=self.docx_source([
            {"kind":"paragraph","text":"服务办法","level":1},
            {"kind":"paragraph","text":"本节说明免费维修期。","level":2},
            {"kind":"paragraph","text":"免费维修期为签收次日起 18 个月。","level":0},
            {"kind":"table","rows":[["零件","额度"],["电池","280 元"]]},
        ])
        self.assertEqual(d['status'],'ready')
        meta=self.get(d,'/view').json()
        self.assertEqual([h['level'] for h in meta['outline']],[1,2])
        self.assertEqual(meta['structure'],'Word 标题与表格')
        p=self.get(d,'/pages/1').json()
        self.assertTrue(p['text_available']);self.assertFalse(p['ocr_required'])
        for block in p['blocks']:
            self.assertEqual(p['text'][block['start']:block['end']],block['text'])
        quote='免费维修期为签收次日起 18 个月。'
        r=self.bind(d,quote).json()
        self.assertEqual(r['status'],'located');self.assertEqual(r['quote'],quote)
        self.assertEqual(r['locator']['location_precision'],'text_range')
        rows=[b for b in p['blocks'] if b['kind']=='table_row']
        self.assertTrue(rows);self.assertIn('280 元',p['text'])
        self.assertEqual(self.get(d,'/search',q=urllib.parse.quote('280 元')).json()['matches'][0]['page'],1)
    def test_docx_heading_path_matches_markdown_convention(self):
        d,_=self.docx_source([{"kind":"paragraph","text":"甲","level":1},
                              {"kind":"paragraph","text":"乙","level":2},
                              {"kind":"paragraph","text":"正文。","level":0}])
        blocks=self.get(d,'/pages/1').json()['blocks']
        self.assertEqual(blocks[0]['title_path'],'甲')
        self.assertEqual(blocks[1]['title_path'],'甲 / 乙')
        self.assertEqual(blocks[2]['title_path'],'甲 / 乙')
    def test_docx_failures_are_explicit_and_original_is_retained(self):
        broken,_=self.source(b'this is not a zip archive','docx')
        self.assertEqual(broken['status'],'failed')
        self.assertTrue(broken['quality']['errors'])
        self.assertEqual(self.get(broken,'/view').status_code,400)
        self.assertEqual(self.get(broken,'/pages/1').status_code,400)
        self.assertEqual(self.get(broken,'/original').status_code,200)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:archive.writestr('hello.txt','no body part')
        missing,_=self.source(buffer.getvalue(),'docx')
        self.assertEqual(missing['status'],'failed')
        self.assertTrue(missing['quality']['errors'])
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:archive.writestr('word/document.xml','<w:document><w:body>')
        malformed,_=self.source(buffer.getvalue(),'docx')
        self.assertEqual(malformed['status'],'failed')
    def test_docx_recognition_flag_only_when_an_image_is_embedded(self):
        plain,_=self.docx_source([{"kind":"paragraph","text":"   ","level":0}])
        self.assertEqual(plain['status'],'ready')
        self.assertNotIn('需要文字识别',plain['quality']['quality_flags'])
        self.assertFalse(self.get(plain,'/pages/1').json()['ocr_required'])
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml','<?xml version="1.0"?><w:document xmlns:w="'+docx.NS+
                             '"><w:body><w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>')
            archive.writestr('word/media/image1.png',b'\x89PNG\r\n\x1a\n'+b'0'*32)
        scan,_=self.source(buffer.getvalue(),'docx')
        self.assertEqual(scan['status'],'needs_review')
        self.assertIn('需要文字识别',scan['quality']['quality_flags'])
        page=self.get(scan,'/pages/1').json()
        self.assertTrue(page['ocr_required']);self.assertFalse(page['text_available'])
        self.assertFalse(page['vision_verified'])
    def test_docx_reparse_keeps_outline_and_pins_previous_parse(self):
        d,_=self.docx_source([{"kind":"paragraph","text":"标题甲","level":1},
                              {"kind":"paragraph","text":"正文。","level":0}])
        meta=self.get(d,'/view').json()
        record=self.client.post('/api/documents/'+d['id']+'/reparse',
                                 json={'page':1,'version':d['sha256'],'parse_id':meta['parse_id']}).json()
        self.assertFalse(record['changed'])
        self.assertNotEqual(record['after_parse_id'],meta['parse_id'])
        self.assertEqual([h['level'] for h in self.get(d,'/view',parse_id=record['after_parse_id']).json()['outline']],[1])
        self.assertEqual(self.get(d,'/pages/1',parse_id=meta['parse_id']).status_code,200)
        self.assertEqual(self.bind(d,'正文。',parse_id=meta['parse_id']).json()['status'],'located')


if __name__=='__main__':unittest.main()
