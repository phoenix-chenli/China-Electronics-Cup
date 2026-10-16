"""Source geometry, evidence agreement and bounded optional page processing."""
import copy
import hashlib
import io
import threading
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image,ImageDraw
from backend import page_preprocessing as p, document_providers as providers


def line(text='Revenue 120.50',box=(.1,.2,.7,.3),score=.95,status='confirmed'):
    a,b,c,d=box
    return {'text':text,'box':list(box),'polygon':[[a,b],[c,b],[c,d],[a,d]],'confidence':score,'status':status,'line_id':0}


def result(lines=()):return {'lines':copy.deepcopy(list(lines)),'issues':[],'provenance':{},'duration_ms':1}


def image():
    im=Image.new('RGB',(400,300),'white');ImageDraw.Draw(im).rectangle((40,60,200,90),fill=(90,90,90))
    buf=io.BytesIO();im.save(buf,format='PNG');return im,buf.getvalue()


class PagePreprocessingTests(unittest.TestCase):
    def test_transparent_black_is_white_and_visible_ink_retained(self):
        im=Image.new('RGBA',(30,20),(0,0,0,0));im.putpixel((4,4),(0,0,0,255))
        out=p.white_background(im)
        self.assertEqual(out.getpixel((0,0)),(255,255,255));self.assertEqual(out.getpixel((4,4)),(0,0,0))
        b=io.BytesIO();im.save(b,format='PNG');self.assertEqual(providers._image(b.getvalue()).tobytes(),out.tobytes())

    def test_palette_transparency_composited(self):
        im=Image.new('P',(4,4),0);im.putpalette([0,0,0,0,0,0]+[0]*762);im.info['transparency']=0;im.putpixel((1,1),1)
        self.assertEqual(p.white_background(im).getpixel((0,0)),(255,255,255));self.assertEqual(p.white_background(im).getpixel((1,1)),(0,0,0))

    def test_uniform_page_skips_all_enhancement(self):
        self.assertFalse(p.analyze(Image.new('RGB',(900,1200),'white'))['eligible'])
        self.assertEqual(p.plans({'eligible':False,'flags':['noise']}),[])

    def test_colour_chart_does_not_trigger_destructive_greyscale(self):
        im=Image.new('RGB',(400,300),'white');d=ImageDraw.Draw(im)
        d.rectangle((30,30,180,280),fill=(10,80,180));d.rectangle((220,100,360,280),fill=(180,20,30))
        quality=p.analyze(im)
        self.assertGreater(quality['colour_fraction'],.025);self.assertEqual(p.plans(quality),[])

    def test_low_contrast_and_shadow_detected_without_reference_text(self):
        a=np.tile(np.linspace(100,230,600,dtype='uint8'),(800,1));im=Image.fromarray(a).convert('RGB');d=ImageDraw.Draw(im)
        for y in range(70,700,70):d.text((70,y),'Revenue 120.50',fill=(65,65,65))
        self.assertIn('uneven_illumination',p.analyze(im)['flags'])
        im=Image.new('RGB',(400,300),(210,210,210));d=ImageDraw.Draw(im)
        for y in range(20,260,30):d.rectangle((30,y,330,y+8),fill=(175,175,175))
        self.assertIn('low_contrast',p.analyze(im)['flags'])

    def test_skew_requires_consistent_lines_and_rejects_one_diagonal(self):
        gray=np.full((300,400),255,dtype='uint8');rows=[]
        for y in [.2,.35,.5,.65]:
            row=line(box=(.1,y,.8,y+.05));row['polygon'][1][1]+=.04;rows.append(row)
        angle,audit=p._skew(gray,rows)
        self.assertGreater(angle,2);self.assertEqual(audit['support'],4)
        self.assertEqual(p._skew(gray,rows[:1])[0],0)

    def test_expanded_deskew_affine_round_trip_and_size_budget(self):
        im,_=image();plan={'name':'geometry','deskew_degrees':5,'scale':8,'contrast':'none','interpolation':'cubic'}
        raw,inverse,size=p.render(im,plan)
        self.assertLessEqual(max(size),2400);self.assertLessEqual(size[0]*size[1],6000000)
        matrix=np.linalg.inv(np.asarray(inverse));original=np.array([[80,70,1],[200,70,1],[200,95,1],[80,95,1]],dtype=float)
        processed=(matrix@original.T).T
        row=line();row['polygon']=[[x/size[0],y/size[1]] for x,y,_ in processed]
        mapped,rejected=p.map_lines([row],inverse,size,im.size)
        self.assertFalse(rejected);self.assertTrue(np.allclose(np.array(mapped[0]['polygon'])*im.size,original[:,:2],atol=1e-4))

    def test_padding_is_not_source_evidence(self):
        mapped,rejected=p.map_lines([line(box=(0,0,.1,.1))],[[1,0,-30],[0,1,-30],[0,0,1]],[400,300],[400,300])
        self.assertEqual(mapped,[]);self.assertEqual(rejected[0]['reason'],'outside_original_page_or_padding')

    def test_changed_number_never_overwrites_original(self):
        original=result([line('120')]);variant=[line('720')]
        merged,audit=p.merge(original,[variant,variant])
        self.assertEqual(merged['lines'][0]['text'],'120');self.assertEqual(merged['lines'][0]['status'],'pending')
        self.assertEqual(original['lines'][0]['status'],'confirmed');self.assertEqual(audit[0]['action'],'literal_conflict_requires_review')

    def test_whole_region_needed_not_nearby_or_fragment(self):
        original=result([line('OperatingProfit',score=.65,status='pending')]);fragment=[line('Profit',box=(.5,.2,.7,.3))]
        merged,_=p.merge(original,[fragment,fragment])
        self.assertEqual(len(merged['lines']),1);self.assertEqual(merged['lines'][0]['status'],'pending')

    def test_low_score_original_restored_only_by_two_full_unchanged_readings(self):
        original=result([line(score=.65,status='pending')]);variant=[line()]
        self.assertEqual(p.merge(original,[variant])[0]['lines'][0]['status'],'pending')
        merged,audit=p.merge(original,[variant,variant]);self.assertEqual(merged['lines'][0]['status'],'confirmed')
        self.assertEqual(merged['lines'][0]['original_confidence'],.65);self.assertTrue(audit)

    def test_other_source_pending_reason_not_cleared(self):
        row=line(score=.65,status='pending');row['source_pending_reason']='formula_structure_requires_review'
        merged,_=p.merge(result([row]),[[line()],[line()]])
        self.assertEqual(merged['lines'][0]['source_pending_reason'],'formula_structure_requires_review')

    def test_new_text_requires_two_unique_full_source_regions(self):
        variant=[line()]
        self.assertFalse(p.merge(result(),[variant])[0]['lines'])
        self.assertFalse(p.merge(result(),[variant,variant+variant])[0]['lines'])
        merged,audit=p.merge(result(),[variant,variant]);self.assertEqual(len(merged['lines']),1);self.assertEqual(audit[0]['action'],'new_observation_requires_review')
        self.assertEqual(merged['lines'][0]['status'],'pending')

    def test_combined_output_limit_does_not_truncate(self):
        original=result([line('x'*24000)]);new=[line('New row',box=(.1,.7,.8,.8))]
        merged,audit=p.merge(original,[new,new]);self.assertEqual(merged['lines'],original['lines']);self.assertEqual(audit[-1]['reason'],'combined_output_limit')

    def test_repeated_missing_minus_is_still_a_review_candidate(self):
        # Real development regression: two enhanced views both dropped '-'.
        guessed=[line('华南利润：18.75')]
        merged,_=p.merge(result(),[guessed,guessed])
        self.assertEqual(merged['lines'][0]['status'],'pending')
        self.assertEqual(merged['lines'][0]['source_pending_reason'],'preprocessing_new_observation_requires_review')

    def test_shared_coordinates_keep_manual_quarter_turn(self):
        a=line('top',box=(.1,.1,.2,.2));b=line('bottom',box=(.7,.1,.8,.2))
        merged,_=p.merge(result([b]),[[a],[a]],rotation=90)
        self.assertEqual([l['text'] for l in merged['lines']],['top','bottom'])

    def run_page(self, callback, **extra):
        im,raw=image()
        return p.recognize(raw,im,callback,**extra)

    def test_clear_result_one_call_and_real_provenance(self):
        calls=[]
        def ocr(raw,**_):calls.append(raw);return {**result([line()]),'input_image_sha256':hashlib.sha256(raw).hexdigest()}
        with patch.object(p,'analyze',return_value={'eligible':True,'flags':[]}):out=self.run_page(ocr)
        self.assertEqual(len(calls),1);self.assertEqual(out['preprocessing']['extra_calls'],0);self.assertEqual(out['lines'][0]['text'],'Revenue 120.50')

    def test_identical_pixels_not_counted_as_distinct_views(self):
        im,raw=image();calls=[]
        def ocr(data,**_):calls.append(data);return {**result(),'input_image_sha256':hashlib.sha256(data).hexdigest()}
        with patch.object(p,'analyze',return_value={'eligible':True,'flags':['small_text']}),patch.object(p,'render',return_value=(raw,np.eye(3).tolist(),list(im.size))):out=self.run_page(ocr)
        self.assertEqual(len(calls),1);self.assertEqual(len(out['preprocessing']['skipped_variants']),2)

    def test_optional_failure_keeps_base_and_records_error(self):
        calls=[];events=[]
        def ocr(raw,**_):
            calls.append(raw)
            if len(calls)>1:raise ValueError('fixture optional inference failed')
            return {**result([line()]),'input_image_sha256':hashlib.sha256(raw).hexdigest()}
        with patch.object(p,'analyze',return_value={'eligible':True,'flags':['low_contrast']}):out=self.run_page(ocr,trace=events.append)
        self.assertEqual(len(calls),3);self.assertEqual(out['lines'][0]['text'],'Revenue 120.50')
        self.assertEqual([r['status'] for r in out['preprocessing']['attempts']],['completed','failed','failed']);self.assertTrue(all(e['duration_ms'] is not None for e in events if e['status']!='running'))

    def test_cancellation_not_swallowed_by_fallback(self):
        cancelled=threading.Event();calls=[];events=[]
        def ocr(raw,**_):calls.append(raw);cancelled.set();return {**result(),'input_image_sha256':hashlib.sha256(raw).hexdigest()}
        with self.assertRaises(InterruptedError):self.run_page(ocr,cancelled=cancelled,trace=events.append)
        self.assertEqual(len(calls),1);self.assertEqual(events[-1]['status'],'cancelled')

    def test_expired_deadline_runs_nothing(self):
        with self.assertRaises(TimeoutError):self.run_page(lambda *_a,**_k:self.fail('inference called'),deadline=0)

    def test_input_hash_mismatch_fails_base(self):
        with self.assertRaisesRegex(ValueError,'摘要'):self.run_page(lambda *_a,**_k:{**result(),'input_image_sha256':'0'*64})

    def test_attaching_two_embedded_images_keeps_both(self):
        page={};p.attach(page,{'preprocessing':{'version':p.VERSION}},'one');p.attach(page,{'preprocessing':{'version':p.VERSION}},'two')
        self.assertEqual([r['media_id'] for r in page['page_preprocessing']],['one','two'])
        p.attach(page,{},'one');self.assertEqual([r['media_id'] for r in page['page_preprocessing']],['two'])


if __name__=='__main__':unittest.main()
