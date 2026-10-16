"""Source-bound crop-contract goldens; fixtures do not measure OCR accuracy."""
import hashlib
import io
import unittest

from PIL import Image
from backend import ocr_structure as spatial


class LabelQualityTest(unittest.TestCase):
    def fixture(self,text='Revenue',confidence=.65,box=None):
        raw=io.BytesIO();Image.new('RGB',(1000,400),'white').save(raw,format='PNG')
        line={'text':text,'confidence':confidence,'status':'pending','box':box or [.2,.2,.8,.6]}
        result={'lines':[line],'tables':[{'id':'t','status':'pending','cells':[
            {'id':'h','role':'row_header','text':text,'status':'pending','evidence_lines':[0]}]}]}
        return raw.getvalue(),result

    def provider(self,text='Revenue',box=None,confidence=.95,extra=None,bad_hash=False):
        def recognize(raw,**kwargs):
            lines=[] if text is None else [{'text':text,'box':box or [.02,.02,.98,.98],
                'confidence':confidence,'status':'confirmed'}]
            lines.extend(extra or [])
            return {'lines':lines,'duration_ms':3,'input_image_sha256':'0'*64 if bad_hash else hashlib.sha256(raw).hexdigest()}
        return recognize

    def review(self,result):return result['label_reviews'][0]
    def line(self,result):return result['lines'][0]
    def cell(self,result):return result['tables'][0]['cells'][0]

    def test_low_score_unchanged_label_can_be_observed_consistent(self):
        raw,result=self.fixture()
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(self.line(result)['status'],'confirmed')
        self.assertEqual(self.cell(result)['text'],'Revenue')
        self.assertEqual(self.line(result)['original_confidence'],.65)
        self.assertEqual(self.review(result)['quality_status'],'observed_consistent')
        self.assertEqual(self.line(result)['label_quality'],'uncalibrated_same_engine_crop_consistency')
        self.assertFalse(self.line(result)['human_reviewed'])
        self.assertFalse(self.review(result)['independent_verification'])

    def test_same_engine_changed_glyphs_are_candidates_not_a_correction(self):
        raw,result=self.fixture(text='Revenve')
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(self.line(result)['text'],'Revenve')
        self.assertEqual(self.line(result)['status'],'pending')
        self.assertEqual(self.review(result)['candidates'],['Revenue','Revenue'])
        self.assertEqual(self.review(result)['consistent_candidate'],'Revenue')
        self.assertEqual(self.line(result)['source_pending_reason'],'label_reread_source_conflict')

    def test_unit_case_sign_and_number_changes_never_become_equal(self):
        for original,candidate in [('Q1','Q7'),('mW','MW'),('Cost-1','Cost+1'),('Margin (%)','Margin')]:
            with self.subTest(original=original):
                raw,result=self.fixture(text=original)
                spatial.reread_labels(raw,result,self.provider(candidate))
                self.assertEqual(self.line(result)['status'],'pending')
                self.assertEqual(self.cell(result)['text'],original)

    def test_whitespace_only_repetition_preserves_source_label(self):
        raw,result=self.fixture(text='Gross  Margin')
        spatial.reread_labels(raw,result,self.provider('Gross Margin'))
        self.assertEqual(self.line(result)['status'],'confirmed')
        self.assertEqual(self.line(result)['text'],'Gross  Margin')

    def test_tiny_centre_fragment_cannot_certify_the_full_label(self):
        raw,result=self.fixture()
        spatial.reread_labels(raw,result,self.provider(box=[.4,.02,.6,.98]))
        self.assertEqual(self.line(result)['status'],'pending')
        reasons=[d['reason'] for d in self.review(result)['calls'][0]['candidate_decisions']]
        self.assertIn('incomplete_or_neighbour_region',reasons)

    def test_large_neighbour_box_cannot_cover_a_small_label_and_pass(self):
        raw,result=self.fixture(box=[.5,.4,.51,.42])
        spatial.reread_labels(raw,result,self.provider(box=[.01,.01,.99,.99]))
        self.assertEqual(self.line(result)['status'],'pending')
        self.assertEqual(self.review(result)['candidates'],[None,None])

    def test_low_score_extra_fragment_makes_high_score_candidate_ambiguous(self):
        raw,result=self.fixture()
        extra=[{'text':'7','box':[.1,.1,.35,.9],'confidence':.4,'status':'pending'}]
        spatial.reread_labels(raw,result,self.provider(extra=extra))
        self.assertEqual(self.line(result)['status'],'pending')
        self.assertEqual(self.review(result)['calls'][0]['decision_reason'],'fragmented_or_overlapping_observations')

    def test_candidate_is_bound_to_the_actual_crop_hash(self):
        raw,result=self.fixture()
        spatial.reread_labels(raw,result,self.provider(bad_hash=True))
        self.assertEqual(self.line(result)['status'],'pending')
        call=self.review(result)['calls'][0]
        self.assertEqual(call['decision_reason'],'crop_image_hash_mismatch')
        self.assertNotEqual(call['input_image_sha256'],call['provider_input_image_sha256'])

    def test_candidate_crop_box_is_mapped_back_to_original_page(self):
        raw,result=self.fixture()
        spatial.reread_labels(raw,result,self.provider())
        review=self.review(result);candidate=review['candidate_observations'][0]
        rect=review['page_pixel_rect'];box=candidate['crop_box']
        self.assertEqual(candidate['coordinate_space'],'original_image')
        self.assertAlmostEqual(candidate['box'][0],(rect[0]+box[0]*(rect[2]-rect[0]))/1000)
        self.assertAlmostEqual(candidate['box'][3],(rect[1]+box[3]*(rect[3]-rect[1]))/400)

    def test_invalid_source_region_is_pending_without_call(self):
        for box in [[.2,.2,.2,.4],[.8,.2,.2,.4],[float('nan'),.2,.8,.4],[True,.2,.8,.4]]:
            with self.subTest(box=box):
                raw,result=self.fixture(box=box)
                def never(*args,**kwargs):self.fail('Invalid source box must not call OCR')
                spatial.reread_labels(raw,result,never)
                self.assertEqual(self.cell(result)['status'],'pending')
                self.assertEqual(self.review(result)['reason'],'invalid_source_region')

    def test_invalid_or_nonfinite_candidate_score_does_not_pass(self):
        for score in [float('nan'),float('inf'),True,1.2,None]:
            with self.subTest(score=score):
                raw,result=self.fixture()
                spatial.reread_labels(raw,result,self.provider(confidence=score))
                self.assertEqual(self.line(result)['status'],'pending')

    def test_geometry_or_structure_pending_is_not_waived_by_text_agreement(self):
        raw,result=self.fixture()
        self.line(result)['source_pending_reason']='logical_grid_geometry_conflict'
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(self.line(result)['status'],'pending')
        self.assertEqual(self.review(result)['reason'],'label_source_requires_review')

    def test_multi_observation_cell_does_not_lose_other_fragments(self):
        raw,result=self.fixture()
        result['lines'].append({'text':'2025','confidence':.99,'status':'pending','box':[.2,.65,.8,.8]})
        self.cell(result).update(text='Revenue\n2025',evidence_lines=[0,1])
        replies=iter(['Revenue','Revenue',None,None])
        def recognize(raw,**kwargs):return self.provider(next(replies))(raw,**kwargs)
        spatial.reread_labels(raw,result,recognize)
        self.assertEqual(self.line(result)['status'],'confirmed')
        self.assertEqual(self.cell(result)['text'],'Revenue\n2025')
        self.assertEqual(self.cell(result)['status'],'pending')

    def test_multi_observation_cell_with_unknown_source_index_stays_pending(self):
        raw,result=self.fixture();self.cell(result)['evidence_lines']=[0,99]
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(self.line(result)['status'],'confirmed')
        self.assertEqual(self.cell(result)['status'],'pending')

    def test_booleans_are_not_observation_indices(self):
        raw,result=self.fixture();self.cell(result)['evidence_lines']=[False]
        self.line(result).update(status='confirmed',confidence=.99)
        def never(*args,**kwargs):self.fail('Boolean is not an observation index')
        spatial.reread_labels(raw,result,never)
        self.assertNotIn('label_reviews',result)

    def test_complex_typography_and_unreadable_controls_remain_pending(self):
        for text in ['Area²','x₁','Result↑','A\u200bB','Reve�ue']:
            with self.subTest(text=text):
                raw,result=self.fixture(text=text)
                spatial.reread_labels(raw,result,self.provider(text))
                self.assertEqual(self.line(result)['status'],'pending')

    def test_pending_high_score_source_is_not_silently_skipped(self):
        raw,result=self.fixture(confidence=.99)
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(len(self.review(result)['calls']),2)
        self.assertEqual(self.line(result)['status'],'confirmed')

    def test_manual_confirmation_is_preserved_without_crop_calls(self):
        raw,result=self.fixture();self.line(result).update(human_reviewed=True,status='confirmed')
        def never(*args,**kwargs):self.fail('Do not override a human correction')
        spatial.reread_labels(raw,result,never)
        self.assertTrue(self.line(result)['human_reviewed'])

    def test_failures_record_each_actual_call_and_preserve_original(self):
        raw,result=self.fixture()
        def fail(raw,**kwargs):raise ValueError('fixture OCR failure')
        spatial.reread_labels(raw,result,fail)
        self.assertEqual(len(self.review(result)['calls']),2)
        self.assertTrue(all(call['status']=='failed' and call['page_pixel_rect'] for call in self.review(result)['calls']))
        self.assertTrue(all(type(call['duration_ms']) is int for call in self.review(result)['calls']))
        self.assertEqual(self.cell(result)['status'],'pending')

    def test_non_table_uncertain_short_label_is_reread(self):
        raw,result=self.fixture();result.pop('tables')
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(self.line(result)['status'],'confirmed')
        self.assertEqual(len(self.review(result)['calls']),2)

    def test_non_table_numbers_formulas_and_prose_do_not_use_crop_budget(self):
        for text in ['123.45%','2025/01','(10)','-45.5','x = 4','∑ x','This is a sentence.','long text '*20]:
            with self.subTest(text=text):
                raw,result=self.fixture(text=text);result.pop('tables')
                def never(*args,**kwargs):self.fail('Not a short uncertain label')
                spatial.reread_labels(raw,result,never)
                self.assertNotIn('label_reviews',result)

    def test_known_formula_observation_cannot_be_recovered_as_a_label(self):
        raw,result=self.fixture(text='x1');result.pop('tables')
        result['formulas']=[{'evidence_lines':[0]}]
        spatial.reread_labels(raw,result,self.provider('x1'))
        self.assertNotIn('label_reviews',result)

    def test_known_compact_identifier_label_is_checked_despite_high_score(self):
        raw,result=self.fixture(text='11/00',confidence=.98);self.line(result)['status']='confirmed'
        spatial.reread_labels(raw,result,self.provider('I1/O0'))
        self.assertEqual(self.line(result)['text'],'11/00')
        self.assertEqual(self.line(result)['status'],'pending')
        self.assertEqual(self.review(result)['consistent_candidate'],'I1/O0')

    def test_correlated_confusable_glyph_identity_is_never_certified_by_repeat(self):
        for text in ['11/00','I1/O0','01/01','OO','1']:
            with self.subTest(text=text):
                raw,result=self.fixture(text=text,confidence=.99);self.line(result)['status']='confirmed'
                spatial.reread_labels(raw,result,self.provider(text))
                self.assertEqual(self.line(result)['text'],text)
                self.assertEqual(self.cell(result)['status'],'pending')
                self.assertEqual(self.review(result)['reason'],'label_glyph_identity_requires_independent_confirmation')

    def test_regular_year_label_is_not_a_confusable_only_identifier(self):
        raw,result=self.fixture(text='2025',confidence=.99);self.line(result)['status']='confirmed'
        spatial.reread_labels(raw,result,self.provider('2025'))
        self.assertEqual(self.cell(result)['status'],'confirmed')

    def test_glyph_identity_gate_does_not_block_numeric_data_values(self):
        raw,result=self.fixture(text='11/00',confidence=.99)
        self.line(result)['status']='confirmed';self.cell(result).update(role='data',status='confirmed')
        def never(*args,**kwargs):self.fail('Data value is not a label role')
        spatial.reread_labels(raw,result,never)
        self.assertEqual(self.line(result)['status'],'confirmed')

    def test_deferred_ambiguous_label_does_not_bypass_the_crop_budget(self):
        raw,result=self.fixture()
        result['lines']=[dict(self.line(result),confidence=.1) for _ in range(6)]+[
            dict(self.line(result),text='11/00',confidence=.99,status='confirmed')]
        result['tables'][0]['cells']=[dict(self.cell(result),id=str(i),text=line['text'],evidence_lines=[i])
                                    for i,line in enumerate(result['lines'])]
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(len(result['label_reviews']),6)
        self.assertEqual(result['lines'][6]['status'],'pending')
        self.assertEqual(result['tables'][0]['cells'][6]['status'],'pending')

    def test_historical_changed_text_is_not_certified_against_another_original(self):
        raw,result=self.fixture(text='CurrentLabel')
        self.line(result).update(original_text='OlderLabel')
        spatial.reread_labels(raw,result,self.provider('OlderLabel'))
        self.assertEqual(self.line(result)['text'],'CurrentLabel')
        self.assertEqual(self.line(result)['status'],'pending')

    def test_no_table_budget_is_also_bounded(self):
        raw,result=self.fixture();result.pop('tables')
        result['lines']=[dict(self.line(result)) for _ in range(8)]
        calls=[]
        def recognize(raw,**kwargs):calls.append(raw);return self.provider()(raw,**kwargs)
        spatial.reread_labels(raw,result,recognize)
        self.assertEqual(len(calls),12)
        self.assertEqual(len(result['label_reviews']),6)

    def test_table_row_label_has_priority_over_lower_score_other_text(self):
        raw,result=self.fixture()
        result['lines']=[dict(self.line(result),confidence=.1) for _ in range(7)]+[self.line(result)]
        self.cell(result)['evidence_lines']=[7]
        spatial.reread_labels(raw,result,self.provider())
        self.assertEqual(result['label_reviews'][0]['observation_id'],7)
        self.assertEqual(len(result['label_reviews']),6)

    def test_cancel_and_timeout_preserve_actual_attempt_in_result(self):
        for error,status in [(InterruptedError('cancelled'),'cancelled'),(TimeoutError('deadline'),'timeout')]:
            with self.subTest(status=status):
                raw,result=self.fixture()
                def stop(raw,**kwargs):raise error
                with self.assertRaises(type(error)):spatial.reread_labels(raw,result,stop)
                self.assertEqual(self.review(result)['calls'][0]['status'],status)
                self.assertGreaterEqual(self.review(result)['calls'][0]['duration_ms'],0)

    def test_six_labels_limit_twelve_crop_calls(self):
        raw,result=self.fixture()
        for index in range(1,8):
            result['lines'].append(dict(result['lines'][0]))
            result['tables'][0]['cells'].append(dict(self.cell(result),id=str(index),evidence_lines=[index]))
        calls=[]
        def recognize(raw,**kwargs):calls.append(raw);return self.provider()(raw,**kwargs)
        spatial.reread_labels(raw,result,recognize)
        self.assertEqual(len(calls),12)
        self.assertEqual(len(result['label_reviews']),6)
        self.assertTrue(all(c['status']=='pending' for c in result['tables'][0]['cells'][6:]))


if __name__=='__main__':unittest.main()
