"""Explainable page observations; no semantic quality score or automatic rewrite."""
def inspect(page):
    text=page.get('text','');bad=sum(ch=='\ufffd' or ord(ch)<32 and ch not in '\n\r\t' for ch in text)
    ratio=bad/max(1,len(text))
    page['quality_checks']=[{'name':'原件旋转','value':page.get('rotation',0),'status':'observed'},
        {'name':'可读取文字层','value':bool(text.strip()),'status':'observed'},
        {'name':'异常字符比例','value':round(ratio,4),'count':bad,'status':'needs_review' if bad>=2 and ratio>.02 else 'observed'},
        {'name':'待确认文字块','value':sum(b.get('status','confirmed')!='confirmed' for b in page.get('blocks',[])),'status':'observed'}]
    if bad>=2 and ratio>.02:
        issues=page.setdefault('issues',[])
        if not any(isinstance(i,dict) and i.get('code')=='damaged_text_layer' for i in issues):
            issues.append({'code':'damaged_text_layer','message':f'原文字层含 {bad} 个替换或控制字符（{ratio:.1%}）；请对照原件重读、识别或人工复核'})
        for block in page.get('blocks',[]):
            if any(ch=='\ufffd' or ord(ch)<32 and ch not in '\n\r\t' for ch in block.get('text','')):
                block.update(status='pending',source_pending_reason='damaged_text_layer')
    page['text_available']=any(b.get('status','confirmed')=='confirmed' and b.get('text','').strip() for b in page.get('blocks',[]))
    return page
