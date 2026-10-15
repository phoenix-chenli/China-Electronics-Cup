import copy
import io
import json
import time
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw
from backend import visual_formula as f, anthropic_vision as a, document_providers as p, document_native as n
def token(value):return {'kind':'token','value':value}
def fraction(top='a',bottom='b'):return {'kind':'fraction','numerator':token(top),'denominator':token(bottom)}
class VisualFormulaTests(unittest.TestCase):
    def test_layout_conflict_marks_unknown_instead_of_guessing_script_direction(self):
        _,body,_,_=self.zoom_fixture();tree=body['formulas'][0]['tree'];original=copy.deepcopy(tree)
        audited,report=f.audit_geometry(tree,{'pairs':[{'observed_position':'raised'}]})
        self.assertEqual(tree,original);self.assertFalse(f.parse(audited)['structure_complete'])
        self.assertEqual(report['conflicts'][0]['candidate']['sub']['value'],'i')
        self.assertNotIn('sup',audited['lower']);self.assertEqual(audited['lower']['kind'],'unknown')
        preserved,report=f.audit_geometry(tree,{'pairs':[{'observed_position':'lowered'}]})
        self.assertEqual(preserved,tree);self.assertEqual(report['conflicts'],[])
    def test_local_geometry_is_position_hint_not_character_certification(self):
        image=Image.new('RGB',(120,140),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((40,10,70,70),fill='black');draw.rectangle((42,95,52,125),fill='black');draw.rectangle((57,82,62,98),fill='black')
        raw=io.BytesIO();image.save(raw,format='PNG');hints=f.geometry_hints(raw.getvalue())
        if not hints['available']:self.skipTest('optional local pixel component dependency absent')
        self.assertTrue(any(pair['observed_position']=='raised' for pair in hints['pairs']))
        self.assertIn('not character identity',hints['scope']);self.assertNotIn('confirmed',str(hints))
    def zoom_fixture(self):
        raw=io.BytesIO();Image.new('RGB',(100,80),'white').save(raw,format='PNG')
        tree={'kind':'large_operator','symbol':'∑','lower':{'kind':'scripts','base':token('y'),'sub':token('i'),'sup':None},'upper':None,'body':token('x')}
        body={'blocks':[],'tables':[],'charts':[],'issues':[],'provenance':{},'formulas':[{'box':[.1,.1,.9,.9],'tree':tree}]}
        corrected=copy.deepcopy(body);corrected['formulas'][0]['tree']['lower'].update(sub=None,sup=token('i'))
        provider={'id':'v','revision':1,'model':'fixture','timeout':30}
        return raw.getvalue(),body,corrected,provider
    def test_zoom_disagreement_keeps_both_candidates_pending_and_records_actual_call(self):
        raw,body,corrected,provider=self.zoom_fixture();result=p._validate_visual(body,[]);events=[]
        response={'text':json.dumps(corrected),'model':'fixture','usage':{'total_tokens':7}}
        with patch.object(p,'_vision_current',return_value=provider),patch.object(p.c,'chat',return_value=response) as chat:
            out=p._reread_visual_formulas(raw,result,provider,None,time.monotonic()+60,events.append)
        self.assertEqual(chat.call_count,1);formula=out['formulas'][0]
        self.assertEqual(formula['status'],'pending');self.assertEqual(formula['zoom_verification'],'same_model_disagreement')
        self.assertNotEqual(formula['latex'],formula['page_candidate']['latex'])
        self.assertEqual([e['status'] for e in events],['running','completed'])
        self.assertEqual(events[-1]['usage']['total_tokens'],7)
        self.assertTrue(all(0<=v<=1 for v in formula['box']))
    def test_zoom_failure_keeps_original_and_records_failure(self):
        raw,body,_,provider=self.zoom_fixture();result=p._validate_visual(body,[]);original=copy.deepcopy(result['formulas'][0]);events=[]
        with patch.object(p,'_vision_current',return_value=provider),patch.object(p.c,'chat',side_effect=ValueError('provider unavailable')):
            out=p._reread_visual_formulas(raw,result,provider,None,time.monotonic()+60,events.append)
        self.assertEqual(out['formulas'][0],original);self.assertEqual(events[-1]['status'],'failed')
        self.assertEqual(out['formula_rereads'][0]['status'],'failed')
    def test_malformed_zoom_cannot_restore_a_locally_conflicting_source_candidate(self):
        raw,body,_,provider=self.zoom_fixture();result=p._validate_visual(body,[])
        hints={'available':True,'pairs':[{'observed_position':'raised'}]}
        response={'text':'```json\n{}\n```','model':'fixture','usage':{}}
        with patch.object(f,'geometry_hints',return_value=hints),patch.object(p,'_vision_current',return_value=provider),patch.object(p.c,'chat',return_value=response) as chat:
            out=p._reread_visual_formulas(raw,result,provider,None,time.monotonic()+60,None)
        formula=out['formulas'][0]
        self.assertFalse(formula['structure_complete']);self.assertEqual(formula['status'],'pending')
        self.assertEqual(formula['page_candidate']['tree'],body['formulas'][0]['tree'])
        self.assertTrue(formula['geometry_audit']['conflicts']);self.assertEqual(out['formula_rereads'][0]['status'],'failed')
        prompt=chat.call_args.args[1][0]['content'][0]['text']
        self.assertIn('no markdown fences',prompt);self.assertIn('never copy them into provenance',prompt)
    def test_zoom_budget_skips_call_and_preserves_pending(self):
        raw,body,_,provider=self.zoom_fixture();result=p._validate_visual(body,[])
        with patch.object(p.c,'chat') as chat:
            out=p._reread_visual_formulas(raw,result,provider,None,time.monotonic()+5,None)
        chat.assert_not_called();self.assertEqual(out['formula_rereads'][0]['status'],'not_run_budget')
    def test_zoom_call_limit_is_two(self):
        raw,body,corrected,provider=self.zoom_fixture();body['formulas']*=3;result=p._validate_visual(body,[])
        response={'text':json.dumps(corrected),'model':'fixture','usage':{}}
        with patch.object(p,'_vision_current',return_value=provider),patch.object(p.c,'chat',return_value=response) as chat:
            out=p._reread_visual_formulas(raw,result,provider,None,time.monotonic()+60,None)
        self.assertEqual(chat.call_count,2);self.assertEqual(len(out['formula_rereads']),2)
    def test_fraction_preserves_denominator(self):
        out=f.parse(fraction());self.assertEqual(out['latex'],r'\frac{a}{b}');self.assertEqual(out['status'],'pending')
    def test_missing_denominator_never_becomes_complete(self):
        tree=fraction();tree['denominator']={'kind':'unknown','reason':'cropped'}
        out=f.parse(tree);self.assertFalse(out['structure_complete']);self.assertIn('denominator',out['structure_audit']['unknown'][0]['path'])
    def test_null_required_operand_keeps_explicit_unknown(self):
        tree=fraction();tree['denominator']=None
        self.assertFalse(f.parse(tree)['structure_complete'])
    def test_explicit_thousand_coordinate_conversion(self):
        body={'blocks':[],'tables':[],'charts':[],'issues':[],'provenance':{},'formulas':[{'box':[100,200,900,800],'coordinate_space':'normalized_1000','tree':fraction()}]}
        out=p._validate_visual(body,[])['formulas'][0];self.assertEqual(out['box'],[.1,.2,.9,.8]);self.assertEqual(out['model_box'],[100,200,900,800])
        body['formulas'][0].pop('coordinate_space')
        with self.assertRaises(ValueError):p._validate_visual(body,[])
    def test_nested_root_scripts_and_sum(self):
        tree={'kind':'large_operator','symbol':'∑','lower':token('i=1'),'upper':token('N'),'body':{'kind':'root','index':None,'body':{'kind':'scripts','base':token('x'),'sub':token('i'),'sup':token('2')}}}
        self.assertEqual(f.parse(tree)['latex'],r'\sum_{i=1}^{N} \sqrt{{x}_{i}^{2}}')
    def test_integral_without_printed_limits(self):
        tree={'kind':'large_operator','symbol':'∫','lower':None,'upper':None,'body':token('f(x)dx')}
        self.assertNotIn('_{',f.parse(tree)['latex'])
    def test_unknown_upper_bound_is_retained(self):
        tree={'kind':'large_operator','symbol':'∑','lower':token('i=1'),'upper':{'kind':'unknown','reason':'blurred'},'body':token('x')}
        self.assertFalse(f.parse(tree)['structure_complete'])
    def test_matrix_shape(self):
        self.assertIn(' & ',f.parse({'kind':'matrix','rows':[[token('1'),token('2')],[token('3'),token('4')]]})['latex'])
        with self.assertRaises(ValueError):f.parse({'kind':'matrix','rows':[[token('1')],[token('2'),token('3')]]})
    def test_injection_and_unexpected_operands_rejected(self):
        for tree in [token(r'\input{file}'),{**fraction(),'extra':token('c')},{'kind':[]}, {'kind':'row','children':[]}]:
            with self.assertRaises(ValueError):f.parse(tree)
    def test_depth_and_nodes_bounded(self):
        tree=token('a')
        for _ in range(22):tree={'kind':'root','body':tree,'index':None}
        with self.assertRaises(ValueError):f.parse(tree)
    def test_old_visual_payload_remains_compatible(self):
        body={'blocks':[],'tables':[],'charts':[],'issues':[],'provenance':{}}
        self.assertEqual(p._validate_visual(body,[])['formulas'],[])
    def test_visual_formula_stays_pending_with_matching_ocr(self):
        body={'blocks':[],'tables':[],'charts':[],'formulas':[{'box':[.1,.1,.9,.9],'tree':fraction()}],'issues':[],'provenance':{}}
        out=p._validate_visual(body,[{'text':'a b','box':[.1,.1,.9,.9],'id':1}]);self.assertEqual(out['formulas'][0]['status'],'pending')
    def test_formula_located_and_reviewable_without_mutating_original(self):
        base=n._page(1);old=copy.deepcopy(base)
        formula=f.parse(fraction());formula['box']=[.1,.1,.9,.9]
        page=n.apply_visual_page(base,{'blocks':[],'tables':[],'charts':[],'formulas':[formula],'issues':[]})
        self.assertEqual(base,old);self.assertEqual(page['blocks'][0]['status'],'pending');self.assertIn('formula_id',page['blocks'][0])
        from backend.document_review import targets
        self.assertEqual(targets(page)[0]['kind'],'formula')
        again=n.apply_visual_page(page,{'blocks':[],'tables':[],'charts':[],'formulas':[formula],'issues':[]})
        self.assertEqual(len(again['formulas']),1)
    def test_human_review_lists_original_and_zoom_formula_candidates(self):
        from backend.document_review import targets
        formula=f.parse(fraction());formula.update(box=[.1,.1,.9,.9],page_candidate={'latex':'original uncertain formula'})
        page=n.apply_visual_page(n._page(1),{'blocks':[],'tables':[],'charts':[],'formulas':[formula],'issues':[]})
        self.assertEqual(targets(page)[0]['ocr_candidates'],['original uncertain formula',formula['latex']])
    def test_underlying_ocr_cannot_bypass_pending_formula(self):
        base=n._page(1);n._append(base,'10','text',[.2,.2,.3,.3],status='confirmed',media_id=None)
        formula=f.parse(fraction());formula['box']=[.1,.1,.9,.9]
        page=n.apply_visual_page(base,{'blocks':[],'tables':[],'charts':[],'formulas':[formula],'issues':[]})
        self.assertTrue(all(b['status']=='pending' for b in page['blocks']))
    def test_letter_only_formula_fragments_are_also_blocked(self):
        base=n._page(1);n._append(base,'a b','text',[.2,.2,.3,.3],status='confirmed',media_id=None)
        formula=f.parse(fraction());formula['box']=[.1,.1,.9,.9]
        page=n.apply_visual_page(base,{'blocks':[],'tables':[],'charts':[],'formulas':[formula],'issues':[]})
        self.assertTrue(all(b['status']=='pending' for b in page['blocks']))
    def test_anthropic_image_bytes_preserved(self):
        payload=a.request([{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,YWJj'}}]}],'m',10)
        self.assertEqual(payload['messages'][0]['content'][0]['source']['data'],'YWJj')
    def test_external_image_url_not_silently_fetched(self):
        with self.assertRaises(ValueError):a.request([{'role':'user','content':[{'type':'image_url','image_url':{'url':'https://example.com/private.png'}}]}],'m',10)
    def test_truncated_response_rejected(self):
        with self.assertRaises(ValueError):a.response({'stop_reason':'max_tokens','content':[{'type':'text','text':'{}'}]},'m')
    def test_anthropic_usage_not_confidence(self):
        out=a.response({'content':[{'type':'text','text':'{}'}],'usage':{'input_tokens':4,'output_tokens':2}},'m')
        self.assertEqual(out['usage']['total_tokens'],6);self.assertNotIn('confidence',out)
