"""Chart relation atoms, authoritative bindings and immutable human corrections."""
import copy
import hashlib
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-chart-evidence-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import document_native as native, documents, document_review as review, rag_index as index, rag_grounding as grounding, store as s


def saved_chart(labels=('A', 'B')):
    return {'id':'chart1','status':'confirmed','title':'Maintenance','unit':'CNY',
            'x_label':'Month','y_label':'Cost','box':[.1,.1,.9,.9],'method':'docx_chart_cache',
            'source_part':'word/charts/chart1.xml','series':[{'name':'Expense','name_status':'confirmed','axis':'primary',
            'points':[{'id':'point'+str(i),'label':label,'value':str(120+i*10),'printed_text':str(120+i*10),
                       'status':'confirmed','value_source':'native_cache','source_part':f'word/charts/chart1.xml/series[0]/point[{i}]'} for i,label in enumerate(labels)]}]}


class ChartEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
        index.initialize()
        from backend import document_tasks
        document_tasks.initialize()

    @classmethod
    def tearDownClass(cls):
        TEMP.cleanup()

    def source(self, chart=None):
        path=s.UPLOADS/(uuid.uuid4().hex+'.md')
        path.write_text('Synthetic chart source.',encoding='utf-8')
        doc=s.index_document(path)
        original=documents.layout(doc['id'])
        body={k:copy.deepcopy(v) for k,v in original.items() if k not in {'document','parse_id'}}
        page=native._page(1)
        chart=copy.deepcopy(chart or saved_chart())
        page['charts']=[chart]
        native._chart_text(page,chart,'docx_chart_cache')
        page['text_available']=True
        body['pages']=[page]
        with s.conn() as db:documents.save_parse(db,doc['id'],doc['sha256'],body,'synthetic chart relation fixture')
        return doc,documents.layout(doc['id'])

    def locator(self,doc,data,start=None,end=None):
        locator={'document_id':doc['id'],'sha256':doc['sha256'],'parse_id':data['parse_id'],'page':1}
        if start is not None:locator['text_range']={'start':start,'end':end}
        return locator

    def test_point_is_atomic_and_partial_numeric_selection_is_rejected(self):
        doc,data=self.source()
        page=data['pages'][0]
        block=page['blocks'][0]
        self.assertGreater(len(block['text']),30)
        spans=list(index._source_spans(page,len,1000))
        self.assertIn((block['start'],block['end'],False),spans)
        start=page['text'].index('120')
        located=documents.resolve(self.locator(doc,data,start,start+3),'120')
        self.assertEqual(located['status'],'located')
        self.assertFalse(located['quality']['chart_complete'])
        self.assertEqual(located['quality']['chart_point_bindings'],[])
        with self.assertRaisesRegex(ValueError,'完整图点'):
            grounding.bind_source({'id':'fake','type':'document','source':'Synthetic','snippet':'120','locator':self.locator(doc,data,start,start+3),'quality':{'chart_complete':True}})

    def test_full_binding_regenerated_from_authoritative_point_and_passed_to_model(self):
        doc,data=self.source()
        block=data['pages'][0]['blocks'][0]
        source={'id':'evidence','type':'document','source':'Synthetic','snippet':block['text'],
                'locator':self.locator(doc,data,block['start'],block['end']),
                'quality':{'chart_point_bindings':[{'chart_id':'forged','unit':'USD'}]}}
        span=grounding.bind_source(source)
        bindings=grounding.packet([span])['spans'][0]['extraction']['chart_point_bindings']
        self.assertEqual(len(bindings),1)
        self.assertEqual(bindings[0]['chart_id'],'chart1')
        self.assertEqual(bindings[0]['point_id'],'point0')
        self.assertNotIn('forged',str(bindings))

    def test_oversized_atom_omitted_with_explicit_gap(self):
        chart=saved_chart(('LongCategory'+'x'*500,))
        doc,data=self.source(chart)
        page=data['pages'][0]
        self.assertFalse(list(index._source_spans(page,len,120)))
        with patch.object(index,'_counter',return_value=(len,'characters',120)):
            generation=index._build_document(doc['id'])
        self.assertIn('source_chart_point_too_long',{m.get('reason_code') for m in generation['missing_pages']})

    def test_unbound_legacy_chart_cannot_assert_complete_relation(self):
        doc,data=self.source()
        body={k:copy.deepcopy(v) for k,v in data.items() if k not in {'document','parse_id'}}
        body['pages'][0]['charts']=[]
        with s.conn() as db:documents.save_parse(db,doc['id'],doc['sha256'],body,'legacy missing chart')
        latest=documents.layout(doc['id'])
        block=latest['pages'][0]['blocks'][0]
        result=documents.resolve(self.locator(doc,latest,block['start'],block['end']),block['text'])
        self.assertFalse(result['quality']['chart_complete'])

    def test_raw_ocr_inside_confirmed_chart_or_crossing_boundary_stays_pending(self):
        page=native._page(1)
        page['charts']=[saved_chart()]
        for text,box in [('120',[.2,.2,.3,.3]),('A',[.15,.8,.2,.86]),('Label 999',[0,.4,.2,.45]),('Outside',[.92,.92,.99,.99])]:
            native._append(page,text,box=box,status='confirmed',source_method='ocr')
        native._protect_chart_regions(page)
        self.assertEqual([b['status'] for b in page['blocks']],['pending','pending','pending','confirmed'])
        self.assertEqual([t['text'] for t in review.targets(page) if t['kind']=='text'],['Outside'])

    def test_pending_ocr_in_chart_cannot_bypass_point_review(self):
        page=native._page(1)
        page['charts']=[saved_chart()]
        page['recognition_pending']=[{'text':'12O','box':[.2,.2,.3,.3]}, {'text':'Unrelated','box':[.92,.92,.99,.99]}]
        ids={t['id'] for t in review.targets(page)}
        self.assertNotIn('pending:0',ids)
        self.assertIn('pending:1',ids)

    def correct(self,doc,data,point='point0',**changes):
        values={'version':doc['sha256'],'parse_id':data['parse_id'],'page':1,'target':'point:chart1:0:'+point,
                'operator':'synthetic reviewer','accepted':True,'text':'120','label':'A','series_name':'Expense',
                'unit':'CNY','title':'Maintenance','x_label':'Month','y_label':'Cost',**changes}
        with patch.object(index,'submit_index',return_value={'id':'fixture-index'}):
            return review.publish(doc['id'],review.Correction(**values))

    def test_metadata_change_invalidates_other_points_without_rewriting_old_parse(self):
        doc,before=self.source()
        self.correct(doc,before)
        first=documents.layout(doc['id'])
        self.correct(doc,first,point='point1',text='130',label='B',unit='10k CNY')
        latest=documents.layout(doc['id'])
        points=latest['pages'][0]['charts'][0]['series'][0]['points']
        self.assertEqual([p['status'] for p in points],['pending','confirmed'])
        self.assertFalse(points[0]['human_reviewed'])
        self.assertEqual(latest['human_reviews'][-1]['invalidated_point_ids'],['point0'])
        self.assertEqual(len(latest['pages'][0]['blocks']),1)
        self.assertIn('130',latest['pages'][0]['blocks'][0]['text'])
        self.assertNotIn('120',latest['pages'][0]['text'])
        old=documents.layout(doc['id'],doc['sha256'],first['parse_id'])
        self.assertEqual(old['pages'][0]['charts'][0]['unit'],'CNY')
        self.assertEqual(old['pages'][0]['charts'][0]['series'][0]['points'][0]['status'],'confirmed')

    def test_same_metadata_preserves_other_confirmed_point_and_fresh_values(self):
        doc,before=self.source()
        self.correct(doc,before,point='point1',text='132',label='B')
        latest=documents.layout(doc['id'])
        self.assertEqual(latest['human_reviews'][-1]['invalidated_point_ids'],[])
        self.assertEqual(len(latest['pages'][0]['blocks']),2)
        self.assertIn('132',latest['pages'][0]['text'])

    def test_pie_and_donut_formal_human_review_need_no_fictional_axes(self):
        from backend import chart_structure
        for kind in ('pie','donut','pieChart','doughnutChart'):
            with self.subTest(kind=kind):
                chart=saved_chart(('A',));chart.update(kind=kind,x_label='',y_label='')
                doc,before=self.source(chart)
                target=next(t for t in review.targets(before['pages'][0]) if t['kind']=='chart_point')
                self.assertEqual(target['chart_kind'],kind)
                self.correct(doc,before,text='0',unit='%',x_label='',y_label='')
                latest=documents.layout(doc['id']);result=latest['pages'][0]['charts'][0]
                self.assertEqual((result['x_label'],result['y_label']),('',''))
                self.assertEqual(result['relation_status'],'consistent')
                views=list(chart_structure.point_views(result))
                self.assertEqual(len(views),1)
                self.assertEqual(views[0]['source_binding']['value_source'],'human_review')
                self.assertEqual((views[0]['source_binding']['x_label_text'],views[0]['source_binding']['y_label_text']),('',''))
                self.assertEqual((views[0]['source_binding']['value'],views[0]['source_binding']['unit_text']),('0','%'))
                block=latest['pages'][0]['blocks'][0]
                bound=documents.resolve(self.locator(doc,latest,block['start'],block['end']),block['text'])
                self.assertTrue(bound['quality']['chart_complete'])
                self.assertEqual(bound['quality']['chart_point_bindings'][0]['point_id'],'point0')
                self.assertEqual(documents.layout(doc['id'],parse_id=before['parse_id'])['pages'][0]['charts'][0]['series'][0]['points'][0]['value_source'],'native_cache')

    def test_pie_and_donut_formal_review_reject_invented_axis_labels(self):
        for kind in ('pie','donut','pieChart','doughnutChart'):
            chart=saved_chart(('A',));chart.update(kind=kind,x_label='',y_label='')
            doc,before=self.source(chart)
            for x_label,y_label in (('Invented horizontal',''),('','Invented vertical'),('Invented horizontal','Invented vertical')):
                with self.subTest(kind=kind,x_label=x_label,y_label=y_label):
                    with self.assertRaisesRegex(ValueError,'没有横纵轴'):
                        self.correct(doc,before,x_label=x_label,y_label=y_label)
                    self.assertEqual(documents.current_parse_id(doc['id'],doc['sha256']),before['parse_id'])

    def test_fallback_label_candidates_are_scoped_to_the_same_observation_and_media(self):
        page=native._page(1)
        first=native._append(page,'Blurry original',status='pending',media_id='public-one',observation_id=0)
        second=native._append(page,'Other image original',status='pending',media_id='public-two',observation_id=0)
        no_identity=native._append(page,'Unidentified original',status='confirmed',media_id='public-one')
        page['recognition_pending']=[{'text':'Pending original','media_id':'public-one','observation_id':1}]
        page['label_reviews']=[
            {'media_id':'public-one','observation_id':0,'candidates':['Candidate A','Candidate A','',' ',None,7,'Candidate B']},
            {'media_id':'public-two','observation_id':0,'candidates':['Other image candidate']},
            {'media_id':'public-one','observation_id':1,'candidates':['Pending candidate','Pending candidate']},
            {'media_id':'public-one','observation_id':2,'candidates':['Unrelated observation candidate']}]
        before=copy.deepcopy(page);targets={item['id']:item for item in review.targets(page)}
        self.assertEqual(targets['block:'+first['id']]['ocr_candidates'],['Candidate A','Candidate B'])
        self.assertEqual(targets['block:'+second['id']]['ocr_candidates'],['Other image candidate'])
        self.assertEqual(targets['block:'+no_identity['id']]['ocr_candidates'],[])
        self.assertEqual(targets['pending:0']['ocr_candidates'],['Pending candidate'])
        self.assertEqual(targets['block:'+first['id']]['text'],'Blurry original')
        self.assertEqual(targets['block:'+first['id']]['status'],'pending')
        self.assertEqual(targets['pending:0']['text'],'Pending original')
        self.assertEqual(targets['pending:0']['status'],'pending')
        self.assertEqual(page,before)

    def test_fallback_pending_correction_updates_only_its_media_observation_and_keeps_history(self):
        doc,data=self.source()
        body={k:copy.deepcopy(v) for k,v in data.items() if k not in {'document','parse_id'}}
        page=native._page(1)
        lines=[{'text':'Blurry one','media_id':'public-one','observation_id':0,'box':[.1,.2,.4,.3],
                'status':'pending','confidence':.65,'source_pending_reason':'label_crop_conflict'},
               {'text':'Different image','media_id':'public-two','observation_id':0,'box':[.1,.2,.4,.3],
                'status':'pending','confidence':.62,'source_pending_reason':'label_crop_conflict'}]
        page['ocr_lines']=copy.deepcopy(lines);page['recognition_pending']=copy.deepcopy(lines)
        body['pages']=[page]
        with s.conn() as db:documents.save_parse(db,doc['id'],doc['sha256'],body,'unstructured fallback label fixture')
        before=documents.layout(doc['id']);old_page=copy.deepcopy(before['pages'][0])
        request={'version':doc['sha256'],'parse_id':before['parse_id'],'page':1,'target':'pending:0',
                 'operator':'synthetic reviewer','accepted':True,'text':'Clear one'}
        with patch.object(index,'submit_index',return_value={'id':'fixture-index'}):
            review.publish(doc['id'],review.Correction(**request))
        first=documents.layout(doc['id']);new_page=first['pages'][0]
        one,two=new_page['ocr_lines']
        self.assertEqual((one['text'],one['before_human_text'],one['status']),('Clear one','Blurry one','confirmed'))
        self.assertTrue(one['human_reviewed']);self.assertNotIn('source_pending_reason',one)
        self.assertEqual(two,lines[1])
        self.assertEqual(new_page['recognition_pending'],[lines[1]])
        block=next(b for b in new_page['blocks'] if b['text']=='Clear one')
        self.assertEqual((block['media_id'],block['observation_id']),('public-one',0))
        self.assertEqual(documents.layout(doc['id'],parse_id=before['parse_id'])['pages'][0],old_page)
        # A second correction through the newly created block must keep the
        # same observation identity, rather than leaving a stale OCR label.
        request.update(parse_id=first['parse_id'],target='block:'+block['id'],text='Clearer one')
        with patch.object(index,'submit_index',return_value={'id':'fixture-index'}):
            review.publish(doc['id'],review.Correction(**request))
        final=documents.layout(doc['id'])['pages'][0]
        self.assertEqual((final['ocr_lines'][0]['text'],final['ocr_lines'][0]['before_human_text']),('Clearer one','Clear one'))
        self.assertEqual(final['ocr_lines'][1],lines[1])
        self.assertEqual(documents.layout(doc['id'],parse_id=first['parse_id'])['pages'][0]['ocr_lines'][0]['text'],'Clear one')

    def test_table_linked_pending_and_raw_text_have_only_the_cell_review_entry(self):
        page=native._page(1)
        raw=native._append(page,'Blurry table label',status='pending',media_id='public-one',observation_id=0)
        page['recognition_pending']=[
            {'text':'Blurry table label','media_id':'public-one','observation_id':0},
            {'text':'Other image label','media_id':'public-two','observation_id':0},
            {'text':'Independent label','media_id':'public-one','observation_id':1}]
        page['tables']=[{'id':'source-table','title':'Public table','media_id':'public-one','rows':1,'columns':1,
                         'status':'pending','relation_status':'pending','cells':[
                             {'id':'source-cell','row':0,'column':0,'text':'Blurry table label','status':'pending',
                              'evidence_lines':[0],'label_review':'pending'}]}]
        before=copy.deepcopy(page);targets=review.targets(page);ids={item['id'] for item in targets}
        self.assertEqual([item['id'] for item in targets if item['kind']=='cell'],['cell:source-table:source-cell'])
        self.assertNotIn('block:'+raw['id'],ids)
        self.assertNotIn('pending:0',ids)
        self.assertIn('pending:1',ids)
        self.assertIn('pending:2',ids)
        self.assertEqual(page,before)


if __name__=='__main__':unittest.main()
