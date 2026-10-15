"""Immutable corrections and pending evidence guards over a real isolated HTTP app."""
import copy
import json
import unittest
import uuid
from unittest.mock import patch
from tests.test_conversations import LocalClient,TEMP,conv,s
from backend import documents,document_review,rag_index


class DocumentReviewTests(unittest.TestCase):
    def test_visual_formula_review_clears_quality_warning_and_keeps_candidate_history(self):
        from backend import document_native as native, document_providers as providers
        d,_,body=self.source('')
        parsed={k:copy.deepcopy(v) for k,v in body.items() if k not in {'document','parse_id'}}
        payload={'blocks':[],'tables':[],'charts':[],'issues':[],'provenance':{},'formulas':[{'coordinate_space':'normalized_1000','box':[100,100,900,900],'tree':{'kind':'fraction','numerator':{'kind':'token','value':'a'},'denominator':None}}]}
        parsed['pages'][0]=native.apply_visual_page(parsed['pages'][0],providers._validate_visual(payload,[]))
        with s.conn() as db:data=documents.save_parse(db,d['id'],d['sha256'],parsed,'视觉公式候选夹具')
        target='block:'+data['pages'][0]['blocks'][0]['id']
        with patch.object(rag_index,'submit_index',return_value={'id':'formula-index'}):
            response=self.correct(d,data,target,text=r'\frac{a}{b}')
        self.assertEqual(response.status_code,200,response.text)
        page=documents.layout(d['id'])['pages'][0]
        self.assertEqual(page['formulas'][0]['status'],'confirmed')
        self.assertFalse(any(isinstance(i,dict) and i.get('code')=='formula_structure_incomplete' for i in page['issues']))
        old=documents.layout(d['id'],parse_id=data['parse_id'])['pages'][0]
        self.assertEqual(old['formulas'][0]['status'],'pending')
        self.assertFalse(old['formulas'][0]['structure_complete'])
    @classmethod
    def setUpClass(cls):cls.client=LocalClient()
    @classmethod
    def tearDownClass(cls):
        conv.manager.pool.shutdown(wait=True);cls.client.close();TEMP.cleanup()
    def source(self,text='限额为100万元。\n下一条保持不变。'):
        p=s.UPLOADS/(uuid.uuid4().hex+'.md');p.write_text(text,encoding='utf-8')
        d=s.index_document(p);return d,p,documents.layout(d['id'])
    def correct(self,d,data,target,**values):
        return self.client.post('/api/documents/'+d['id']+'/review',json={
            'version':d['sha256'],'parse_id':data['parse_id'],'page':1,'target':target,
            'operator':'公开夹具验收者','accepted':True,**values})
    def test_label_cell_correction_updates_original_line_and_clears_pending(self):
        from backend import document_native as native
        for status in ['pending','confirmed']:
            d,_,body=self.source('')
            lines=[{'text':'BlurLabel','box':[.1,.1,.3,.2],'confidence':.95,'status':status},{'text':'10','box':[.4,.1,.5,.2],'confidence':.95,'status':'confirmed'}]
            tables=[{'id':'t','rows':1,'columns':2,'status':status,'box':[.1,.1,.5,.2],'cells':[{'id':'a','row':0,'column':0,'text':'BlurLabel','box':lines[0]['box'],'role':'row_header','status':status,'evidence_lines':[0],'label_review':status},{'id':'b','row':0,'column':1,'text':'10','role':'data','status':'confirmed'}]}]
            parsed={k:copy.deepcopy(v) for k,v in body.items() if k not in {'document','parse_id'}}
            parsed['pages'][0]=native.apply_ocr_page(parsed['pages'][0],{'lines':lines,'tables':tables,'label_reviews':[{'observation_id':0,'candidates':['ClearLabel','OtherLabel']}]})
            with s.conn() as db:documents.save_parse(db,d['id'],d['sha256'],parsed,'public label fixture')
            before=documents.layout(d['id']);target=next(t for t in document_review.targets(before['pages'][0]) if t.get('ocr_candidates'))
            self.assertEqual(target['ocr_candidates'],['ClearLabel','OtherLabel'])
            with patch.object(rag_index,'submit_index',return_value={'id':'job'}):response=self.correct(d,before,target['id'],text='ClearLabel')
            self.assertEqual(response.status_code,200,response.text)
            page=documents.layout(d['id'])['pages'][0]
            self.assertFalse(page['recognition_pending']);self.assertNotIn('BlurLabel',page['text'])
            self.assertEqual(page['ocr_lines'][0]['text'],'ClearLabel')
            self.assertEqual(documents.layout(d['id'],d['sha256'],before['parse_id'])['pages'][0]['ocr_lines'][0]['text'],'BlurLabel')
    def test_unrelated_edit_keeps_inline_formula_offsets_pinned(self):
        from tests.test_document_native import docx
        from backend import document_native as native
        path=s.UPLOADS/(uuid.uuid4().hex+'.docx')
        ns=native.NS['m'];xml=f'<m:oMath xmlns:m="{ns}"><m:r><m:t>x</m:t></m:r></m:oMath>'
        docx(path,'<w:p><w:r><w:t>Before</w:t></w:r></w:p><w:p>'+xml+'</w:p>')
        d=s.index_document(path);before=documents.layout(d['id']);target='block:'+before['pages'][0]['blocks'][0]['id']
        with patch.object(rag_index,'submit_index',return_value={'id':'job'}):response=self.correct(d,before,target,text='A much longer introduction')
        self.assertEqual(response.status_code,200,response.text)
        page=documents.layout(d['id'])['pages'][0];interval=page['blocks'][1]['formula_ranges'][0]
        self.assertEqual(page['text'][interval['start']:interval['end']],' $x$ ')
    def test_docx_formula_paragraph_review_supersedes_old_observations(self):
        from tests.test_document_native import docx
        from backend import document_native as native
        path=s.UPLOADS/(uuid.uuid4().hex+'.docx')
        ns=native.NS['m']
        docx(path,f'<w:p><m:oMath xmlns:m="{ns}"><m:unsupported><m:e><m:r><m:t>x</m:t></m:r></m:e></m:unsupported></m:oMath></w:p>')
        d=s.index_document(path);before=documents.layout(d['id'])
        target=next(t for t in document_review.targets(before['pages'][0]) if t['kind']=='formula')
        with patch.object(rag_index,'submit_index',return_value={'id':'one-document-job'}):
            response=self.correct(d,before,target['id'],text='(x)')
        self.assertEqual(response.status_code,200,response.text)
        page=documents.layout(d['id'])['pages'][0]
        self.assertEqual(page['formulas'][0]['status'],'superseded')
        self.assertEqual(next(c['value'] for c in page['quality_checks'] if c['name']=='公式结构待复核'),0)
        self.assertEqual(documents.layout(d['id'],d['sha256'],before['parse_id'])['pages'][0]['formulas'][0]['status'],'pending')
    def test_formula_review_confirms_complete_structure_and_keeps_old_parse(self):
        from backend import document_native as native,ocr_structure
        d,_,body=self.source('')
        lines=[{'text':text,'box':box,'confidence':.95,'status':'confirmed'} for text,box in
               [('ψ() =',[.4,.4,.46,.42]),('1',[.47,.39,.48,.41]),('∑',[.5,.4,.54,.42])]]
        parsed={k:copy.deepcopy(v) for k,v in body.items() if k not in {'document','parse_id'}}
        parsed['pages'][0]=native.apply_ocr_page(parsed['pages'][0],{'lines':lines,'formulas':ocr_structure.formulas(lines)})
        with s.conn() as db: saved=documents.save_parse(db,d['id'],d['sha256'],parsed,'公开公式结构夹具')
        pending=documents.layout(d['id']);target=next(t for t in document_review.targets(pending['pages'][0]) if t['kind']=='formula')
        with patch.object(rag_index,'submit_index',return_value={'id':'formula-index'}):
            response=self.correct(d,pending,target['id'],text=r'\psi(A)=\frac{1}{N}\sum_{i=1}^{N}p_i')
        self.assertEqual(response.status_code,200,response.text)
        after=documents.layout(d['id']);formula=after['pages'][0]['formulas'][0]
        self.assertEqual(formula['status'],'confirmed');self.assertTrue(formula['human_reviewed'])
        self.assertFalse(after['pages'][0]['recognition_pending'])
        self.assertEqual(documents.layout(d['id'],d['sha256'],saved['parse_id'])['pages'][0]['formulas'][0]['status'],'pending')
    def test_original_and_historical_quote_survive_correction_and_stale_write(self):
        d,path,before=self.source();old_hash=s.digest(path)
        block=before['pages'][0]['blocks'][0]
        with patch.object(rag_index,'submit_index',return_value={'id':'one-document-job'}) as submit:
            r=self.correct(d,before,'block:'+block['id'],text='限额为200万元。')
        self.assertEqual(r.status_code,200,r.text);after=r.json()
        submit.assert_called_once_with([d['id']])
        self.assertEqual(s.digest(path),old_hash)
        old=documents.resolve({'document_id':d['id'],'sha256':d['sha256'],'parse_id':before['parse_id']},'限额为100万元。')
        new=documents.resolve({'document_id':d['id'],'sha256':d['sha256'],'parse_id':after['parse_id']},'限额为200万元。')
        self.assertEqual(old['status'],'located');self.assertEqual(new['status'],'located')
        self.assertFalse(old['quality']['human_reviewed']);self.assertTrue(new['quality']['human_reviewed'])
        body=documents.layout(d['id']);audit=body['human_reviews'][-1]
        self.assertEqual(audit['before']['text'],'限额为100万元。');self.assertEqual(audit['after']['text'],'限额为200万元。')
        self.assertIn('下一条保持不变。',body['pages'][0]['text'])
        stale=self.correct(d,before,'block:'+block['id'],text='限额为300万元。')
        self.assertEqual(stale.status_code,409)
        self.assertEqual(documents.current_parse_id(d['id'],d['sha256']),after['parse_id'])
    def test_pending_observation_needs_explicit_confirmation_and_index_failure_is_recoverable(self):
        d,_,body=self.source('')
        parsed={k:copy.deepcopy(v) for k,v in body.items() if k not in {'document','parse_id'}}
        parsed['pages'][0]['recognition_pending']=[{'text':'低置信度100','box':[.1,.2,.5,.3]}]
        with s.conn() as db:data=documents.save_parse(db,d['id'],d['sha256'],parsed,'公开低置信度夹具')
        old_id=data['parse_id']
        response=self.correct(d,data,'pending:0',text='原件确认金额为100元。',accepted=False)
        self.assertEqual(response.status_code,422)
        with patch.object(rag_index,'submit_index',side_effect=ValueError('公开索引故障')):
            response=self.correct(d,data,'pending:0',text='原件确认金额为100元。')
        self.assertEqual(response.status_code,200,response.text);self.assertIn('index_error',response.json())
        self.assertNotEqual(response.json()['parse_id'],old_id)
        self.assertEqual(documents.layout(d['id'],parse_id=old_id)['pages'][0]['text'],'')
        page=documents.layout(d['id'])['pages'][0]
        self.assertEqual(page['recognition_pending'],[]);self.assertTrue(page['blocks'][0]['human_reviewed'])
    def test_chart_value_alone_cannot_confirm_association(self):
        d,_,body=self.source('')
        parsed={k:copy.deepcopy(v) for k,v in body.items() if k not in {'document','parse_id'}}
        parsed['pages'][0]['charts']=[{'id':'chart1','status':'pending','title':'检修费用','unit':'元','box':[0,0,1,1],
            'series':[{'name':'费用','name_status':'pending','status':'pending','points':[{'id':'point1','label':'一月','value':None,'status':'pending','value_source':'unknown'}]}]}]
        with s.conn() as db:data=documents.save_parse(db,d['id'],d['sha256'],parsed,'公开图点夹具')
        incomplete=self.correct(d,data,'point:chart1:0:point1',text='0')
        self.assertEqual(incomplete.status_code,409)
        with patch.object(rag_index,'submit_index',return_value={'id':'chart-index'}):
            complete=self.correct(d,data,'point:chart1:0:point1',text='0',label='一月',series_name='费用',unit='元',title='检修费用',x_label='月份',y_label='费用')
        self.assertEqual(complete.status_code,200,complete.text)
        page=documents.layout(d['id'])['pages'][0]
        self.assertEqual(page['charts'][0]['status'],'confirmed');self.assertIn('数值：0',page['text'])
        self.assertTrue(page['blocks'][0]['human_reviewed'])

    def test_blank_and_zero_cells_stay_distinct_and_other_pending_cells_are_not_confirmed(self):
        d,_,body=self.source('')
        parsed={k:copy.deepcopy(v) for k,v in body.items() if k not in {'document','parse_id'}}
        parsed['pages'][0]['tables']=[{'id':'t1','title':'库存','rows':1,'columns':2,'status':'pending',
            'cells':[{'id':'c0','row':0,'column':0,'text':None,'status':'pending'},
                     {'id':'c1','row':0,'column':1,'text':None,'status':'pending'}]}]
        with s.conn() as db:data=documents.save_parse(db,d['id'],d['sha256'],parsed,'公开单元格夹具')
        with patch.object(rag_index,'submit_index',return_value={'id':'cell-index'}):
            first=self.correct(d,data,'cell:t1:c0',text='0')
            self.assertEqual(first.status_code,200,first.text)
            page=documents.layout(d['id'])['pages'][0]
            self.assertEqual(page['tables'][0]['cells'][1]['status'],'pending');self.assertEqual(page['text'],'')
            second=self.correct(d,documents.layout(d['id']),'cell:t1:c1',text='')
            self.assertEqual(second.status_code,200,second.text)
        page=documents.layout(d['id'])['pages'][0]
        self.assertEqual([c['text'] for c in page['tables'][0]['cells']],['0',''])
        self.assertIn('0',page['text'])

    def test_damaged_text_is_excluded_and_correction_restores_confirmed_range(self):
        d,_,body=self.source('金额��100元。')
        block=body['pages'][0]['blocks'][0]
        self.assertEqual(block['status'],'pending')
        self.assertEqual(documents.resolve({'document_id':d['id'],'sha256':d['sha256'],'parse_id':body['parse_id']},'金额��100元。')['status'],'unlocated')
        with patch.object(rag_index,'submit_index',return_value={'id':'repair-index'}):
            response=self.correct(d,body,'block:'+block['id'],text='金额为100元。')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(documents.resolve({'document_id':d['id'],'sha256':d['sha256'],'parse_id':response.json()['parse_id']},'金额为100元。')['status'],'located')


if __name__=='__main__':unittest.main()
