"""Bounded OOXML/native structures and provenance-preserving derived views."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'm':'http://schemas.openxmlformats.org/officeDocument/2006/math',
    'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
    'c':'http://schemas.openxmlformats.org/drawingml/2006/chart'}
W='{'+NS['w']+'}';R='{'+NS['r']+'}'
PARSER='source-layout-v2'
MAX_TEXT=2_000_000


def _package(path):
    archive=zipfile.ZipFile(path)
    infos=archive.infolist()
    if len(infos)>2048 or sum(x.file_size for x in infos)>128*1024*1024:
        archive.close();raise ValueError('DOCX 展开体积或条目数超过限制')
    names=set()
    for item in infos:
        name=item.filename
        if name in names or '\\' in name or name.startswith('/') or '..' in name.split('/') or item.flag_bits&1 or ((item.external_attr>>16)&0o170000)==0o120000:
            archive.close();raise ValueError('DOCX 包路径或条目无效')
        names.add(name)
    if 'word/document.xml' not in names:
        archive.close();raise ValueError('文件不是有效的 DOCX 文档')
    return archive


def _xml(archive,name,optional=False):
    if name not in archive.namelist():
        if optional:return None
        raise ValueError('DOCX 缺少必要结构')
    data=archive.read(name)
    if len(data)>12*1024*1024 or b'<!DOCTYPE' in data.replace(b'\x00',b'').upper() or b'<!ENTITY' in data.replace(b'\x00',b'').upper():
        raise ValueError('DOCX XML 超限或含不支持的实体')
    try:root=ET.fromstring(data)
    except ET.ParseError:raise ValueError('DOCX XML 损坏') from None
    stack=[(root,0)];count=0
    while stack:
        node,depth=stack.pop();count+=1
        if depth>64 or count>150000:raise ValueError('DOCX XML 层级或节点数超限')
        stack.extend((child,depth+1) for child in node)
    return root


def _relations(archive,part):
    parent,name=posixpath.split(part);file=parent+'/_rels/'+name+'.rels'
    root=_xml(archive,file,True);out={}
    if root is None:return out
    for node in root:
        if node.get('TargetMode','').lower()=='external':continue
        target=node.get('Target','')
        if ':' in target or '\\' in target:continue
        resolved=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(parent,target))
        if resolved.startswith('../') or resolved not in archive.namelist():continue
        out[node.get('Id')]=resolved
    return out


def _paragraph(node):
    text=[]
    def visible(parent):
        if parent.tag in {W+'del',W+'moveFrom',W+'drawing',W+'pict',W+'object',W+'tbl',W+'instrText'}:return
        yield parent
        if parent.tag=='{'+NS['m']+'}oMath':return
        for child in parent:yield from visible(child)
    for child in visible(node):
        if child.tag=='{'+NS['m']+'}oMath':
            from .math_structure import latex
            try:text.append(' $'+latex(child)+'$ ')
            except ValueError:text.append(' [公式结构需复核] ')
        elif child.tag in {W+'t',W+'delText'}:
            if child.tag==W+'t':text.append(child.text or '')
        elif child.tag==W+'tab':text.append('\t')
        elif child.tag in {W+'br',W+'cr'}:text.append('\n')
    return ''.join(text)


def _page(number):
    return {'number':number,'width':595,'height':842,'rotation':0,'text':'','text_available':False,
        'ocr_required':False,'vision_verified':False,'blocks':[],'images':[],'headings':[],'tables':[],'charts':[],
        'page_kind':'logical_section','page_label':'逻辑节（非 Word 实际分页）','issues':[],
        'provenance':{'method':'docx_native','location_precision':'package_structure','human_reviewed':False}}


def _append(page,text,kind='text',box=None,**metadata):
    if not text:return None
    if len(page['text'])+len(text)>MAX_TEXT:raise ValueError('解析文字超过限制')
    start=len(page['text']);page['text']+=text+'\n\n'
    block={'id':f"p{page['number']}-derived-{uuid.uuid4().hex[:16]}",'start':start,'end':start+len(text),
        'text':text,'kind':kind,'box':box,'chars':[],**metadata}
    page['blocks'].append(block)
    if text.strip() and metadata.get('status','confirmed')=='confirmed':page['text_available']=True
    return block


def _table_rows(page,table,method):
    title=table.get('title') if table.get('title_status','confirmed')=='confirmed' else None
    title=title or '表格结构视图'
    if table.get('status')=='pending' and not any(c.get('label_review') for c in table.get('cells',[])):return
    headers=[cell for cell in table.get('cells',[]) if cell.get('role') in {'header','column_header'} and cell.get('text')]
    if any(h.get('status','confirmed')!='confirmed' for h in headers):return
    for row in range(table['rows']):
        cells=[c for c in table['cells'] if c['row']<=row<c['row']+c.get('rowspan',1)]
        if not cells or any(c.get('status','confirmed')!='confirmed' for c in cells):continue
        pairs=[]
        for cell in sorted(cells,key=lambda c:c['column']):
            if not cell.get('text'):continue
            parents=[h for h in headers if h['row']<row and h['column']<=cell['column']<h['column']+h.get('colspan',1)]
            label=' / '.join(h['text'] for h in sorted(parents,key=lambda h:h['row']))
            if not label and table.get('method')=='ocr_aligned_columns':label=f"第{cell['column']+1}列"
            pairs.append((label+'：' if label else '')+cell['text'])
        if not pairs:continue
        block=_append(page,title+f' · 第{row+1}行\n'+' | '.join(pairs),'table_row',table.get('box'),
            source_method=method,status='confirmed',table_id=table['id'],cell_ids=[c['id'] for c in cells],
            derived_view=True,
            source_part=table.get('source_part'),media_id=table.get('media_id'),
            location_precision='region' if table.get('box') else 'package_structure')
        if block:table.setdefault('text_ranges',[]).append({'start':block['start'],'end':block['end'],'cell_ids':block['cell_ids']})


def _table(node,source,index,depth=0):
    if depth>6:raise ValueError('DOCX 嵌套表格超过限制')
    grid=node.find('w:tblGrid',NS);columns=len(grid) if grid is not None else 0
    cells=[];nested=[];previous={};rows=node.findall('w:tr',NS)
    if len(rows)>1000 or columns>100:raise ValueError('DOCX 表格尺寸超过限制')
    for ri,row in enumerate(rows):
        before=row.find('w:trPr/w:gridBefore',NS)
        ci=int(before.get(W+'val','0')) if before is not None else 0
        header=row.find('w:trPr/w:tblHeader',NS) is not None
        current={}
        for ti,tc in enumerate(row.findall('w:tc',NS)):
            span=tc.find('w:tcPr/w:gridSpan',NS);span=int(span.get(W+'val','1')) if span is not None else 1
            if span<1 or span>100 or ci<0 or ci+span>100:raise ValueError('DOCX 合并单元格范围无效')
            merge=tc.find('w:tcPr/w:vMerge',NS);mode=merge.get(W+'val','continue') if merge is not None else None
            text='\n'.join(_paragraph(p) for p in tc.findall('w:p',NS)).strip()
            part=f'{source}/tr[{ri+1}]/tc[{ti+1}]'
            if mode=='continue':
                origin=previous.get(ci)
                if origin is None or origin['colspan']!=span or origin['row']+origin['rowspan']!=ri:
                    raise ValueError('DOCX 纵向合并缺少对应起始单元格')
                if text:raise ValueError('DOCX 纵向续格有独立文字，需核对原文件')
                origin['rowspan']+=1
                for col in range(ci,ci+span):current[col]=origin
            else:
                if mode not in {None,'restart'}:raise ValueError('DOCX 合并类型无效')
                cell={'id':f't{index}-r{ri}-c{ci}','row':ri,'column':ci,'rowspan':1,'colspan':span,
                    'text':text,'box':None,'role':'header' if header else 'data','status':'confirmed',
                    'blank':not bool(text),'source_part':part}
                cells.append(cell)
                if mode=='restart':
                    for col in range(ci,ci+span):current[col]=cell
            for ni,child in enumerate(tc.findall('w:tbl',NS)):
                child_table=_table(child,part+f'/tbl[{ni+1}]',str(index)+'-'+str(ri)+'-'+str(ci)+'-'+str(ni),depth+1)
                child_table['parent_cell_id']=current.get(ci,cell if mode!='continue' else origin)['id'];nested.append(child_table)
            ci+=span
        columns=max(columns,ci);previous=current
    return {'id':f't{index}','title':'DOCX 表格','rows':len(rows),'columns':columns,'cells':cells,'box':None,
        'status':'confirmed','method':'docx_native','source_part':source,'nested_tables':nested}


def _chart(archive,part,index):
    root=_xml(archive,part);chart=root.find('c:chart',NS)
    if chart is None:raise ValueError('DOCX 图表结构缺失')
    title=''.join(node.text or '' for node in chart.findall('c:title//a:t',NS))
    plot=chart.find('c:plotArea',NS);series=[];kinds=[];issues=[]
    if plot is None:raise ValueError('DOCX 图表缺少绘图区')
    axes=[]
    for axis in plot:
        tag=axis.tag.split('}')[-1]
        if tag.endswith('Ax'):
            label=''.join(x.text or '' for x in axis.findall('c:title//a:t',NS))
            axisid=axis.find('c:axId',NS)
            axes.append({'id':axisid.get('val') if axisid is not None else None,'label':label,
                'logarithmic':axis.find('c:scaling/c:logBase',NS) is not None,
                'display_unit':axis.find('c:dispUnits/c:builtInUnit',NS).get('val') if axis.find('c:dispUnits/c:builtInUnit',NS) is not None else None})
        elif tag.endswith('Chart'):
            kinds.append(tag)
            axisids=[n.get('val') for n in axis.findall('c:axId',NS)]
            for ser in axis.findall('c:ser',NS):
                si=len(series)
                name=''.join(n.text or '' for n in ser.findall('c:tx//c:v',NS)) or f'系列{len(series)+1}'
                def points(node):
                    if node is None:return {}
                    result={}
                    for p in node.findall('.//c:pt',NS):
                        idx=p.get('idx');v=p.find('c:v',NS)
                        if idx is None or not idx.isdigit() or v is None or int(idx) in result:raise ValueError('DOCX 图表数据点无效或重复')
                        result[int(idx)]=v.text or ''
                    return result
                category=ser.find('c:cat',NS);value_node=ser.find('c:val',NS)
                if category is None:category=ser.find('c:xVal',NS)
                if value_node is None:value_node=ser.find('c:yVal',NS)
                cat=points(category);values=points(value_node)
                if len(values)>2000:raise ValueError('DOCX 图表数据过多')
                out=[]
                for idx,value in sorted(values.items()):
                    try:
                        number=Decimal(value)
                        if not number.is_finite():raise InvalidOperation
                    except InvalidOperation:issues.append(f'{name}有无效数值');continue
                    label=cat.get(idx)
                    status='confirmed' if label is not None else 'pending'
                    out.append({'id':f'chart{index}-s{si}-p{idx}','label':label,'value':value,'printed_text':value,
                        'box':None,'value_source':'native_cache','status':status,'source_part':part+f'/series[{si}]/point[{idx}]'})
                if not out:issues.append(f'{name}没有可用保存数据，未估算图形')
                series.append({'name':name,'axis_ids':axisids,'points':out,'color':None})
    return {'id':f'chart{index}','title':title,'kind':','.join(kinds),'box':None,'series':series,'axes':axes,
        'unit':None,'source_part':part,'method':'docx_chart_cache','status':'confirmed' if series and any(s['points'] for s in series) else 'pending','issues':issues}


def _chart_text(page,chart,method):
    if chart.get('status')=='pending':return
    for si,series in enumerate(chart.get('series',[])):
        if series.get('name_status','confirmed')!='confirmed':continue
        for point in series.get('points',[]):
            if point.get('status')!='confirmed' or point.get('value') is None or point.get('value_source') in {'estimated','unknown'}:continue
            metadata=chart.get('metadata_status',{})
            unit=(chart.get('unit') if metadata.get('unit','confirmed')=='confirmed' else None) or '单位未标明'
            title=(chart.get('title') if metadata.get('title','confirmed')=='confirmed' else None) or '图表结构视图'
            text=f"图表：{title}\n系列：{series.get('name','')}；分类：{point.get('label','')}；数值：{point['value']}；单位：{unit}"
            _append(page,text,'chart',point.get('box') or chart.get('box'),source_method=method,status='confirmed',
                derived_view=True,
                chart_id=chart['id'],point_id=point['id'],source_part=point.get('source_part') or chart.get('source_part'),
                media_id=chart.get('media_id'),
                location_precision='region' if point.get('box') or chart.get('box') else 'package_structure')


def _media(archive,part):
    from PIL import Image
    raw=archive.read(part)
    if len(raw)>15*1024*1024:raise ValueError('DOCX 嵌图超过限制')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width*image.height>50_000_000:raise ValueError('DOCX 嵌图像素超过限制')
        fmt=(image.format or '').lower();width,height=image.size
    if fmt not in {'png','jpeg','webp','gif','bmp'}:raise ValueError('DOCX 嵌图格式未支持')
    digest=hashlib.sha256(raw).hexdigest()
    return {'id':'media-'+digest[:20],'part':part,'sha256':digest,'width':width,'height':height,
        'content_type':{'jpeg':'image/jpeg','png':'image/png','webp':'image/webp','gif':'image/gif','bmp':'image/bmp'}[fmt],
        'box':None,'status':'needs_review','location_precision':'media'}


def read_docx_media(path,media_id):
    if not re.fullmatch(r'media-[a-f0-9]{20}',media_id):raise ValueError('无效的嵌图标识')
    with _package(path) as archive:
        for part in archive.namelist():
            if part.startswith('word/media/') and not part.endswith('/'):
                try:meta=_media(archive,part)
                except (ValueError,OSError):continue
                if meta['id']==media_id:return archive.read(part),meta['content_type']
    raise ValueError('这个版本没有对应嵌图')


def _ordered_children(part,prefix=''):
    for ni,node in enumerate(part):
        location=prefix+f'/{node.tag.split("}")[-1]}[{ni+1}]'
        if node.tag in {W+'del',W+'moveFrom',W+'sdtPr',W+'sdtEndPr',W+'customXmlPr'}:continue
        if node.tag in {W+'sdt',W+'sdtContent',W+'customXml',W+'ins',W+'moveTo'}:
            yield from _ordered_children(node,location)
        else:yield location,node


def parse_docx(path):
    pages=[_page(1)];titles=[];table_index=0;chart_index=0;total=0
    with _package(path) as archive:
        root=_xml(archive,'word/document.xml');body=root.find('w:body',NS)
        if body is None:raise ValueError('DOCX 正文缺失')
        styles=_xml(archive,'word/styles.xml',True);levels={}
        if styles is not None:
            for style in styles.findall('w:style',NS):
                level=style.find('w:pPr/w:outlineLvl',NS);name=style.find('w:name',NS)
                if level is not None and level.get(W+'val','').isdigit():levels[style.get(W+'styleId')]=min(6,int(level.get(W+'val'))+1)
                elif name is not None:
                    match=re.fullmatch(r'(?:heading\s*|标题\s*)([1-6])',name.get(W+'val',''),re.I)
                    if match:levels[style.get(W+'styleId')]=int(match[1])
        parts=[('word/document.xml',body)]
        for name in sorted(archive.namelist()):
            if re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml',name):
                value=_xml(archive,name)
                if 'notes.xml' in name:
                    for note in value:
                        if note.get(W+'id','0').lstrip('-').isdigit() and int(note.get(W+'id','0'))>0:parts.append((name+'/note['+note.get(W+'id')+']',note))
                else:parts.append((name,value))
        for source,part in parts:
            if source!='word/document.xml':pages.append(_page(len(pages)+1))
            rels=_relations(archive,source.split('/note[')[0])
            for suffix,node in _ordered_children(part):
                page=pages[-1];location=source+suffix
                if node.tag==W+'p':
                    text=_paragraph(node);style=node.find('w:pPr/w:pStyle',NS);outline=node.find('w:pPr/w:outlineLvl',NS)
                    level=min(6,int(outline.get(W+'val'))+1) if outline is not None and outline.get(W+'val','').isdigit() and int(outline.get(W+'val'))<9 else levels.get(style.get(W+'val')) if style is not None else None
                    if level==1 and page['text'] and source=='word/document.xml':pages.append(_page(len(pages)+1));page=pages[-1]
                    if level:titles=titles[:max(0,level-1)]+[text.strip()]
                    block=_append(page,text,'heading' if level else 'text',source_part=location,source_method='docx_native',
                        level=level or 0,
                        title_path=' / '.join(titles),status='confirmed',location_precision='package_structure')
                    formula_ids=[]
                    for fi, math_node in enumerate(node.findall('.//m:oMath',NS)):
                        from .math_structure import latex
                        try:
                            math_text=latex(math_node);math_status='confirmed';reason=None
                        except ValueError as exc:
                            math_text='';math_status='pending';reason=str(exc)
                            if block:block.update(status='pending',source_pending_reason='unsupported_office_math')
                        page.setdefault('formulas',[]).append({'id':f"docx-math-{page['number']}-{len(page.get('formulas',[]))}",
                            'text':math_text,'latex':math_text,'status':math_status,'method':'docx_omml',
                            'source_part':location+f'/math[{fi}]','original_xml':ET.tostring(math_node,encoding='unicode'),
                            'reason':reason,'location_precision':'package_structure'})
                        formula_ids.append(page['formulas'][-1]['id'])
                    if block and formula_ids:block['formula_ids']=formula_ids
                    if level and block:page['headings'].append({'title':text.strip(),'level':level,'page':page['number'],'start':block['start'],'end':block['end'],'block_id':block['id']})
                    for image in node.findall('.//a:blip',NS):
                        target=rels.get(image.get(R+'embed'))
                        if target:
                            try:
                                media=_media(archive,target)
                                if not any(x['id']==media['id'] for x in page['images']):page['images'].append(media)
                            except (ValueError,OSError):page['issues'].append('嵌图不支持或无法读取，保留原 DOCX')
                    for chart in node.findall('.//c:chart',NS):
                        target=rels.get(chart.get(R+'id'))
                        if target:
                            chart_index+=1;value=_chart(archive,target,chart_index);page['charts'].append(value);_chart_text(page,value,'docx_chart_cache')
                elif node.tag==W+'tbl':
                    table_index+=1;table=_table(node,location,table_index);page['tables'].append(table);_table_rows(page,table,'docx_native')
                    stack=list(table['nested_tables'])
                    while stack:
                        child=stack.pop(0);page['tables'].append(child);_table_rows(page,child,'docx_native');stack[0:0]=child['nested_tables']
                    # Drawings in table cells still belong to this logical section.
                    for image in node.findall('.//a:blip',NS):
                        target=rels.get(image.get(R+'embed'))
                        if target:
                            try:
                                media=_media(archive,target)
                                if not any(x['id']==media['id'] for x in page['images']):page['images'].append(media)
                            except (ValueError,OSError):page['issues'].append('表格嵌图不支持或无法读取，保留原 DOCX')
                    for chart in node.findall('.//c:chart',NS):
                        target=rels.get(chart.get(R+'id'))
                        if target:
                            chart_index+=1;value=_chart(archive,target,chart_index);page['charts'].append(value);_chart_text(page,value,'docx_chart_cache')
                elif node.tag not in {W+'sectPr',W+'bookmarkStart',W+'bookmarkEnd'}:
                    page['issues'].append('未支持的 OOXML 容器：'+node.tag.split('}')[-1]+'，原 DOCX 保留；请核对内容')
                total=sum(len(p['text']) for p in pages)
                if total>MAX_TEXT or len(pages)>200:raise ValueError('DOCX 文字或逻辑节超过限制')
        referenced={media['part'] for page in pages for media in page['images']}
        unresolved=[name for name in archive.namelist() if name.startswith('word/media/') and not name.endswith('/') and name not in referenced]
        if unresolved:
            pages[0]['issues'].append('存在未关联或无法读取的嵌图部件，保留原件供复核；不生成图片内容')
            pages[0]['unresolved_media_count']=len(unresolved)
        for page in pages:
            page['ocr_required']=not page['text_available'] and bool(page['images'] or page.get('unresolved_media_count'))
    return {'pages':pages,'outline':[h for p in pages for h in p['headings']],
        'structure':'DOCX 标题与逻辑节（非 Word 实际分页）','parser':PARSER,
        'provenance':{'method':'docx_native','tables':table_index,'charts':chart_index,'external_relationships_read':False}}


def enhance_pdf_page(path,number,base_page):
    import pymupdf
    page=copy.deepcopy(base_page);page.setdefault('tables',[]);page.setdefault('charts',[]);page.setdefault('issues',[])
    page['provenance']={'method':'pdf_native','location_precision':'character','human_reviewed':False}
    from .documents import _box
    with pymupdf.open(path) as doc:
        original=doc[number-1]
        try:found=original.find_tables()
        except Exception:page['issues'].append('原生表格结构未提取，可选择正式结构解析');return page
        for ti,source in enumerate(found.tables[:50]):
            matrix=source.extract();cells=[]
            if len(matrix)>500 or source.col_count>100:page['issues'].append('表格过大，未截断作为完整结构');continue
            for ri,row in enumerate(source.rows):
                for ci,box in enumerate(row.cells):
                    if box is None:continue
                    text=matrix[ri][ci] if ri<len(matrix) and ci<len(matrix[ri]) else None
                    cells.append({'id':f'pdf-p{number}-t{ti}-r{ri}-c{ci}','row':ri,'column':ci,'rowspan':1,'colspan':1,
                        'text':text or '','blank':text=='','box':_box(box,original),'pdf_box':list(box),
                        'role':'header' if ri==0 and source.header.names and not source.header.external else 'data','status':'confirmed' if text is not None else 'pending'})
            # Recover spans only from actual cell rectangle coverage of the grid.
            xs=sorted(set(round(v,3) for row in source.rows for box in row.cells if box for v in (box[0],box[2])))
            ys=sorted(set(round(v,3) for row in source.rows for box in row.cells if box for v in (box[1],box[3])))
            for cell in cells:
                box=cell['pdf_box'];cs=[x for x in xs if box[0]-.2<=x<=box[2]+.2];rs=[y for y in ys if box[1]-.2<=y<=box[3]+.2]
                cell['colspan']=max(1,len(cs)-1);cell['rowspan']=max(1,len(rs)-1)
            table={'id':f'pdf-p{number}-t{ti}','title':'原生 PDF 表格','box':_box(source.bbox,original),'rows':source.row_count,
                'columns':source.col_count,'cells':cells,'method':'pdf_native_table','status':'confirmed',
                'external_header':{'names':source.header.names,'box':_box(source.header.bbox,original)} if source.header.external else None}
            page['tables'].append(table);_table_rows(page,table,'pdf_native_table')
    return page


def detect_ruled_tables(raw,lines):
    """Recover bounded ruled grids; OCR observations never fill unreadable ink."""
    import cv2
    import numpy as np
    image=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    if image is None:return []
    height,width=image.shape
    if width*height>6_000_000:return []
    binary=cv2.adaptiveThreshold(image,255,cv2.ADAPTIVE_THRESH_MEAN_C,cv2.THRESH_BINARY_INV,31,12)
    horizontal=cv2.morphologyEx(binary,cv2.MORPH_OPEN,np.ones((1,max(25,width//30)),np.uint8))
    vertical=cv2.morphologyEx(binary,cv2.MORPH_OPEN,np.ones((max(25,height//30),1),np.uint8))
    merged=cv2.dilate(cv2.bitwise_or(horizontal,vertical),np.ones((3,3),np.uint8))
    contours,_=cv2.findContours(merged,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);tables=[]
    def peaks(values,threshold):
        positions=np.flatnonzero(values>threshold).tolist();groups=[]
        for pos in positions:
            if groups and pos-groups[-1][-1]<=3:groups[-1].append(pos)
            else:groups.append([pos])
        return [round(sum(g)/len(g)) for g in groups]
    for contour in sorted(contours,key=lambda c:cv2.boundingRect(c)[1]):
        x,y,w,h=cv2.boundingRect(contour)
        if w<width*.2 or h<30:continue
        xs=[x+i for i in peaks((vertical[y:y+h,x:x+w]>0).sum(axis=0),h*.25)]
        ys=[y+i for i in peaks((horizontal[y:y+h,x:x+w]>0).sum(axis=1),w*.25)]
        if not 3<=len(xs)<=51 or not 3<=len(ys)<=101:continue
        nr,nc=len(ys)-1,len(xs)-1;parent=list(range(nr*nc))
        def find(i):
            while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
            return i
        def union(a,b):parent[find(a)]=find(b)
        for row in range(nr):
            for col in range(nc):
                if col+1<nc:
                    patch=vertical[ys[row]+3:ys[row+1]-3,max(0,xs[col+1]-2):xs[col+1]+3]
                    if patch.size and (patch>0).any(axis=1).mean()<.6:union(row*nc+col,row*nc+col+1)
                if row+1<nr:
                    patch=horizontal[max(0,ys[row+1]-2):ys[row+1]+3,xs[col]+3:xs[col+1]-3]
                    if patch.size and (patch>0).any(axis=0).mean()<.6:union(row*nc+col,(row+1)*nc+col)
        groups={}
        for r in range(nr):
            for c in range(nc):groups.setdefault(find(r*nc+c),[]).append((r,c))
        cells=[];ambiguous=False
        for positions in groups.values():
            r0=min(r for r,c in positions);r1=max(r for r,c in positions)+1;c0=min(c for r,c in positions);c1=max(c for r,c in positions)+1
            if len(positions)!=(r1-r0)*(c1-c0):ambiguous=True;break
            box=[xs[c0]/width,ys[r0]/height,xs[c1]/width,ys[r1]/height];observations=[]
            for li,line in enumerate(lines):
                b=line['box'];cx=(b[0]+b[2])/2;cy=(b[1]+b[3])/2
                if box[0]<=cx<=box[2] and box[1]<=cy<=box[3]:observations.append((li,line))
            ink=image[ys[r0]+3:ys[r1]-3,xs[c0]+3:xs[c1]-3];blank=bool(ink.size and ink.min()>=245)
            okay=all(line.get('status','confirmed')=='confirmed' and line.get('confidence',0)>=.8 and line['box'][0]>=box[0]-.01 and line['box'][2]<=box[2]+.01 for _,line in observations)
            text='\n'.join(line['text'] for _,line in sorted(observations,key=lambda x:(x[1]['box'][1],x[1]['box'][0])))
            cells.append({'id':f'ocr-t{len(tables)}-r{r0}-c{c0}','row':r0,'column':c0,'rowspan':r1-r0,'colspan':c1-c0,
                'text':text,'box':box,'role':'data','blank':blank,'status':'confirmed' if okay and (text or blank) else 'pending',
                'evidence_lines':[li for li,_ in observations]})
        if ambiguous:continue
        tables.append({'id':f'ocr-t{len(tables)}','title':'扫描规则网格（表头角色待确认）','box':[xs[0]/width,ys[0]/height,xs[-1]/width,ys[-1]/height],
            'rows':nr,'columns':nc,'cells':cells,'method':'ocr_geometry','header_status':'pending','status':'confirmed' if all(c['status']=='confirmed' for c in cells) else 'pending'})
        if len(tables)>=20:break
    return tables


def _remove_derived(page,media_id,visual_only=False):
    def remove(block):
        if block.get('media_id')!=media_id:return False
        return block.get('source_method')=='visual_observed' if visual_only else True
    removed=[b for b in page.get('blocks',[]) if remove(b)]
    spans=[]
    for block in sorted(removed,key=lambda b:b['start']):
        end=block['end']
        if page['text'][end:end+2]=='\n\n':end+=2
        if spans and block['start']<=spans[-1][1]:spans[-1][1]=max(spans[-1][1],end)
        else:spans.append([block['start'],end])
    def offset(value):return value-sum(max(0,min(value,end)-start) for start,end in spans if start<value)
    if spans:
        cursor=0;pieces=[]
        for start,end in spans:pieces.append(page['text'][cursor:start]);cursor=end
        pieces.append(page['text'][cursor:]);page['text']=''.join(pieces)
    kept=[]
    for block in page.get('blocks',[]):
        if remove(block):continue
        block['start']=offset(block['start']);block['end']=offset(block['end'])
        for char in block.get('chars',[]):char['start']=offset(char['start']);char['end']=offset(char['end'])
        kept.append(block)
    page['blocks']=kept
    for heading in page.get('headings',[]):
        heading['start']=offset(heading['start']);heading['end']=offset(heading['end'])
    for field in ('tables','charts','formulas'):
        page[field]=[x for x in page.get(field,[]) if not (x.get('media_id')==media_id and (not visual_only or x.get('method')=='visual_observed'))]
        for obj in page[field]:
            for span in obj.get('text_ranges',[]):span['start']=offset(span['start']);span['end']=offset(span['end'])
    if not visual_only:
        for field in ('ocr_lines','recognition_pending'):page[field]=[x for x in page.get(field,[]) if x.get('media_id')!=media_id]


def apply_ocr_page(base_page,result,media_id=None):
    page=copy.deepcopy(base_page)
    if media_id is None:page.update(text='',blocks=[],headings=[],tables=[],charts=[])
    else:_remove_derived(page,media_id)
    page.setdefault('tables',[]);page.setdefault('charts',[]);page.setdefault('issues',[])
    page.setdefault('ocr_lines',[]);page.setdefault('recognition_pending',[])
    page.setdefault('formulas',[])
    if media_id is None:page['formulas']=[]
    else:page['formulas']=[f for f in page['formulas'] if f.get('media_id')!=media_id]
    page['label_reviews']=([] if media_id is None else [r for r in page.get('label_reviews',[]) if r.get('media_id')!=media_id])+[{**copy.deepcopy(r),'media_id':media_id} for r in result.get('label_reviews',[])]
    formula_lines={i for f in result.get('formulas',[]) for i in f.get('evidence_lines',[])}
    if media_id is None:page['ocr_lines']=[];page['recognition_pending']=[]
    for index,line in enumerate(result.get('lines',[])):
        status=line.get('status') or ('confirmed' if line.get('confidence',0)>=.8 else 'pending')
        observation={**copy.deepcopy(line),'status':status,'media_id':media_id,'observation_id':index}
        if index in formula_lines:
            observation.update(status='pending',source_pending_reason='formula_structure_requires_review')
            status='pending'
        page['ocr_lines'].append(observation)
        if status!='confirmed':
            page['recognition_pending'].append(observation);continue
        _append(page,line['text'],'text',line.get('box'),source_method='ocr',confidence=line.get('confidence'),
            status=status,location_precision='line',media_id=media_id,observation_id=index)
    for table in result.get('tables',[]):
        table=copy.deepcopy(table);table['media_id']=media_id
        table['id']=f"p{page['number']}-{media_id or 'page'}-{table['id']}"
        for ci,cell in enumerate(table.get('cells',[])):cell['id']=table['id']+f'-c{ci}'
        page['tables'].append(table);_table_rows(page,table,'ocr_geometry')
    for formula in result.get('formulas',[]):
        formula=copy.deepcopy(formula);formula['media_id']=media_id
        formula['id']=f"p{page['number']}-{media_id or 'page'}-{formula['id']}"
        page['formulas'].append(formula)
        _append(page,formula['text'],'formula',formula['box'],source_method='ocr_spatial_formula',
                status=formula['status'],media_id=media_id,formula_id=formula['id'],
                location_precision='region',source_pending_reason='formula_structure_requires_review')
    page['text_available']=any(b.get('status','confirmed')=='confirmed' and b['text'].strip() for b in page['blocks'])
    page['ocr_required']=not page['text_available']
    _recognition_issues(page,result,'ocr',media_id,reset=True)
    if media_id is None:page['location_precision']='line'
    page['provenance']={**result.get('provenance',{}),'method':'ocr','model':result.get('model'),'location_precision':'line',
        'fingerprint':result.get('fingerprint'),'input_image_sha256':result.get('input_image_sha256'),
        'duration_ms':result.get('duration_ms'),'rotation':result.get('rotation',0),'human_reviewed':False}
    from .document_quality import inspect
    return inspect(page)


def apply_visual_page(base_page,result,media_id=None):
    page=copy.deepcopy(base_page);page.setdefault('tables',[]);page.setdefault('charts',[]);page.setdefault('issues',[])
    _remove_derived(page,media_id,visual_only=True)
    for block in result.get('blocks',[]):
        if block.get('status')=='confirmed':
            _append(page,block['text'],block.get('kind','text'),block.get('box'),status='confirmed',
                source_method='visual_observed',location_precision='region',media_id=media_id)
    for ti,table in enumerate(result.get('tables',[])):
        table=copy.deepcopy(table);table.update(media_id=media_id,method='visual_observed');table['id']=f"vision-p{page['number']}-{media_id or 'page'}-t{ti}"
        for ci,cell in enumerate(table.get('cells',[])):cell.setdefault('id',table['id']+f'-c{ci}')
        page['tables'].append(table);_table_rows(page,table,'visual_observed')
    for ci,chart in enumerate(result.get('charts',[])):
        chart=copy.deepcopy(chart);chart.update(media_id=media_id,method='visual_observed');chart['id']=f"vision-p{page['number']}-{media_id or 'page'}-chart{ci}"
        for si,series in enumerate(chart.get('series',[])):
            for pi,point in enumerate(series.get('points',[])):point.setdefault('id',chart['id']+f'-s{si}-p{pi}')
        page['charts'].append(chart);_chart_text(page,chart,'visual_observed')
    _recognition_issues(page,result,'visual_observed',media_id)
    # Unverified chart associations cannot be bypassed via the underlying OCR
    # numbers. Keep them visible in the parse, excluded from answer evidence.
    uncertain=[c.get('box') for c in result.get('charts',[]) if c.get('status')!='confirmed' and c.get('box')]
    for block in page.get('blocks',[]):
        box=block.get('box')
        if block.get('media_id')!=media_id or not box or not re.search(r'\d',block.get('text','')):continue
        for area in uncertain:
            overlap=max(0,min(box[2],area[2])-max(box[0],area[0]))*max(0,min(box[3],area[3])-max(box[1],area[1]))
            if overlap/((box[2]-box[0])*(box[3]-box[1]))>=.8:
                block.update(status='pending',source_pending_reason='unverified_chart_association');break
    page['text_available']=any(b.get('status','confirmed')=='confirmed' and b.get('text','').strip() for b in page.get('blocks',[]))
    page['provenance']={**result.get('provenance',{}),'method':'visual_observed','human_reviewed':False,
        'structural_check':'completed','semantic_accuracy':'not_proven'}
    page['visual_result']={k:copy.deepcopy(result[k]) for k in ('provider','raw_response','usage','input_image_sha256','duration_ms') if k in result}
    return page


def _recognition_issues(page,result,method,media_id,reset=False):
    methods={'ocr','visual_observed'} if reset else {method}
    page['issues']=[issue for issue in page.get('issues',[]) if not (isinstance(issue,dict) and issue.get('media_id')==media_id and issue.get('source_method') in methods)]
    page['issues'].extend({**(copy.deepcopy(issue) if isinstance(issue,dict) else {'message':str(issue)}),
                          'source_method':method,'media_id':media_id} for issue in result.get('issues',[]))


def link_continuations(pages):
    """Only adjacent structurally matching tables with explicit repeat headers."""
    links=[]
    for before,after in zip(pages,pages[1:]):
        if after['number']!=before['number']+1:continue
        for left in before.get('tables',[]):
            for right in after.get('tables',[]):
                if left.get('columns')!=right.get('columns') or left.get('status')!='confirmed' or right.get('status')!='confirmed':continue
                def headers(table):return [(c['column'],c.get('colspan',1),re.sub(r'\s+','',c['text'])) for c in table.get('cells',[]) if c.get('role') in {'header','column_header'} and c.get('text')]
                lh,rh=headers(left),headers(right)
                if not lh or lh!=rh:continue
                a,b=left.get('box'),right.get('box')
                if not a or not b or a[3]<.70 or b[1]>.35 or abs(a[0]-b[0])>.02 or abs(a[2]-b[2])>.02:continue
                link={'previous_table_id':left['id'],'next_table_id':right['id'],'pages':[before['number'],after['number']],
                    'status':'candidate','reason':'相邻页、列数和重复表头一致、横向区域对齐；业务续表关系仍待确认'}
                right.setdefault('continuations',[]).append(link);links.append(link)
    return links
