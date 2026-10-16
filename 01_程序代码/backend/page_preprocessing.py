"""Bounded local page variants with inverse geometry and literal OCR agreement.

Pixels and model scores are observations, never semantic correctness scores.
The original image/reading is retained; a changed glyph is a review candidate.
"""
from __future__ import annotations
import copy
import hashlib
import io
import math
import time
import uuid
from datetime import datetime,timezone

VERSION = 'page-preprocessing-v1'
MAX_EXTRA_CALLS = 2
MAX_SECONDS = 120
MAX_SIDE = 2400
MAX_LINES = 400
MAX_CHARACTERS = 24000


def attach(page, result, media_id=None):
    records=[r for r in page.get('page_preprocessing',[]) if r.get('media_id')!=media_id]
    if result.get('preprocessing'):records.append({**copy.deepcopy(result['preprocessing']),'media_id':media_id})
    page['page_preprocessing']=records


def white_background(image):
    """Composite transparency before RGB conversion; never change orientation."""
    from PIL import Image
    if image.mode in {'RGBA','LA'} or 'transparency' in image.info:
        rgba=image.convert('RGBA')
        return Image.alpha_composite(Image.new('RGBA',image.size,'white'),rgba).convert('RGB')
    return image.convert('RGB')


def _check(cancelled, deadline):
    if cancelled is not None and cancelled.is_set():raise InterruptedError('页面预处理已取消')
    if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('页面预处理超过总时限')


def _skew(gray, lines):
    import cv2
    import numpy as np
    h,w=gray.shape;angles=[]
    for line in lines:
        polygon=line.get('polygon')
        if not polygon or len(polygon)!=4 or len(line.get('text','').strip())<4:continue
        dx=(polygon[1][0]-polygon[0][0])*w;dy=(polygon[1][1]-polygon[0][1])*h
        if math.hypot(dx,dy)<20:continue
        angle=(math.degrees(math.atan2(dy,dx))+45)%90-45
        if abs(angle)<=8:angles.append(angle)
    if len(angles)>=4:
        center=float(np.median(angles));inliers=[a for a in angles if abs(a-center)<=.7]
        if len(inliers)>=4 and len(inliers)/len(angles)>=.8:
            return round(float(np.median(inliers)),3),{'method':'ocr_line_baselines','support':len(inliers),'observed_lines':len(angles)}
    # Fallback only for text-like masks, with several independent row bands.
    _,mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    density=float(np.mean(mask>0))
    if not .003<=density<=.20:return 0,{'method':'projection','reason':'non_text_density'}
    samples=[]
    for angle in np.arange(-7,7.01,.5):
        m=cv2.getRotationMatrix2D((w/2,h/2),float(angle),1)
        rotated=cv2.warpAffine(mask,m,(w,h),flags=cv2.INTER_NEAREST,borderValue=0)
        projection=np.sum(rotated>0,axis=1,dtype=np.float64)
        samples.append((float(np.sum(projection**2)),float(angle),projection))
    best=max(samples,key=lambda v:v[0]);zero=next(v[0] for v in samples if v[1]==0)
    other=max(v[0] for v in samples if abs(v[1]-best[1])>=1.5)
    active=best[2]>max(8,float(best[2].max())*.35)
    bands=int(np.sum(active & ~np.r_[False,active[:-1]]))
    gain=best[0]/max(1,zero);separation=best[0]/max(1,other)
    accepted=abs(best[1])<7 and gain>=1.25 and separation>=1.04 and bands>=4
    return (best[1] if accepted else 0),{'method':'projection','gain':round(gain,4),'separation':round(separation,4),'row_bands':bands,'accepted':accepted}


def analyze(image, lines=()):
    import cv2
    import numpy as np
    small=image.copy();small.thumbnail((900,900))
    rgb=np.asarray(small);gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    h,w=gray.shape
    if min(h,w)<16:return {'eligible':False,'reason':'crop_or_tiny_image','flags':[]}
    p1,p50,p99=np.percentile(gray,[1,50,99])
    if p99-p1<3:return {'eligible':False,'reason':'blank_or_uniform_image','flags':[]}
    color=float(np.mean(np.max(rgb,axis=2).astype(float)-np.min(rgb,axis=2)>24))
    kernel=max(9,(min(h,w)//25)|1)
    background=cv2.morphologyEx(gray,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(kernel,kernel)))
    bg10,bg90=np.percentile(background,[10,90])
    residual=gray.astype(float)-cv2.medianBlur(gray,3).astype(float)
    smooth=(background.astype(float)-gray.astype(float))<8
    noise=float(np.std(residual[smooth])) if np.any(smooth) else 0
    flags=[]
    inverted=p50<90 and p99-p1>80
    work=255-gray if inverted else gray
    textlike=color<.025
    if inverted and textlike:flags.append('dark_background')
    if textlike and bg90-bg10>32 and not inverted:flags.append('uneven_illumination')
    if textlike and p99-p1<105:flags.append('low_contrast')
    if textlike and noise>9:flags.append('noise')
    heights=[(line['box'][3]-line['box'][1])*image.height for line in lines if line.get('box') and len(line.get('text',''))>=3]
    if heights and float(np.median(heights))<14 and max(image.size)<1800:flags.append('small_text')
    if textlike and 'uneven_illumination' in flags:
        work=np.clip(work.astype(float)*np.minimum(2.5,245/np.maximum(background.astype(float),40)),0,255).astype('uint8')
    angle,audit=_skew(work,lines) if textlike else (0,{'method':'skipped','reason':'colour_content'})
    if .4<=abs(angle)<=7:flags.append('skew')
    return {'eligible':True,'flags':flags,'colour_fraction':round(color,5),'background_spread':round(float(bg90-bg10),3),
            'intensity_range':round(float(p99-p1),3),'noise_residual_std':round(noise,3),'deskew_degrees':angle,
            'skew_observation':audit,'analysis_size':[w,h],
            'orientation':'explicit_user_rotation_only; no automatic quarter-turn or perspective claim'}


def plans(quality):
    flags=set(quality.get('flags',[]))
    if not quality.get('eligible') or not flags:return []
    common={'deskew_degrees':quality.get('deskew_degrees',0) if 'skew' in flags else 0,
            'flatten':'uneven_illumination' in flags,'denoise':'noise' in flags,
            'invert':'dark_background' in flags,'scale':2 if 'small_text' in flags else 1}
    return [{**common,'name':'adaptive_primary','contrast':'stretch' if 'low_contrast' in flags else 'none','interpolation':'cubic'},
            {**common,'name':'adaptive_secondary','contrast':'clahe' if flags & {'low_contrast','uneven_illumination','noise','dark_background'} else 'none','interpolation':'linear'}]


def render(image, plan):
    """Return PNG and the exact processed-pixel -> source-pixel affine map."""
    import cv2
    import numpy as np
    rgb=np.asarray(image);height,width=rgb.shape[:2];matrix=np.eye(3,dtype=float)
    if plan.get('invert'):rgb=255-rgb
    if plan.get('flatten') or plan.get('denoise') or plan.get('contrast')!='none':
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        if plan.get('denoise'):gray=cv2.bilateralFilter(gray,5,18,3)
        if plan.get('flatten'):
            # Estimate paper at reduced resolution; no foreground erasing/opening.
            sw=min(width,700);sh=max(1,round(height*sw/width))
            down=cv2.resize(gray,(sw,sh),interpolation=cv2.INTER_AREA)
            k=max(15,(min(sw,sh)//18)|1)
            bg=cv2.morphologyEx(down,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(k,k)))
            bg=cv2.GaussianBlur(bg,(0,0),3)
            bg=cv2.resize(bg,(width,height),interpolation=cv2.INTER_LINEAR).astype(float)
            gray=np.clip(gray.astype(float)*np.minimum(2.5,245/np.maximum(bg,40)),0,255).astype('uint8')
        if plan.get('contrast')=='clahe':gray=cv2.createCLAHE(clipLimit=2,tileGridSize=(8,8)).apply(gray)
        elif plan.get('contrast')=='stretch':
            lo,hi=np.percentile(gray,[.5,99.5])
            if hi-lo>=5:gray=np.clip((gray.astype(float)-lo)*255/(hi-lo),0,255).astype('uint8')
        rgb=cv2.cvtColor(gray,cv2.COLOR_GRAY2RGB)
    angle=plan.get('deskew_degrees',0)
    if angle:
        m=cv2.getRotationMatrix2D((width/2,height/2),angle,1)
        rad=math.radians(angle);nw=math.ceil(abs(width*math.cos(rad))+abs(height*math.sin(rad)));nh=math.ceil(abs(height*math.cos(rad))+abs(width*math.sin(rad)))
        m[0,2]+=(nw-width)/2;m[1,2]+=(nh-height)/2
        rgb=cv2.warpAffine(rgb,m,(nw,nh),flags=cv2.INTER_CUBIC if plan['interpolation']=='cubic' else cv2.INTER_LINEAR,borderValue=(255,255,255))
        matrix=np.vstack((m,[0,0,1]));width,height=nw,nh
    scale=min(float(plan.get('scale',1)),MAX_SIDE/max(width,height),(6_000_000/(width*height))**.5)
    nw=max(1,round(width*scale));nh=max(1,round(height*scale))
    if (nw,nh)!=(width,height):
        rgb=cv2.resize(rgb,(nw,nh),interpolation=cv2.INTER_CUBIC if plan['interpolation']=='cubic' else cv2.INTER_LINEAR)
        # Pixel-centre resizing: OpenCV maps x_d+.5 to (x_s+.5)*scale.
        sx,sy=nw/width,nh/height
        matrix=np.array([[sx,0,(sx-1)/2],[0,sy,(sy-1)/2],[0,0,1]])@matrix
    from PIL import Image
    output=io.BytesIO();Image.fromarray(rgb).save(output,format='PNG')
    return output.getvalue(),np.linalg.inv(matrix).tolist(),[nw,nh]


def map_lines(lines, inverse, processed_size, source_size):
    import numpy as np
    mapped=[];rejected=[];pw,ph=processed_size;sw,sh=source_size;m=np.asarray(inverse,dtype=float)
    for row in lines:
        line=copy.deepcopy(row);polygon=row.get('polygon')
        if polygon is None:
            b=row.get('box');polygon=[[b[0],b[1]],[b[2],b[1]],[b[2],b[3]],[b[0],b[3]]] if b else []
        points=[]
        for p in polygon:
            v=m@np.array([p[0]*pw,p[1]*ph,1]);points.append([float(v[0]/sw),float(v[1]/sh)])
        if len(points)!=4 or any(not math.isfinite(x+y) or not -1e-6<=x<=1+1e-6 or not -1e-6<=y<=1+1e-6 for x,y in points):
            rejected.append({'text':row.get('text'),'reason':'outside_original_page_or_padding'});continue
        points=[[round(min(1,max(0,v)),8) for v in p] for p in points]
        line.update(polygon=points,box=[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)])
        mapped.append(line)
    return mapped,rejected


def _text(line):return ' '.join(line.get('text','').split())


def _overlap(a,b):
    x=max(0,min(a[2],b[2])-max(a[0],b[0]));y=max(0,min(a[3],b[3])-max(a[1],b[1]));area=x*y
    aa=(a[2]-a[0])*(a[3]-a[1]);bb=(b[2]-b[0])*(b[3]-b[1])
    return area/max(1e-12,aa),area/max(1e-12,bb)


def _matches(line, rows, exact=True):
    return [r for r in rows if (not exact or _text(r)==_text(line)) and min(_overlap(line['box'],r['box']))>=.6]


def merge(original, variants, rotation=0):
    """Never select a page by mean score; keep literal conflicts for review."""
    result=copy.deepcopy(original);lines=result['lines'];decisions=[]
    for line in lines:
        candidates=[_matches(line,v) for v in variants]
        if len(candidates)!=2 or not all(len(v)==1 and v[0].get('confidence',0)>=.9 for v in candidates):
            alternatives=[_matches(line,v,False) for v in variants]
            if len(alternatives)==2 and all(len(v)==1 and v[0].get('confidence',0)>=.9 for v in alternatives) and _text(alternatives[0][0])==_text(alternatives[1][0])!=_text(line):
                line.update(status='pending',source_pending_reason='preprocessing_literal_conflict',preprocessing_candidates=[v[0]['text'] for v in alternatives])
                decisions.append({'text':line['text'],'box':line['box'],'action':'literal_conflict_requires_review','candidates':line['preprocessing_candidates']})
            continue
        if line.get('status')=='pending' and line.get('source_pending_reason') in {None,'low_ocr_score'}:
            line.update(status='confirmed',original_confidence=line.get('confidence'),confidence=min(v[0]['confidence'] for v in candidates),preprocessing_verification='same_engine_two_view_literal_consistency')
            line.pop('source_pending_reason',None)
            decisions.append({'text':line['text'],'box':line['box'],'action':'unchanged_observation_recovered'})
    additions=[]
    if len(variants)==2:
        for line in variants[0]:
            if line.get('confidence',0)<.9 or line.get('status')!='confirmed' or not _text(line):continue
            if any(max(_overlap(line['box'],old['box']))>.15 for old in lines+additions):continue
            matches=_matches(line,variants[1])
            if len(matches)!=1 or matches[0].get('confidence',0)<.9 or matches[0].get('status')!='confirmed':continue
            # A repeated observation must be unique in both views, not an OCR
            # duplicate reused to corroborate a neighbour.
            if len(_matches(matches[0],variants[0]))!=1:continue
            # Two enhanced views can repeat a missing minus or a wrong glyph.
            # Newly recovered content has no original-reading witness and must
            # remain a human-review candidate, even with two high scores.
            item=copy.deepcopy(line);item.update(status='pending',confidence=min(line['confidence'],matches[0]['confidence']),preprocessing_verification='same_engine_two_view_new_observation',source_pending_reason='preprocessing_new_observation_requires_review')
            additions.append(item)
    if len(lines)+len(additions)>MAX_LINES or sum(len(line['text']) for line in lines+additions)>MAX_CHARACTERS:
        decisions.append({'action':'new_observations_omitted','reason':'combined_output_limit','count':len(additions)});additions=[]
    for line in additions:
        line['line_id']=len(lines);lines.append(line)
        decisions.append({'text':line['text'],'box':line['box'],'action':'new_observation_requires_review'})
    if additions:
        def reading_key(line):
            b=line['box'];x,y=(b[0]+b[2])/2,(b[1]+b[3])/2
            x,y={0:(x,y),90:(1-y,x),180:(1-x,1-y),270:(y,1-x)}[rotation]
            return y,x
        lines.sort(key=reading_key)
        for i,line in enumerate(lines):line['line_id']=i
    result['issues']=[i for i in result.get('issues',[]) if i.get('code') not in {'no_text','low_ocr_score'}]
    for line in lines:
        if line.get('status')!='confirmed':result['issues'].append({'code':line.get('source_pending_reason') or 'low_ocr_score','message':'页面多视图识别仍需对照原图复核','line_id':line.get('line_id'),'box':line['box']})
    if not lines:result['issues'].append({'code':'no_text','message':'本机OCR及按需预处理未识别到可靠文字'})
    return result,decisions


def recognize(raw, image, recognize_line, rotation=0, cancelled=None, deadline=None, trace=None):
    started=time.monotonic();deadline=min(deadline if deadline is not None else started+MAX_SECONDS,started+MAX_SECONDS)
    record={'version':VERSION,'input_image_sha256':hashlib.sha256(raw).hexdigest(),'source_size':list(image.size),
            'max_extra_calls':MAX_EXTRA_CALLS,'attempts':[],'independent_verification':False,
            'confidence_scope':'uncalibrated_same_engine_observations; not semantic correctness'}
    def run(payload,name,**metadata):
        _check(cancelled,deadline);begin=time.monotonic()
        call={'id':uuid.uuid4().hex,'method':'page_ocr','variant':name,'status':'running','duration_ms':None,'started_at':datetime.now(timezone.utc).isoformat(),
              'input_image_sha256':hashlib.sha256(payload).hexdigest(),**metadata}
        record['attempts'].append(call)
        if trace:trace(copy.deepcopy(call))
        try:
            value=recognize_line(payload,rotation=rotation,cancelled=cancelled,deadline=deadline)
            _check(cancelled,deadline)
            if value.get('input_image_sha256')!=call['input_image_sha256']:raise ValueError('页面识别图像摘要与实际输入不符')
            call.update(status='completed',model_duration_ms=value.get('duration_ms'),line_count=len(value.get('lines',[])))
            return value
        except (ValueError,InterruptedError,TimeoutError) as exc:
            call.update(status='cancelled' if isinstance(exc,InterruptedError) else 'timeout' if isinstance(exc,TimeoutError) else 'failed',error=str(exc));raise
        finally:
            call['duration_ms']=round((time.monotonic()-begin)*1000)
            call['finished_at']=datetime.now(timezone.utc).isoformat()
            if trace:trace(copy.deepcopy(call))
    original=run(raw,'original');_check(cancelled,deadline)
    quality=analyze(image,original['lines']);record['quality']=quality
    variants=[];seen_pixels={hashlib.sha256(image.tobytes()).hexdigest()}
    for plan in plans(quality)[:MAX_EXTRA_CALLS]:
        _check(cancelled,deadline);stage=time.monotonic()
        payload,inverse,size=render(image,plan);_check(cancelled,deadline)
        from PIL import Image
        with Image.open(io.BytesIO(payload)) as prepared:pixel_digest=hashlib.sha256(prepared.convert('RGB').tobytes()).hexdigest()
        if pixel_digest in seen_pixels:
            record.setdefault('skipped_variants',[]).append({'variant':plan['name'],'reason':'identical_pixel_view'});continue
        seen_pixels.add(pixel_digest)
        try:
            observed=run(payload,plan['name'],operations=plan,processed_size=size,source_inverse_affine=inverse,render_duration_ms=round((time.monotonic()-stage)*1000))
        except ValueError:continue
        mapped,rejected=map_lines(observed['lines'],inverse,size,image.size)
        record['attempts'][-1]['discarded_padding_observations']=rejected
        variants.append(mapped)
    result,decisions=merge(original,variants,rotation) if variants else (copy.deepcopy(original),[])
    _check(cancelled,deadline)
    record.update(status='completed',decisions=decisions,extra_calls=len(record['attempts'])-1,duration_ms=round((time.monotonic()-started)*1000),
                  selection='original retained; full-region literal agreement only',result_line_count=len(result['lines']))
    result['preprocessing']=record;result['provenance']['preprocessing']=copy.deepcopy(record)
    result['duration_ms']=record['duration_ms'];result['provenance']['duration_ms']=record['duration_ms']
    return result
