"""Local human corrections publish new parses; original and historical parses stay immutable."""
from __future__ import annotations
import copy
import json
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import Field
from . import connections, documents, document_native as native, store as s

router=APIRouter(prefix='/api/documents')


class Correction(connections.Strict):
    version:str=Field(pattern=r'^[a-f0-9]{64}$')
    parse_id:str=Field(min_length=1,max_length=100)
    page:int=Field(ge=1)
    target:str=Field(min_length=1,max_length=300)
    operator:str=Field(min_length=1,max_length=80)
    text:str=Field(default='',max_length=4000)
    label:str=Field(default='',max_length=300)
    series_name:str=Field(default='',max_length=300)
    unit:str=Field(default='',max_length=80)
    title:str=Field(default='',max_length=300)
    x_label:str=Field(default='',max_length=200)
    y_label:str=Field(default='',max_length=200)
    accepted:Literal[True]


def targets(page):
    items=[]
    for b in page.get('blocks',[]):
        if b.get('table_id') or b.get('chart_id') or b.get('source_pending_reason')=='unverified_chart_association':continue
        items.append({'id':'block:'+b['id'],'kind':'formula' if b.get('formula_id') or b.get('formula_ids') else 'text','text':b['text'],'box':b.get('box'),'media_id':b.get('media_id'),'status':b.get('status','confirmed')})
    for i,b in enumerate(page.get('recognition_pending',[])):
        if b.get('source_pending_reason')=='formula_structure_requires_review':continue
        items.append({'id':'pending:'+str(i),'kind':'text','text':b.get('text',''),'box':b.get('box'),'media_id':b.get('media_id'),'status':'pending'})
    for t in page.get('tables',[]):
        for c in t.get('cells',[]):
            candidates=list(dict.fromkeys(text for r in page.get('label_reviews',[]) if r.get('media_id')==t.get('media_id') and r.get('observation_id') in c.get('evidence_lines',[]) for text in r.get('candidates',[]) if isinstance(text,str) and text.strip()))
            items.append({'id':'cell:'+t['id']+':'+c['id'],'kind':'cell','text':c.get('text',''),
                          'ocr_candidates':candidates,
                          'label':f"{t.get('title') or '表格'} · 第 {c['row']+1} 行 {c['column']+1} 列",'box':c.get('box') or t.get('box'),'media_id':t.get('media_id'),'status':c.get('status','pending')})
    for chart in page.get('charts',[]):
        for si,series in enumerate(chart.get('series',[])):
            for p in series.get('points',[]):
                items.append({'id':'point:'+chart['id']+':'+str(si)+':'+p['id'],'kind':'chart_point',
                    'text':str(p['value']) if p.get('value') is not None else '', 'label':p.get('label') or '',
                    'series_name':series.get('name') or '', 'unit':chart.get('unit') or '', 'title':chart.get('title') or '',
                    'x_label':chart.get('x_label') or '', 'y_label':chart.get('y_label') or '',
                    'box':p.get('box') or chart.get('box'),'media_id':chart.get('media_id'),'status':p.get('status','pending')})
    return items


def _rewrite(page, changes):
    """Apply nonoverlapping block edits while keeping unrelated source offsets and boxes."""
    edits=sorted(changes, key=lambda x:x[0]);old=page['text'];cursor=0;parts=[]
    for start,end,text in edits:
        if start<cursor:raise ValueError('修改范围重叠')
        parts.extend((old[cursor:start],text));cursor=end
    parts.append(old[cursor:]);page['text']=''.join(parts)
    def offset(n):return n+sum(len(text)-(end-start) for start,end,text in edits if end<=n)
    for b in page['blocks']:
        found=next((e for e in edits if e[0]==b['start'] and e[1]==b['end']),None)
        if found:
            start=offset(b['start']);b.update(start=start,end=start+len(found[2]),text=found[2],chars=[])
        else:
            b['start']=offset(b['start']);b['end']=offset(b['end'])
            for c in b.get('chars',[]):c['start']=offset(c['start']);c['end']=offset(c['end'])
    for h in page.get('headings',[]):
        h['start']=offset(h['start']);h['end']=offset(h['end'])
    for table in page.get('tables',[]):
        for r in table.get('text_ranges',[]):r['start']=offset(r['start']);r['end']=offset(r['end'])


def _drop(page,predicate):
    removed=[b for b in page['blocks'] if predicate(b)]
    _rewrite(page,[(b['start'],b['end'],'') for b in removed])
    page['blocks']=[b for b in page['blocks'] if not predicate(b)]


def publish(did,req):
    if not req.operator.strip():raise ValueError('请填写操作者标识')
    with documents.LOCK:
        data=documents.layout(did,req.version,req.parse_id)
        if not data['document']['is_current'] or data['document']['archived']:raise ValueError('只能复核未归档的当前原件')
        if documents.current_parse_id(did,req.version)!=req.parse_id:raise ValueError('基础解析已变化，请重新打开当前页')
        if req.page>len(data['pages']):raise ValueError('页码超出范围')
        body={k:copy.deepcopy(v) for k,v in data.items() if k not in {'document','parse_id'}}
        page=body['pages'][req.page-1];item=next((t for t in targets(page) if t['id']==req.target),None)
        if not item:raise ValueError('复核对象不在固定解析中')
        before=copy.deepcopy(item);audit={'operator':req.operator.strip(),'time':s.now(),'page':req.page,'target':req.target,
            'base_parse_id':req.parse_id,'before':before,'after':None,'confirmation':'用户声明已对照原件确认；不代表系统独立核验'}
        mark={'status':'confirmed','verification':'human_reviewed','human_reviewed':True,'reviewed_by':req.operator.strip(),'reviewed_at':audit['time']}
        if req.target.startswith('block:'):
            b=next(b for b in page['blocks'] if 'block:'+b['id']==req.target)
            if not req.text.strip():raise ValueError('文字修正不能为空')
            _rewrite(page,[(b['start'],b['end'],req.text)])
            b.update(**mark,source_method='human_review',location_precision='region' if b.get('box') else 'text_range')
            if b.get('formula_ids'):
                for fid in b['formula_ids']:
                    formula=next(f for f in page['formulas'] if f['id']==fid)
                    formula.update(status='superseded',verification='human_reviewed_paragraph',human_reviewed=True,reviewed_by=req.operator.strip(),replacement_block_id=b['id'],reason=None)
                b.pop('source_pending_reason',None)
            if b.get('formula_id'):
                formula=next(f for f in page['formulas'] if f['id']==b['formula_id'])
                formula.update(text=req.text,latex=req.text,reason=None,**mark)
                page['recognition_pending']=[l for l in page.get('recognition_pending',[])
                    if not (l.get('media_id')==formula.get('media_id') and l.get('observation_id') in formula.get('evidence_lines',[]))]
                b.pop('source_pending_reason',None)
        elif req.target.startswith('pending:'):
            i=int(req.target.split(':',1)[1]);line=page['recognition_pending'].pop(i)
            if not req.text.strip():raise ValueError('文字修正不能为空')
            native._append(page,req.text,'text',line.get('box'),**mark,source_method='human_review',media_id=line.get('media_id'),location_precision='line')
        elif req.target.startswith('cell:'):
            table=next(t for t in page['tables'] if req.target.startswith('cell:'+t['id']+':'))
            cell=next(c for c in table['cells'] if req.target=='cell:'+table['id']+':'+c['id'])
            cell.update(text=req.text,**mark)
            if cell.get('label_review'):
                indices=cell.get('evidence_lines',[]);media_id=table.get('media_id')
                affected=[b for b in page['blocks'] if b.get('source_method')=='ocr' and b.get('media_id')==media_id and b.get('observation_id') in indices]
                _rewrite(page,[(b['start'],b['end'],req.text) for b in affected])
                for b in affected:b.update(**mark,source_method='human_review')
                for observation in page.get('ocr_lines',[]):
                    if observation.get('media_id')==media_id and observation.get('observation_id') in indices:
                        observation.update(before_human_text=observation['text'],text=req.text,**mark)
                page['recognition_pending']=[l for l in page.get('recognition_pending',[]) if not (l.get('media_id')==media_id and l.get('observation_id') in indices)]
            table['status']='confirmed' if all(c.get('status')=='confirmed' for c in table['cells']) else 'pending'
            _drop(page,lambda b:b.get('table_id')==table['id'])
            table['text_ranges']=[];native._table_rows(page,table,'human_review')
            for b in page['blocks']:
                if b.get('table_id')==table['id']:
                    b['human_reviewed']=all(c.get('human_reviewed') is True for c in table['cells'] if c['id'] in b.get('cell_ids',[]))
        else:
            chart=next(c for c in page['charts'] if req.target.startswith('point:'+c['id']+':'))
            prefix='point:'+chart['id']+':';si,pid=req.target[len(prefix):].split(':',1)
            series=chart['series'][int(si)];point=next(p for p in series['points'] if p['id']==pid)
            from decimal import Decimal,InvalidOperation
            try:
                number=Decimal(req.text)
                if not number.is_finite():raise InvalidOperation
            except InvalidOperation:raise ValueError('图点需要可核对的有限数值；无法读取时保留待复核') from None
            if not all(v.strip() for v in [req.label,req.series_name,req.unit,req.title,req.x_label,req.y_label]):raise ValueError('请确认图点类别、系列、单位、标题和两轴对应关系')
            point.update(value=str(number),label=req.label,printed_text=req.text,value_source='human_review',association_status='confirmed',**mark)
            series.update(name=req.series_name,name_status='confirmed',axis='y',human_reviewed=True)
            chart.update(title=req.title,unit=req.unit,x_label=req.x_label,y_label=req.y_label,human_reviewed=True,
                         metadata_status={k:'confirmed' for k in ['title','unit','x_label','y_label']})
            series['status']='confirmed' if all(p.get('status')=='confirmed' for p in series['points']) else 'pending'
            chart['status']='confirmed' if all(s.get('status')=='confirmed' for s in chart['series']) else 'pending'
            _drop(page,lambda b:b.get('chart_id')==chart['id']);native._chart_text(page,chart,'human_review')
            for b in page['blocks']:
                if b.get('chart_id')==chart['id']:
                    b['human_reviewed']=any(p.get('human_reviewed') is True for series in chart['series'] for p in series['points'] if p['id']==b.get('point_id'))
        audit['after']={k:getattr(req,k) for k in ['text','label','series_name','unit','title','x_label','y_label']}
        body.setdefault('human_reviews',[]).append(audit)
        page.setdefault('provenance',{})['human_reviewed_targets']=sum(r['page']==req.page for r in body['human_reviews'])
        page['text_available']=any(b.get('status','confirmed')=='confirmed' and b.get('text','').strip() for b in page['blocks'])
        page['ocr_required']=not page['text_available']
        from .document_quality import inspect
        inspect(page)
        body['parser']='human-review-v1'
        with s.conn() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT sha256,status FROM documents WHERE id=?',(did,)).fetchone()
            active=documents.active_parse(db,did,req.version)
            if not current or current['sha256']!=req.version or current['status']=='archived' or not active or active['id']!=req.parse_id:raise ValueError('来源在提交期间变化，未保存修改')
            if db.execute("SELECT 1 FROM ef_document_processing_tasks WHERE document_id=? AND status IN ('queued','running')",(did,)).fetchone():raise ValueError('文档正在解析，请完成或取消后复核')
            after=documents.save_parse(db,did,req.version,body,'人工复核：'+req.operator.strip(),req.page)
            current_document=db.execute('SELECT sha256,quality_json FROM documents WHERE id=?',(did,)).fetchone()
            quality=s.retain_provenance(s.quality_for_parse(body),current_document)
            db.execute('UPDATE documents SET status=?,quality_json=?,updated_at=? WHERE id=?',('needs_review' if quality['quality_flags'] else 'ready',json.dumps(quality,ensure_ascii=False),s.now(),did))
            db.execute('DELETE FROM blocks WHERE document_id=?',(did,))
            for i,b in enumerate(s.extract(None,body)):
                db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?)',(did+'-'+str(i),did,b['page'],b['title_path'],b['content_type'],b['text'],json.dumps(b['bbox']),json.dumps(b['quality']),s.now()))
        result={'before_parse_id':req.parse_id,'parse_id':after['parse_id'],'sha256':req.version,'page':req.page,'review':audit}
        try:
            from . import rag_index
            job=rag_index.submit_index([did]);result['index_job_id']=job['id']
        except Exception:result['index_error']='复核已保存，索引提交失败；可在资料库重新建立该文档索引'
        return result


@router.get('/{did}/review')
def preview(did:str,page:int,version:str,parse_id:str):
    try:
        data=documents.layout(did,version,parse_id)
        if not 1<=page<=len(data['pages']):raise ValueError('页码超出范围')
        return {'targets':targets(data['pages'][page-1]),'reviews':[r for r in data.get('human_reviews',[]) if r['page']==page]}
    except ValueError as exc:raise HTTPException(400,str(exc)) from None


@router.post('/{did}/review')
def correct(did:str,req:Correction):
    try:return publish(did,req)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None
