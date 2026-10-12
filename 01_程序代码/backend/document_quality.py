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
    notes=[]
    if scanned>=.2:notes.append(f"约 {scanned:.0%} 的页面没有可用文字，建议先在正式解析中补齐这些页")
    if tables>=1 or image_area>=.25:notes.append("表格或图片占比较大；需要结构化表格与图表数值时，可另选视觉服务，但须先预览外发内容并确认")
    if not headings:notes.append("没有识别到目录层级，长文档的定位与切片上下文会较弱")
    return {"score":score,"level":level,"grade":grade,"factors":factors,
            "suggested_mode":"ocr" if scanned>=.2 else "native","notes":notes,
            "rules":"七项可测量特征按固定权重加权（合计 100）：无文字层页面 30、图像面积占比 15、表格密度 15、版式碎片化 10、缺少目录层级 10、页面旋转 10、文字层损坏 10；分档为 低<25≤中<55≤高。",
            "boundary":"复杂度只描述解析难度，取自解析结果的可测量特征，不含语义判断，也不预测问答准确率或内容质量。"}


def inspect(page):
    text=page.get('text','');bad=sum(ch=='\ufffd' or ord(ch)<32 and ch not in '\n\r\t' for ch in text)
    ratio=bad/max(1,len(text))
    page['quality_checks']=[{'name':'原件旋转','value':page.get('rotation',0),'status':'observed'},
        {'name':'可读取文字层','value':bool(text.strip()),'status':'observed'},
        {'name':'异常字符比例','value':round(ratio,4),'count':bad,'status':'needs_review' if bad>=2 and ratio>.02 else 'observed'},
        {'name':'待确认文字块','value':sum(b.get('status','confirmed')!='confirmed' for b in page.get('blocks',[])),'status':'observed'}]
    formula_pending=sum(f.get('status') not in {'confirmed','superseded'} for f in page.get('formulas',[]))
    page['issues']=[i for i in page.get('issues',[]) if not isinstance(i,dict) or i.get('code')!='formula_structure_incomplete']
    page['quality_checks'].append({'name':'公式结构待复核','value':formula_pending,'status':'needs_review' if formula_pending else 'observed'})
    if formula_pending:
        issues=page.setdefault('issues',[])
        if not any(isinstance(i,dict) and i.get('code')=='formula_structure_incomplete' for i in issues):
            issues.append({'code':'formula_structure_incomplete','message':'二维公式的分母、上下标或求和范围尚未完整核验；已保存空间结构和原图位置，待复核内容不作为确定证据'})
    if bad>=2 and ratio>.02:
        issues=page.setdefault('issues',[])
        if not any(isinstance(i,dict) and i.get('code')=='damaged_text_layer' for i in issues):
            issues.append({'code':'damaged_text_layer','message':f'原文字层含 {bad} 个替换或控制字符（{ratio:.1%}）；请对照原件重读、识别或人工复核'})
        for block in page.get('blocks',[]):
            if any(ch=='\ufffd' or ord(ch)<32 and ch not in '\n\r\t' for ch in block.get('text','')):
                block.update(status='pending',source_pending_reason='damaged_text_layer')
    page['text_available']=any(b.get('status','confirmed')=='confirmed' and b.get('text','').strip() for b in page.get('blocks',[]))
    return page
