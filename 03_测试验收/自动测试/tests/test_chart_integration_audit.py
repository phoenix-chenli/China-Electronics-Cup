"""Independent integration boundaries for chart signs, units and source gaps."""
import copy
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-chart-audit-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import chart_structure, document_native as native, documents, document_review, rag_grounding as grounding, rag_index as index, rag_retrieval as retrieval, store as s


def visual_chart(value='120', unit='CNY', label='A'):
    chart={'id':'synthetic-chart','title':'Revenue','kind':'bar','box':[0,0,1,1],
           'x_label':'Month','y_label':'Amount','unit':unit,'series':[{'name':'Sales','axis':'primary','points':[
               {'id':'synthetic-point','label':label,'value':value,'printed_text':label+' '+value,'box':[.3,.3,.65,.35],'value_source':'printed'}]}]}
    observed=[{'id':'title','text':'Revenue','box':[.3,.02,.7,.08]},
              {'id':'legend','text':'Sales','box':[.7,.11,.9,.15]},
              {'id':'x-axis','text':'Month','box':[.3,.92,.7,.96]},
              {'id':'y-axis','text':'Amount('+unit+')','box':[.02,.35,.14,.39]},
              {'id':'pair','text':label+' '+value,'box':[.3,.3,.65,.35]}]
    return chart_structure.prepare(chart,observed)


class ChartIntegrationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init();index.initialize()

    @classmethod
    def tearDownClass(cls):
        TEMP.cleanup()

    def source(self,chart):
        path=s.UPLOADS/(uuid.uuid4().hex+'.md');path.write_text('Public synthetic chart.',encoding='utf-8')
        doc=s.index_document(path);before=documents.layout(doc['id'])
        body={k:copy.deepcopy(v) for k,v in before.items() if k not in {'document','parse_id'}}
        page=native._page(1);page['charts']=[copy.deepcopy(chart)]
        native._chart_text(page,page['charts'][0],'visual_observed')
        self.assertTrue(page['blocks'],'The independently specified chart must have a complete point')
        page['text_available']=True;body['pages']=[page]
        with s.conn() as db:documents.save_parse(db,doc['id'],doc['sha256'],body,'independent chart integration fixture')
        return doc,documents.layout(doc['id'])

    def scope(self,doc,data,quote=None):
        context={'document_id':doc['id'],'sha256':doc['sha256'],'parse_id':data['parse_id'],'page':1}
        if quote is not None:
            start=data['pages'][0]['text'].index(quote)
            context.update(text_range={'start':start,'end':start+len(quote)},quote=quote)
        with patch.object(index,'_provider_snapshot',return_value={}):return index.freeze_scope(document_context=context)

    def span(self,doc,data):
        block=data['pages'][0]['blocks'][0]
        return grounding.bind_source({'id':'actual-chart','type':'document','source':'public-chart.png','snippet':block['text'],
            'locator':{'document_id':doc['id'],'sha256':doc['sha256'],'parse_id':data['parse_id'],'page':1,
                       'text_range':{'start':block['start'],'end':block['end']}}})

    def test_selected_number_reports_the_need_for_the_complete_chart_point(self):
        doc,data=self.source(visual_chart())
        scope=self.scope(doc,data,'120');gaps=retrieval._source_gaps(scope)
        point_gap=next((gap for gap in gaps if gap['reason_code']=='source_partial_chart_point'),None)
        self.assertIsNotNone(point_gap)
        self.assertEqual(point_gap['action'],'select_complete_chart_point')
        self.assertEqual(index.chunks_for_scope(scope)[0]['quality']['chart_complete'],False)

    def test_page_scope_oversized_chart_atom_has_a_specific_original_chart_action(self):
        # This saved package cache is not an image-derived observation. Its long
        # category is a literal authored source string for the window boundary.
        chart={'id':'long-chart','method':'docx_chart_cache','source_part':'word/charts/chart1.xml',
               'title':'Public','unit':'CNY','status':'confirmed','series':[{'name':'Sales','points':[
                   {'id':'long-point','label':'L'*(index.CHUNK_TOKENS+100),'value':'120','status':'confirmed',
                    'value_source':'native_cache','source_part':'word/charts/chart1.xml/series[0]/point[0]'}]}]}
        doc,data=self.source(chart)
        gaps=retrieval._source_gaps(self.scope(doc,data))
        gap=next((gap for gap in gaps if gap['reason_code']=='source_chart_point_too_long'),None)
        self.assertIsNotNone(gap)
        self.assertEqual(gap['action'],'inspect_original_chart')

    def test_corpus_oversized_chart_atom_keeps_a_chart_specific_action(self):
        doc,data=self.source(visual_chart())
        with patch.object(index,'_counter',return_value=(len,'characters',25)):
            generation=index._build_document(doc['id'])
        scope={'scope_type':'corpus','documents':[{key:generation[key] for key in ('document_id','sha256','parse_id','generation_id')}]}
        gap=next(g for g in retrieval._source_gaps(scope) if g['reason_code']=='source_chart_point_too_long')
        self.assertEqual(gap['action'],'inspect_original_chart')

    def test_unicode_minus_is_a_negative_value_in_the_answer_number_guard(self):
        doc,data=self.source(visual_chart('−12'));span=self.span(doc,data)
        result=grounding.synthesis({'text':'A 的净额为 -12 CNY。','support_span_ids':[span['id']]},{span['id']:span},{span['id']})
        self.assertEqual(result['text'],'A 的净额为 -12 CNY。')

    def test_unicode_negative_value_does_not_silently_support_a_positive_value(self):
        doc,data=self.source(visual_chart('−12'));span=self.span(doc,data)
        with self.assertRaisesRegex(ValueError,'原文未提供'):
            grounding.synthesis({'text':'A 的净额为 12 CNY。','support_span_ids':[span['id']]},{span['id']:span},{span['id']})

    def test_exponent_digit_is_not_an_independent_observed_measurement(self):
        doc,data=self.source(visual_chart('1e3'));span=self.span(doc,data)
        with self.assertRaisesRegex(ValueError,'原文未提供'):
            grounding.synthesis({'text':'A 的净额为 3 CNY。','support_span_ids':[span['id']]},{span['id']:span},{span['id']})

    def test_percent_suffix_from_complete_authoritative_chart_unit_is_not_lost(self):
        doc,data=self.source(visual_chart('12.5','%'));span=self.span(doc,data)
        self.assertEqual(span['quality']['chart_point_bindings'][0]['unit_text'],'%')
        result=grounding.synthesis({'text':'A 的比例为 12.5%。','support_span_ids':[span['id']]},{span['id']:span},{span['id']})
        self.assertEqual(result['text'],'A 的比例为 12.5%。')

    def test_caller_quality_cannot_turn_a_currency_point_into_a_percentage(self):
        doc,data=self.source(visual_chart('12.5','CNY'));span=self.span(doc,data)
        span['quality']['chart_point_bindings'][0]['unit_text']='%'
        with self.assertRaisesRegex(ValueError,'原文未提供'):
            grounding.synthesis({'text':'A 的比例为 12.5%。','support_span_ids':[span['id']]},{span['id']:span},{span['id']})

    def test_two_embedded_images_get_distinct_final_chart_point_identities(self):
        chart=visual_chart();chart.pop('id');chart['series'][0]['points'][0].pop('id')
        result={'blocks':[],'tables':[],'charts':[chart],'issues':[],'provenance':{}}
        page=native.apply_visual_page(native._page(1),result,media_id='public-one')
        page=native.apply_visual_page(page,result,media_id='public-two')
        blocks=[b for b in page['blocks'] if b.get('chart_id')]
        self.assertEqual(len(blocks),2)
        self.assertEqual(len({b['chart_id'] for b in blocks}),2)
        self.assertEqual(len({b['point_id'] for b in blocks}),2)
        for block in blocks:
            self.assertEqual(block['chart_point_binding']['chart_id'],block['chart_id'])
            self.assertEqual(block['chart_point_binding']['point_id'],block['point_id'])

    def test_chart_covered_text_has_no_scalar_human_review_bypass(self):
        page=native._page(1);page['charts']=[visual_chart()]
        native._append(page,'A 120',box=[.3,.3,.65,.35],source_method='human_review',status='confirmed',human_reviewed=True)
        page['recognition_pending']=[{'text':'A 12O','box':[.3,.3,.65,.35]}]
        native._protect_chart_regions(page)
        targets=document_review.targets(page)
        self.assertFalse(any(t['kind']=='text' for t in targets))
        self.assertTrue(any(t['kind']=='chart_point' for t in targets))


if __name__=='__main__':unittest.main()
