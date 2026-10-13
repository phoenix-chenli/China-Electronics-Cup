"""Explainable page observations and a deterministic document-complexity estimate.

Nothing here scores meaning or accuracy. Every number is a measurable property
of the parse result, and the assessment reports both the rule that produced it
and the factors that contributed, so a reader can disagree with one component
instead of with a single opaque figure.
"""


def _area(box):
    if not isinstance(box,(list,tuple)) or len(box)!=4:return 0.0
    try:return max(0.0,float(box[2])-float(box[0]))*max(0.0,float(box[3])-float(box[1]))
    except (TypeError,ValueError):return 0.0


def _capped(value,ceiling):
    return round(min(1.0,max(0.0,value/ceiling)),4) if ceiling else 0.0


def assess(pages, outline=None):
    """Estimate how hard a document is to parse, and which local mode suits it.

    The score is a weighted sum of measurable parse features only; it never
    judges content quality, correctness or answer accuracy. ``suggested_mode``
    deliberately stays inside the local modes, because recommending an outbound
    service would require its own preview-and-consent step.
    """
    pages=list(pages or [])
    total=max(1,len(pages))
    def count(predicate):return sum(1 for p in pages if predicate(p))
    scanned=count(lambda p:not p.get("text_available"))/total
    rotated=count(lambda p:p.get("rotation"))/total
    def damaged_page(p):return any(isinstance(i,dict) and i.get("code")=="damaged_text_layer" for i in p.get("issues",[]))
    damaged=count(damaged_page)/total
    image_area=sum(min(1.0,sum(_area(i.get("box")) for i in p.get("images",[]))) for p in pages)/total
    tables=sum(len(p.get("tables",[])) for p in pages)/total
    blocks=sum(len(p.get("blocks",[])) for p in pages)/total
    headings=len(outline) if outline is not None else sum(len(p.get("headings",[])) for p in pages)
    table_severity=_capped(tables,2.5)
    fragment_severity=_capped(blocks,40)
    factors=[
        {"name":"无文字层页面","weight":30,"severity":round(scanned,4),"contribution":round(scanned*30,1),
         "detail":f"{count(lambda p:not p.get('text_available'))}/{total} 页没有可用文字"},
        {"name":"图像面积占比","weight":15,"severity":round(image_area,4),"contribution":round(image_area*15,1),
         "detail":"页面图片框面积占比；没有坐标的嵌图不计入"},
        {"name":"表格密度","weight":15,"severity":table_severity,"contribution":round(table_severity*15,1),
         "detail":f"平均每页 {tables:.2f} 张表格"},
        {"name":"版式碎片化","weight":10,"severity":fragment_severity,"contribution":round(fragment_severity*10,1),
         "detail":f"平均每页 {blocks:.1f} 个文本块"},
        {"name":"缺少目录层级","weight":10,"severity":1.0 if not headings else 0.0,"contribution":0.0 if headings else 10.0,
         "detail":f"目录条目 {headings} 条"},
        {"name":"页面旋转","weight":10,"severity":round(rotated,4),"contribution":round(rotated*10,1),
         "detail":f"{count(lambda p:p.get('rotation'))}/{total} 页带有旋转"},
        {"name":"文字层损坏","weight":10,"severity":round(damaged,4),"contribution":round(damaged*10,1),
         "detail":f"{count(damaged_page)}/{total} 页含替换或控制字符"},
    ]
    score=round(sum(factor["contribution"] for factor in factors))
    level="低" if score<25 else "中" if score<55 else "高"
    grade="low" if score<25 else "medium" if score<55 else "high"
    measured={p["number"]:p["quality_signals"]["sharpness"] for p in pages
              if isinstance(p.get("quality_signals"),dict) and isinstance(p["quality_signals"].get("sharpness"),(int,float))}
    signals_by_number={p['number']:p['quality_signals'] for p in pages if isinstance(p.get('quality_signals'),dict)}
    blurry=[]; comparable={}
    for number,value in measured.items():
        signal=signals_by_number[number]
        ink=signal.get('ink_ratio')
        if signal.get('blank') or ink is None:continue
        peers=[v for n,v in measured.items() if n!=number
            and not signals_by_number[n].get('blank')
            and isinstance(signals_by_number[n].get('ink_ratio'),(int,float))
            and .5*max(ink,.002)<=signals_by_number[n]['ink_ratio']<=2*max(ink,.002)]
        if len(peers)<2:continue
        peers.sort();median=peers[len(peers)//2];comparable[number]=len(peers)
        if value<median*SHARPNESS_RATIO:blurry.append(number)
    notes=[]
    if scanned>=.2:notes.append(f"约 {scanned:.0%} 的页面没有可用文字，建议先在正式解析中补齐这些页")
    if tables>=1 or image_area>=.25:notes.append("表格或图片占比较大；需要结构化表格与图表数值时，可另选视觉服务，但须先预览外发内容并确认")
    if blurry:notes.append("第 "+"、".join(str(n) for n in blurry[:8])+" 页的插图清晰度明显低于本文档其他页面，建议对照原件确认")
    if not headings:notes.append("没有识别到目录层级，长文档的定位与切片上下文会较弱")
    return {"score":score,"level":level,"grade":grade,"factors":factors,
            "suggested_mode":"ocr" if scanned>=.2 else "native","notes":notes,
            "blurry_pages":blurry,"sharpness_measured_pages":len(measured),"sharpness_comparable_pages":comparable,
            "rules":"七项可测量特征按固定权重加权（合计 100）：无文字层页面 30、图像面积占比 15、表格密度 15、版式碎片化 10、缺少目录层级 10、页面旋转 10、文字层损坏 10；分档为 低<25≤中<55≤高。清晰度不参与评分：它排除空白页，仅比较墨迹密度在0.5至2倍范围内的至少两张其他页面，以其清晰度中位数为基准，低于 "+str(SHARPNESS_RATIO)+" 倍才列为可疑页，绝对值只保留 "+str(SHARPNESS_FLOOR)+" 的灾难性下限。",
            "boundary":"复杂度只描述解析难度，取自解析结果的可测量特征，不含语义判断，也不预测问答准确率或内容质量。"}


SKEW_LIMIT = 1.5
# Absolute sharpness floor only catches a catastrophic image. Across documents
# edge variance follows how much ink a page carries, so real blur is judged
# against the document's own pages in assess(), not against a fixed number.
SHARPNESS_FLOOR = 600.0
SHARPNESS_RATIO = 0.45
SIGNAL_SIDE = 400
SKEW_LIMIT_DEGREES = 8.0
# Issue codes this module owns. They are rebuilt from the page on every call,
# so re-inspecting an already inspected page (OCR on a parsed page, a human
# correction of a stored parse) cannot accumulate duplicates.
MANAGED_ISSUES = frozenset({"formula_structure_incomplete","damaged_text_layer","page_rotated",
                            "page_upside_down","page_skewed","low_image_sharpness","traditional_characters"})


_TRADITIONAL = None


def _traditional_table():
    """The bounded 繁→简 table, cached, and never allowed to break a parse."""
    global _TRADITIONAL
    if _TRADITIONAL is None:
        try:
            from . import query_normalize
            _TRADITIONAL=query_normalize._table()
        except Exception:
            _TRADITIONAL={}
    return _TRADITIONAL


def text_skew(page):
    """Median baseline angle of lines that carry real glyph boxes, else None.

    Only formats that expose per-character geometry can be measured this way;
    an axis-aligned OCR line box says nothing about skew, so it is not used.
    """
    import math
    width=max(1.0,float(page.get("width") or 1));height=max(1.0,float(page.get("height") or 1))
    angles=[]
    for block in page.get("blocks",[]):
        baselines=block.get("baseline_angles")
        if baselines:
            angles.extend(a for a in baselines if isinstance(a,(int,float)) and abs(a)<15)
            continue
        if "\n" in block.get("text", "").strip():continue
        chars=[c for c in block.get("chars",[]) if isinstance(c.get("box"),(list,tuple)) and len(c["box"])==4]
        if len(chars)<8:continue
        first,last=chars[0],chars[-1]
        dx=((last["box"][0]+last["box"][2])-(first["box"][0]+first["box"][2]))/2*width
        dy=((last["box"][1]+last["box"][3])-(first["box"][1]+first["box"][3]))/2*height
        if dx<=1:continue
        angles.append(math.degrees(math.atan2(dy,dx)))
    if not angles:return None
    angles.sort()
    return round(angles[len(angles)//2],2)


def _projection_skew(gray):
    """The rotation that makes text rows sharpest, by projection variance."""
    import numpy as np
    from PIL import Image
    span=float(gray.max()-gray.min())
    if span<=0:return None
    ink=((gray-gray.min())/span<.5).astype(np.uint8)*255
    if int((ink>0).sum())<50:return None
    image=Image.fromarray(ink)
    best,best_score=None,-1.0
    angle=-SKEW_LIMIT_DEGREES
    while angle<=SKEW_LIMIT_DEGREES+1e-9:
        rows=np.asarray(image.rotate(angle,resample=Image.BILINEAR,fillcolor=0),dtype=np.float32).sum(axis=1)
        score=float(rows.var())
        if score>best_score:best_score,best=score,angle
        angle+=1.0
    return None if best is None else round(float(best),2)


def page_signals(raw):
    """Advisory raster measurements; never raises and never blocks a parse."""
    try:
        import io
        import numpy as np
        from PIL import Image, ImageFilter
        with Image.open(io.BytesIO(raw)) as source:
            image=source.convert("L")
            if max(image.size)>SIGNAL_SIDE:
                scale=SIGNAL_SIDE/max(image.size)
                image=image.resize((max(1,int(image.width*scale)),max(1,int(image.height*scale))))
            gray=np.asarray(image,dtype=np.float32)
            # Edge variance is a sharpness *hint*: it falls with blur and with
            # clean blank pages alike, so it is reported with its threshold and
            # never as proof that the content is unusable.
            sharpness=float(np.asarray(image.filter(ImageFilter.FIND_EDGES),dtype=np.float32)[2:-2,2:-2].var())
            ink_ratio=float((gray<180).mean())
        return {"sharpness":round(sharpness,2),"ink_ratio":round(ink_ratio,4),"blank":ink_ratio<.002,
                "deskew_degrees":_projection_skew(gray),"measurement":"raster-v2-cropped-edges"}
    except Exception:
        # Advisory only: a format that cannot be measured reports nothing rather
        # than failing the document.
        return {}


def inspect(page):
    text=page.get('text','');bad=sum(ch=='\ufffd' or ord(ch)<32 and ch not in '\n\r\t' for ch in text)
    ratio=bad/max(1,len(text))
    rotation=page.get('rotation',0) or 0
    signals=page.get('quality_signals') or {}
    deskew=signals.get('deskew_degrees')
    if deskew is None:
        baseline=text_skew(page)
        deskew=None if baseline is None else round(-baseline,2)
    sharpness=signals.get('sharpness')
    table=_traditional_table()
    traditional=sorted({ch for ch in text if ch in table})
    page['quality_checks']=[{'name':'原件旋转','value':rotation,'status':'needs_review' if rotation else 'observed'},
        {'name':'可读取文字层','value':bool(text.strip()),'status':'observed'},
        {'name':'异常字符比例','value':round(ratio,4),'count':bad,'status':'needs_review' if bad>=2 and ratio>.02 else 'observed'},
        {'name':'待确认文字块','value':sum(b.get('status','confirmed')!='confirmed' for b in page.get('blocks',[])),'status':'observed'}]
    formula_pending=sum(f.get('status') not in {'confirmed','superseded'} for f in page.get('formulas',[]))
    page['issues']=[i for i in page.get('issues',[]) if not isinstance(i,dict) or i.get('code') not in MANAGED_ISSUES]
    page['quality_checks'].append({'name':'公式结构待复核','value':formula_pending,'status':'needs_review' if formula_pending else 'observed'})
    if formula_pending:
        issues=page.setdefault('issues',[])
        if not any(isinstance(i,dict) and i.get('code')=='formula_structure_incomplete' for i in issues):
            issues.append({'code':'formula_structure_incomplete','message':'二维公式的分母、上下标或求和范围尚未完整核验；已保存空间结构和原图位置，待复核内容不作为确定证据'})
    # Orientation and image-quality alarms. Each is a measured value or an
    # explicit "not measured"; none of them rewrites the original, and none
    # reports a page as healthy without evidence.
    page['quality_checks'].append({'name':'页面颠倒','value':'旋转元数据 180°' if rotation==180 else '文字朝向未测量（仅检查旋转元数据）',
        'status':'needs_review' if rotation==180 else 'not_checked'})
    page['quality_checks'].append({'name':'页面倾斜','value':None if deskew is None else deskew,
        'status':'needs_review' if deskew is not None and abs(deskew)>=SKEW_LIMIT else 'observed'})
    page['quality_checks'].append({'name':'插图清晰度','value':sharpness,
        'status':'needs_review' if sharpness is not None and not signals.get('blank') and sharpness<SHARPNESS_FLOOR else 'observed'})
    page['quality_checks'].append({'name':'繁体字','value':len(traditional),
        'status':'needs_review' if traditional else 'observed'})
    issues=page.setdefault('issues',[])
    if rotation:
        issues.append({'code':'page_rotated','message':f'原页带有 {rotation}° 旋转元数据；定位框按显示方向计算，识别或阅读前请确认方向是否正确'})
    if rotation==180:
        issues.append({'code':'page_upside_down','message':'页面旋转元数据为 180°，文字方向可能颠倒；请在识别或视觉解析前确认，并在 0/90/180/270 中选择正确的输入方向'})
    if deskew is not None and abs(deskew)>=SKEW_LIMIT:
        issues.append({'code':'page_skewed','message':f'文字行约有 {abs(deskew):.1f}° 倾斜（使行水平约需旋转 {deskew:+.1f}°）；定位框仍按当前页面计算，识别或阅读前建议先校正'})
    if sharpness is not None and not signals.get('blank') and sharpness<SHARPNESS_FLOOR:
        issues.append({'code':'low_image_sharpness','message':f'插图清晰度提示偏低（边缘方差 {sharpness}，阈值 {SHARPNESS_FLOOR:g}）；低内容密度也可能得到低值，空白页不触发此告警，该值不代表内容不可用，可对照原件重读或改用视觉服务'})
    if traditional:
        issues.append({'code':'traditional_characters','message':'正文含 '+str(sum(1 for ch in text if ch in table))+' 个繁体字（'+'、'.join(traditional[:12])+'）；系统不自动改写原文，可在“对照原件复核”中确认后保存新解析'})
    if bad>=2 and ratio>.02:
        if not any(isinstance(i,dict) and i.get('code')=='damaged_text_layer' for i in issues):
            issues.append({'code':'damaged_text_layer','message':f'原文字层含 {bad} 个替换或控制字符（{ratio:.1%}）；请对照原件重读、识别或人工复核'})
        for block in page.get('blocks',[]):
            if any(ch=='\ufffd' or ord(ch)<32 and ch not in '\n\r\t' for ch in block.get('text','')):
                block.update(status='pending',source_pending_reason='damaged_text_layer')
    page['text_available']=any(b.get('status','confirmed')=='confirmed' and b.get('text','').strip() for b in page.get('blocks',[]))
    return page
