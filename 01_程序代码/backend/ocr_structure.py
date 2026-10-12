"""Conservative spatial views of OCR observations; never invent missing symbols.

Coordinates describe observed relationships. Formula views with unreadable terms
remain pending and are not answer evidence. Original lines are retained for audit.
"""
from __future__ import annotations

import re
import statistics
import io


def detect(lines, rotation=0):
    """Infer in reading coordinates, publish boxes in the original image frame."""
    def turn(box, angle):
        points=[]
        for x,y in [(box[0],box[1]),(box[2],box[3])]:
            if angle==90:x,y=1-y,x
            elif angle==180:x,y=1-x,1-y
            elif angle==270:x,y=y,1-x
            points.append((x,y))
        return [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
    reading=[{**line,'box':turn(line['box'],rotation)} for line in lines]
    tables=borderless_tables(reading)
    excluded={i for t in tables for c in t['cells'] for i in c.get('evidence_lines',[])}
    result={'tables':tables,'formulas':formulas(reading,excluded)}
    def restore(value):
        if isinstance(value,dict):
            return {k:turn(v,(-rotation)%360) if k=='box' else restore(v) for k,v in value.items()}
        if isinstance(value,list):return [restore(v) for v in value]
        return value
    return restore(result)


def reread_formulas(raw, result, recognize, cancelled=None, deadline=None, rotation=0):
    """At most three actual crop calls; coordinate transforms retain page provenance."""
    from PIL import Image, ImageEnhance, ImageFilter
    from .document_providers import _check
    with Image.open(io.BytesIO(raw)) as source:
        image=source.convert('RGB')
    for formula in result.get('formulas',[])[:3]:
        _check(cancelled,deadline)
        x0,y0,x1,y1=formula['box'];width,height=image.size
        rect=(max(0,int((x0-.006)*width)),max(0,int((y0-.006)*height)),
              min(width,int((x1+.006)*width)+1),min(height,int((y1+.006)*height)+1))
        crop=image.crop(rect);scale=min(3,1600/max(crop.size));crop=crop.resize((int(crop.width*scale),int(crop.height*scale)))
        buf=io.BytesIO();crop.save(buf,format='PNG')
        try:observed=recognize(buf.getvalue(),rotation=rotation,cancelled=cancelled,deadline=deadline)
        except (InterruptedError,TimeoutError):raise
        except ValueError as exc:
            formula['crop_call']={'status':'failed','error':str(exc)};continue
        formula['crop_call']={'status':'completed','duration_ms':observed['duration_ms'],
                              'input_image_sha256':observed['input_image_sha256'],'page_pixel_rect':list(rect)}
        mapped=[]
        for line in observed['lines']:
            copy=dict(line);b=line['box']
            copy['box']=[(rect[0]+b[0]*(rect[2]-rect[0]))/width,(rect[1]+b[1]*(rect[3]-rect[1]))/height,
                         (rect[0]+b[2]*(rect[2]-rect[0]))/width,(rect[1]+b[3]*(rect[3]-rect[1]))/height]
            mapped.append(copy)
        formula['crop_observations']=mapped
        if not rotation:
            fractions=_fractions(crop,observed['lines'])
            formula['fractions']=fractions
            if fractions:
                formula['text']+='\n'+ '\n'.join(f['latex'] for f in fractions)
        enhanced=ImageEnhance.Contrast(crop).enhance(1.4).filter(ImageFilter.UnsharpMask(radius=1,percent=120,threshold=3))
        buf=io.BytesIO();enhanced.save(buf,format='PNG')
        try:second=recognize(buf.getvalue(),rotation=rotation,cancelled=cancelled,deadline=deadline)
        except (InterruptedError,TimeoutError):raise
        except ValueError as exc:formula['secondary_crop_call']={'status':'failed','error':str(exc)}
        else:
            formula['secondary_crop_call']={'status':'completed','duration_ms':second['duration_ms'],'input_image_sha256':second['input_image_sha256']}
            formula['secondary_crop_observations']=second['lines']
            formula['secondary_crop_coordinate_space']='formula_crop'
            if not rotation:
                additional=_fractions(enhanced,second['lines'])
                formula['secondary_fractions']=additional
                for fraction in formula.get('fractions',[]):
                    fraction['consistency']='repeated' if any(f['latex']==fraction['latex'] and abs(f['bar_box'][1]-fraction['bar_box'][1])<.03 for f in additional) else 'unconfirmed'
        formula['structure_candidates']=math_relations(observed['lines']) if not rotation else []
        # A crop does not certify the completeness of the entire equation.
        # Missing sum bounds, glyphs or delimiters must still be reviewed.
    return result


def _fractions(image,lines):
    import cv2
    import numpy as np
    pixels=cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2GRAY)
    ink=cv2.threshold(pixels,180,255,cv2.THRESH_BINARY_INV)[1]
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(8,image.width//60),1)))
    contours,_=cv2.findContours(horizontal,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);out=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if w<12 or w>image.width*.85 or h>max(5,image.height*.04):continue
        cx=(x+w/2)/image.width;cy=(y+h/2)/image.height
        above=[l for l in lines if l.get('status')=='confirmed' and l['box'][3]<cy
               and cy-l['box'][3]<.25 and l['box'][0]-.01<=cx<=l['box'][2]+.01
               and l['box'][2]-l['box'][0]<w/image.width+.04]
        below=[l for l in lines if l.get('status')=='confirmed' and l['box'][1]>cy
               and l['box'][1]-cy<.25 and l['box'][0]-.01<=cx<=l['box'][2]+.01
               and l['box'][2]-l['box'][0]<w/image.width+.04]
        if not above or not below:continue
        numerator=max(above,key=lambda l:l['box'][3]);denominator=min(below,key=lambda l:l['box'][1])
        out.append({'latex':r'\frac{'+numerator['text']+'}{'+denominator['text']+'}',
                    'numerator':numerator,'denominator':denominator,'bar_box':[x/image.width,y/image.height,(x+w)/image.width,(y+h)/image.height],
                    'status':'observed','verification':'printed_bar_and_ocr_terms','coordinate_space':'formula_crop'})
    return out[:10]


def _box(lines):
    return [min(l['box'][0] for l in lines), min(l['box'][1] for l in lines),
            max(l['box'][2] for l in lines), max(l['box'][3] for l in lines)]


def _rows(lines):
    groups = []
    for i, line in sorted(enumerate(lines), key=lambda pair: (pair[1]['box'][1], pair[1]['box'][0])):
        box = line['box']; cy = (box[1]+box[3])/2
        match = next((g for g in reversed(groups[-3:]) if abs(g['y']-cy) <= .55*min(g['height'], box[3]-box[1])), None)
        if match is None:
            groups.append({'y': cy, 'height': box[3]-box[1], 'items': [(i, line)]})
        else:
            match['items'].append((i, line))
    for group in groups:
        group['items'].sort(key=lambda item: item[1]['box'][0])
    return groups


def borderless_tables(lines):
    """Repeated numeric column centres, not whitespace alone, establish a grid."""
    rows = _rows(lines); tables = []; run = []
    numeric = re.compile(r'^[\s$+−\-\d.,%*†‡()]+$')
    # A centred merged row name may form an OCR row of its own. Bind it to
    # the first intersecting numeric row only when it is left of that grid.
    for floating in list(rows):
        if len(floating['items'])!=1:continue
        _,label=floating['items'][0];b=label['box']
        if numeric.fullmatch(label['text']):continue
        intersecting=[r for r in rows if r is not floating and len(r['items'])>=2
                      and all(numeric.fullmatch(l['text']) for _,l in r['items'])
                      and b[2]<min(l['box'][0] for _,l in r['items'])
                      and b[1]<=max(l['box'][3] for _,l in r['items']) and b[3]>=min(l['box'][1] for _,l in r['items'])]
        if len(intersecting)>=2:
            first_row=min(intersecting,key=lambda r:r['y'])
            first_row['items']=floating['items']+first_row['items'];rows.remove(floating)

    def publish(run):
        if len(run) < 2:
            return
        first = max(run,key=lambda r:len(r['items']))['items']; centres = [(l['box'][0]+l['box'][2])/2 for _, l in first]
        if len(centres) < 3 or len(centres) > 30:
            return
        previous=tables[-1] if tables else None
        matching_previous=bool(previous and previous['columns']==len(centres))
        if matching_previous:
            data=[c for c in previous['cells'] if c['role']!='column_header']
            matching_previous=all(any(c['column']==ci and abs((c['box'][0]+c['box'][2])/2-x)<.012 for c in data) for ci,x in enumerate(centres[1:],1))
        # Two rows require an explicit preceding header or an already observed
        # grid with identical numeric column positions.
        explicit_header=any(len(r['items'])>=2 and run[0]['y']-.1<r['y']<run[0]['y'] and not any(numeric.fullmatch(l['text']) for _,l in r['items']) for r in rows)
        if len(run)<3 and not matching_previous and not explicit_header:return
        grid = []; used = []
        for row in run:
            assigned=[min(range(len(centres)),key=lambda c:abs((l['box'][0]+l['box'][2])/2-centres[c])) for _,l in row['items']]
            if len(set(assigned))!=len(assigned):return
            if any(abs((l['box'][0]+l['box'][2])/2-centres[c])>.012 for c,(_,l) in zip(assigned,row['items']) if c):return
            grid.append(row); used.extend(l for _, l in row['items'])
        top = min(l['box'][1] for l in used); bottom = max(l['box'][3] for l in used)
        # Preserve preceding aligned header rows, including parent spanning labels.
        heights = statistics.median(l['box'][3]-l['box'][1] for l in used)
        group_rows=[r for r in rows if top-4*heights<=r['y']<top and len(r['items'])==1
                    and len(r['items'][0][1]['text'])<=50 and r['items'][0][1]['box'][2]-r['items'][0][1]['box'][0]<.25
                    and not numeric.fullmatch(r['items'][0][1]['text'])
                    and first[0][1]['box'][0]-.02<=r['items'][0][1]['box'][0]
                    and r['items'][0][1]['box'][2]<=first[-1][1]['box'][2]+.02]
        group_line=group_rows[-1]['items'][0][1] if group_rows else None
        headers = [r for r in rows if max(0,top-(.4 if matching_previous and group_line else .25)) <= r['y'] < top
                   and len(r['items']) >= 2
                   and (sum(bool(numeric.fullmatch(l['text'])) for _,l in r['items'])<2 or all(re.fullmatch(r'(?:19|20)\d{2}',l['text'].strip()) for _,l in r['items'][1:]))
                   and all(first[0][1]['box'][0]-.02 <= l['box'][0] and l['box'][2] <= used[-1]['box'][2]+.02 for _, l in r['items'])]
        headers=headers[-3:]
        cells = []; all_rows = headers+grid
        for ri, row in enumerate(all_rows):
            for li, line in row['items']:
                b = line['box']; cx = (b[0]+b[2])/2
                covered = [c for c, x in enumerate(centres) if b[0]-.012 <= x <= b[2]+.012]
                if ri >= len(headers):
                    covered = [min(range(len(centres)), key=lambda c: abs(centres[c]-cx))]
                if not covered:
                    continue
                cells.append({'id': f'borderless-{len(tables)}-{ri}-{li}', 'row': ri,
                              'column': min(covered), 'rowspan': 1, 'colspan': max(covered)-min(covered)+1,
                              'text': line['text'], 'box': b, 'role': 'column_header' if ri < len(headers) else 'row_header' if min(covered)==0 else 'data',
                              'status': line.get('status', 'confirmed'), 'evidence_lines': [li]})
        # Extend a row name only when its observed box intersects the next
        # data row and that row has no name; never fill ordinary blank cells.
        for cell in cells:
            if cell['role']!='row_header':continue
            following=cell['row']+1
            if following>=len(all_rows) or any(c['row']==following and c['column']==0 for c in cells):continue
            next_boxes=[l['box'] for _,l in all_rows[following]['items']]
            if next_boxes and cell['box'][3]>=min(b[1] for b in next_boxes) and cell['box'][1]<=max(b[3] for b in next_boxes):
                cell['rowspan']=2;cell['span_verification']='observed_box_intersects_both_rows'
        observations = used+[l for r in headers for _, l in r['items']]
        if group_line:observations.append(group_line)
        tables.append({'id': f'borderless-{len(tables)}', 'title': '无框表格坐标结构'+(' · '+group_line['text'] if group_line else ''),
                       'title_status':group_line.get('status','confirmed') if group_line else 'confirmed',
                       'box': _box(observations), 'rows': len(all_rows), 'columns': len(centres), 'cells': cells,
                       'method': 'ocr_aligned_columns', 'header_status': 'observed' if headers else 'pending',
                       'status': 'confirmed' if all(c['status']=='confirmed' for c in cells) else 'pending'})
    for row in rows:
        items = row['items']; nums = sum(bool(numeric.fullmatch(l['text'])) for _, l in items[1:])
        year_header=any(l['text'].strip().lower() in {'model','system','年份','项目','指标'} for _,l in items[:1]) and len(items)>=3 and all(re.fullmatch(r'(?:19|20)\d{2}',l['text'].strip()) for _,l in items[1:])
        suitable = not year_header and len(items) >= 2 and nums >= max(1, len(items)-2)
        if suitable and (not run or row['y']-run[-1]['y'] < 4*max(row['height'], run[-1]['height'])):
            run.append(row)
        else:
            publish(run); run = [row] if suitable else []
    publish(run)
    return tables[:20]


def formulas(lines, excluded=None):
    """Group displayed math spatially; incomplete 2D expressions stay pending."""
    out = []; used = set(excluded or ())
    for i, line in enumerate(lines):
        text = line['text'].strip(); b = line['box']
        if i in used or len(text)>60:continue
        compact_pair=bool(
            re.fullmatch(r'\d{1,4}|[α-ωΑ-Ω]',text) and b[2]-b[0]<.08
            and any(j!=i and j not in used
                    and re.search(r'[A-Za-zα-ωΑ-Ω]',text+l['text'])
                    and re.fullmatch(r'[A-Za-z0-9|α-ωΑ-Ω]+',l['text'].strip())
                    and l['box'][2]-l['box'][0]<.08
                    and 0<l['box'][1]-b[3]<max(.04,2*(b[3]-b[1]))
                    and abs((l['box'][0]+l['box'][2]-b[0]-b[2])/2)<.01
                    for j,l in enumerate(lines)))
        if not compact_pair and not re.search(r'[∑Σ∫]|^[^=]{1,20}\s*=\s*$',text):continue
        h = max(b[3]-b[1], .006)
        region = [(j,l) for j,l in enumerate(lines) if j not in used and len(l['text'])<=35
                  and b[1]-h <= (l['box'][1]+l['box'][3])/2 <= b[3]+(3*h if compact_pair else h)
                  and l['box'][0] >= b[0]-.22 and l['box'][2] <= min(1,b[2]+.3)
                  and (l['box'][2]-l['box'][0]) < .5]
        if len(region)<2:
            continue
        # Long prose is excluded. Keep exact line text and relative coordinates;
        # mathematical equality/denominators cannot be guessed from context.
        selected = [l for _,l in region]; box = _box(selected)
        layout = []
        for row in _rows(selected):
            layout.append(' | '.join(l['text'] for _,l in row['items']))
        out.append({'id': f'ocr-formula-{len(out)}', 'box': box, 'method': 'ocr_spatial_formula',
                    'status': 'pending', 'text': '\n'.join(layout), 'observations': selected,
                    'evidence_lines': [j for j,_ in region], 'reason': '二维公式需要核对分母、上下标和求和范围；保留原图坐标，不以逐行文字替代完整公式'})
        used.update(j for j,_ in region)
    return out[:12]


def reread_labels(raw, result, recognize, cancelled=None, deadline=None, rotation=0):
    """Bounded local rereads; agreement is OCR consistency, not semantic truth."""
    from PIL import Image, ImageEnhance, ImageFilter
    from .document_providers import _check
    import hashlib
    lines=result.get('lines',[]);targets={}
    for table in result.get('tables',[]):
        for cell in table.get('cells',[]):
            if cell.get('role') not in {'header','column_header','row_header'}:continue
            for index in cell.get('evidence_lines',[]):
                if not isinstance(index,int) or not 0<=index<len(lines):continue
                line=lines[index]
                if line.get('confidence',1)>=.94 and len(line['text'])<12 and not re.search(r'[→←↑↓�]',line['text']):continue
                priority=(cell['role']!='row_header',line.get('confidence',0))
                targets[index]=min(targets.get(index,priority),priority)
    if not targets:return result
    with Image.open(io.BytesIO(raw)) as opened:image=opened.convert('RGB')
    width,height=image.size;reviews=result.setdefault('label_reviews',[])
    for index in sorted(targets,key=lambda i:targets[i])[:6]:
        _check(cancelled,deadline);line=lines[index];b=line['box']
        rect=(max(0,int((b[0]-.008)*width)),max(0,int((b[1]-.004)*height)),
              min(width,int((b[2]+.008)*width)+1),min(height,int((b[3]+.004)*height)+1))
        crop=image.crop(rect);scale=min(4,1600/max(crop.size));crop=crop.resize((max(1,int(crop.width*scale)),max(1,int(crop.height*scale))))
        review={'observation_id':index,'original_text':line['text'],'box':b,'calls':[],'candidates':[],'candidate_observations':[],'status':'pending'}
        for name,variant in [('enlarged',crop),('contrast',ImageEnhance.Contrast(crop).enhance(1.4).filter(ImageFilter.UnsharpMask(radius=1,percent=120,threshold=3)))]:
            _check(cancelled,deadline);buf=io.BytesIO();variant.save(buf,format='PNG');payload=buf.getvalue()
            try:observed=recognize(payload,rotation=rotation,cancelled=cancelled,deadline=deadline)
            except (InterruptedError,TimeoutError):raise
            except ValueError as exc:
                review['calls'].append({'variant':name,'status':'failed','error':str(exc),'input_image_sha256':hashlib.sha256(payload).hexdigest()});continue
            review['calls'].append({'variant':name,'status':'completed','duration_ms':observed['duration_ms'],'input_image_sha256':observed['input_image_sha256']})
            # Reject neighbouring text and multi-line fragments; corrections must
            # occupy this original label, not merely occur elsewhere in a crop.
            cx=((b[0]+b[2])/2*width-rect[0])/(rect[2]-rect[0]);cy=((b[1]+b[3])/2*height-rect[1])/(rect[3]-rect[1])
            matching=[l for l in observed['lines'] if l.get('status')=='confirmed' and l.get('confidence',0)>=.9
                      and l['box'][0]<=cx<=l['box'][2] and l['box'][1]<=cy<=l['box'][3]]
            review['candidates'].append(matching[0]['text'] if len(matching)==1 else None)
            review['candidate_observations'].append(matching[0] if len(matching)==1 else None)
        candidates=review['candidates']
        if len(candidates)==2 and all(candidates) and candidates[0].strip()==candidates[1].strip() and not re.search(r'[→←↑↓�]|[₀-₉⁰-⁹]',candidates[0]):
            text=candidates[0].strip();review.update(status='consistent',text=text,verification='same_engine_two_crop_consistency')
            line.update(text=text,status='confirmed',original_text=review['original_text'],original_confidence=line.get('confidence'),confidence=min(l['confidence'] for l in review['candidate_observations']),label_review=review['verification'])
        else:
            line.update(status='pending',source_pending_reason='label_reread_disagreement')
        for table in result.get('tables',[]):
            for cell in table.get('cells',[]):
                if index in cell.get('evidence_lines',[]):
                    cell.update(text=line['text'],status=line['status'],label_review=review['status'])
            table['status']='confirmed' if all(c.get('status')=='confirmed' for c in table.get('cells',[])) else 'pending'
        reviews.append(review)
    return result


def math_relations(lines):
    """Observed operator/bound regions, kept separate from a certified formula."""
    nodes=[]
    for index,line in enumerate(lines):
        if not re.search(r'[∑Σ∫∏]',line['text']):continue
        b=line['box'];height=b[3]-b[1]
        neighbours=[(i,l) for i,l in enumerate(lines) if i!=index and len(l['text'])<=30
                    and abs((l['box'][0]+l['box'][2]-b[0]-b[2])/2)<max(.08,b[2]-b[0])]
        above=[i for i,l in neighbours if 0<b[1]-l['box'][3]<max(.08,2*height)]
        below=[i for i,l in neighbours if 0<l['box'][1]-b[3]<max(.08,2*height)]
        nodes.append({'kind':'large_operator','text':line['text'],'box':b,'operator_observation':index,'coordinate_space':'formula_crop',
                      'upper_candidates':above,'lower_candidates':below,'status':'pending',
                      'reason':'候选上下限须对照原图核验，未推断缺失符号'})
    return nodes[:12]
