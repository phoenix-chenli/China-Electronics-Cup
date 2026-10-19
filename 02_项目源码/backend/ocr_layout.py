"""Conservative unruled table candidates from observed word geometry.

These hypotheses have no physical grid proof and never authorize SQL values.
"""
from __future__ import annotations

from collections import Counter
import re
import statistics

NUMBER = re.compile(r'[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[%％]?')


def _numeric(text):
    return bool(NUMBER.fullmatch(re.sub(r'\s+', '', text)))


def _box(items):
    return [min(w['bbox'][0] for w in items), min(w['bbox'][1] for w in items),
            max(w['bbox'][2] for w in items), max(w['bbox'][3] for w in items)]


def _text(items):
    from .ocr_tesseract import join_words
    return join_words(w['text'] for w in sorted(items, key=lambda w: w['bbox'][0]))


def _rows(words, image):
    heights = [w['bbox'][3]-w['bbox'][1] for w in words]
    height = statistics.median(heights)
    char_width = statistics.median((w['bbox'][2]-w['bbox'][0])/max(1,len(w['text'])) for w in words)
    gap = max(char_width*2.1, height*image.height/image.width*.65)
    bands = []
    for word in sorted(words, key=lambda w: ((w['bbox'][1]+w['bbox'][3])/2,w['bbox'][0])):
        center = (word['bbox'][1]+word['bbox'][3])/2
        if bands and abs(center-bands[-1]['center']) <= .6*height:
            bands[-1]['words'].append(word)
            bands[-1]['center'] = statistics.median((w['bbox'][1]+w['bbox'][3])/2 for w in bands[-1]['words'])
        else:
            bands.append({'center':center,'words':[word]})
    for band in bands:
        segments = []
        for word in sorted(band['words'],key=lambda w:w['bbox'][0]):
            if segments and word['bbox'][0]-max(w['bbox'][2] for w in segments[-1]) <= gap:
                segments[-1].append(word)
            else:
                segments.append([word])
        band['segments'] = [dict(words=s,bbox=_box(s),text=_text(s)) for s in segments]
        band['bbox'] = _box(band['words'])
    return bands,height


def _body(row):
    parts = row['segments']
    return (2 <= len(parts) <= 20 and sum(_numeric(p['text']) for p in parts) >= 1
            and all(len(p['text']) <= 100 and not any(c in p['text'] for c in '=∑∫') for p in parts))


def _assemble(bands, indices, height):
    width = Counter(len(bands[i]['segments']) for i in indices).most_common(1)[0][0]
    if width < 3:
        return None
    exemplar = next(bands[i]['segments'] for i in indices if len(bands[i]['segments']) == width)
    centers = [(s['bbox'][0]+s['bbox'][2])/2 for s in exemplar]
    columns = [[] for _ in centers]
    assignments = []
    for index in indices:
        assigned = {}
        for part in bands[index]['segments']:
            center = (part['bbox'][0]+part['bbox'][2])/2
            col = min(range(width),key=lambda c:abs(center-centers[c]))
            spacing = min([abs(centers[col]-centers[c]) for c in range(width) if c != col])
            if col in assigned or abs(center-centers[col]) > spacing*.4:
                return None
            assigned[col]=part;columns[col].append(part)
        if len(assigned)<width-1:
            return None
        assignments.append(assigned)
    if any(not column for column in columns):
        return None
    lefts = [min(p['bbox'][0] for p in column) for column in columns]
    rights = [max(p['bbox'][2] for p in column) for column in columns]
    if any(rights[c] >= lefts[c+1] for c in range(width-1)):
        return None
    gaps=[lefts[c+1]-rights[c] for c in range(width-1)]
    # Separate aligned panels can share every y-coordinate. A persistent wide
    # gutter plus a new text-label column does not prove one six-column table.
    for split in range(2,width-1):
        if gaps[split-1] > 2.5*statistics.median(gaps):
            label_fractions=[sum(col in row and not _numeric(row[col]['text']) for row in assignments)/len(assignments)
                             for col in (0,split)]
            if min(label_fractions)>=.66:
                return None
    xs = [max(0.,lefts[0]-.012),*[(rights[c]+lefts[c+1])/2 for c in range(width-1)],min(1.,rights[-1]+.012)]
    first = indices[0]
    if first == 0:
        return None  # Stable aligned numbers without a preceding header abstain.
    leaf = bands[first-1]
    if bands[first]['center']-leaf['center']>height*4:
        return None
    headers = []
    for index in range(first-1,max(-1,first-4),-1):
        current = bands[index]
        following = bands[index+1]
        if following['center']-current['center']>height*4 or any(_numeric(p['text']) for p in current['segments']):
            break
        placements = []
        for part in current['segments']:
            b = part['bbox']
            if b[0] < xs[0]-.02 or b[2] > xs[-1]+.02 or not any(c.isalpha() for c in part['text']):
                placements=[];break
            cols = [c for c in range(width) if min(b[2],xs[c+1])-max(b[0],xs[c]) > (b[2]-b[0])*.12]
            if not cols or cols != list(range(min(cols),max(cols)+1)) or len(cols)==width:
                placements=[];break
            placements.append((part,cols))
        covered = [c for _,cols in placements for c in cols]
        if not placements or len(covered)!=len(set(covered)):
            break
        if not headers and len(covered)<width-1:
            return None
        if headers and len(placements)<2:
            break  # A paragraph/caption spanning the table is not a parent label.
        headers.insert(0,(index,placements))
    if not headers:
        return None
    all_indices=[i for i,_ in headers]+indices
    row_count=len(all_indices)
    if row_count*width>1200 or row_count>80:
        return None
    centers_y=[bands[i]['center'] for i in all_indices]
    ys=[max(0.,bands[all_indices[0]]['bbox'][1]-.006),
        *[(a+b)/2 for a,b in zip(centers_y,centers_y[1:])],
        min(1.,bands[all_indices[-1]]['bbox'][3]+.006)]
    rows=[[None]*width for _ in all_indices]
    cells=[]
    for row in range(row_count):
        for col in range(width):
            cells.append(dict(row=row+1,col=col+1,text=None,bbox=[xs[col],ys[row],xs[col+1],ys[row+1]],
                              rowspan=1,colspan=1,covered_by=None,confidence=None,requires_review=True,
                              verification='word_layout_candidate'))
    for row,(_,placements) in enumerate(headers):
        for part,cols in placements:
            start,end=cols[0],cols[-1]
            anchor=cells[row*width+start]
            anchor.update(text=part['text'],colspan=len(cols),bbox=[xs[start],ys[row],xs[end+1],ys[row+1]],
                          confidence=min(w['confidence'] for w in part['words']))
            rows[row][start]=part['text']
            for col in cols[1:]:cells[row*width+col]['covered_by']=[row+1,start+1]
    for offset,assigned in enumerate(assignments,len(headers)):
        for col,part in assigned.items():
            # Crossing observations are rejected; no splitting by number text.
            if part['bbox'][0]<xs[col] or part['bbox'][2]>xs[col+1]:return None
            cells[offset*width+col].update(text=part['text'],confidence=min(w['confidence'] for w in part['words']))
            rows[offset][col]=part['text']
    return dict(bbox=[xs[0],ys[0],xs[-1],ys[-1]],extraction='ocr_layout',structure_basis='aligned_word_layout',
                rows=rows,row_count=row_count,column_count=width,cells=cells,header_rows=list(range(1,len(headers)+1)),
                header_basis='inferred',header_names=[],data_row_offset=0,has_merged_cells=any(c['colspan']>1 for c in cells),
                sql_eligible=False,requires_review=True,
                review_reasons=['无框布局关系来自词框对齐候选，未获物理格线证明；不得直接作为SQL业务数据'])


def detect_layout_tables(image, words, ruled_tables=()):
    if not 9<=len(words)<=5000:
        return []
    words=[w for w in words if w.get('text','').strip() and w.get('confidence',0)>=.35]
    if len(words)<9:return []
    bands,height=_rows(words,image)
    groups=[]
    for i,band in enumerate(bands):
        if not _body(band):continue
        if groups and i==groups[-1][-1]+1 and band['center']-bands[groups[-1][-1]]['center']<=height*4:
            groups[-1].append(i)
        else:groups.append([i])
    output=[]
    for indices in groups:
        if len(indices)<3:continue
        table=_assemble(bands,indices,height)
        if not table:continue
        b=table['bbox']
        if any(b[0]<r['bbox'][2] and b[2]>r['bbox'][0] and b[1]<r['bbox'][3] and b[3]>r['bbox'][1] for r in ruled_tables):continue
        output.append(table)
        if len(output)>=8:break
    return output
