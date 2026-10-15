"""Source-bound table relationships. Layout validity is not semantic accuracy."""
import re
import statistics

VERSION='table-relations-v1'
COLUMN_ROLES={'header','column_header'}
ROW_ROLES={'row_header','stub'}
HEADER_ROLES=COLUMN_ROLES|{'corner'}


def _numeric(text):
    return bool(re.fullmatch(r'[+−-]?[\d,.]+(?:%|‰)?',text.strip()))


def _header_text(text):
    return bool(text.strip()) and (not _numeric(text) or bool(re.fullmatch(r'(?:19|20)\d{2}',text.strip())))


def _bounds(c):
    return c['row'],c['row']+c.get('rowspan',1),c['column'],c['column']+c.get('colspan',1)


def geometry(table):
    """Check physical ordering against declared grid, not merely disjoint boxes."""
    problems=[];axes={}
    for axis,count,lo,hi in [('x',table['columns'],0,2),('y',table['rows'],1,3)]:
        samples={}
        for cell in table['cells']:
            box=cell.get('box')
            if not box:continue
            start=cell['column'] if axis=='x' else cell['row']
            span=cell.get('colspan',1) if axis=='x' else cell.get('rowspan',1)
            samples.setdefault(start,[]).append(box[lo]);samples.setdefault(start+span,[]).append(box[hi])
        if not samples:continue
        # Ink-tight cells may leave whitespace. Reject reversed/contradictory
        # boundaries, without demanding that a model reproduces ruled lines.
        extent=table['box'][hi]-table['box'][lo]
        tolerance=max(.001,min(.012*extent,.15*extent/max(1,count)))
        boundaries={i:statistics.median(values) for i,values in samples.items()}
        for i,values in samples.items():
            if max(values)-min(values)>2*tolerance:problems.append({'axis':axis,'boundary':i,'reason':'inconsistent_cell_edges'})
        ordered=sorted(boundaries)
        if any(boundaries[a]>=boundaries[b]-tolerance for a,b in zip(ordered,ordered[1:])):
            problems.append({'axis':axis,'reason':'grid_order_conflict'})
        axes[axis]={'boundaries':{str(i):round(v,6) for i,v in boundaries.items()},'tolerance':tolerance}
    return {'status':'pending' if problems else 'consistent','issues':problems,'axes':axes,
            'scope':'logical grid versus observed rectangles; not character or business meaning proof'}


def prepare(table):
    """Annotate a bounded grid in place; never fill a missing value or cell."""
    cells=table.get('cells',[]);rows=table.get('rows',0);columns=table.get('columns',0)
    if type(rows) is not int or type(columns) is not int or not 1<=rows<=1000 or not 1<=columns<=100:
        raise ValueError('表格行列数无效或超限')
    if len(cells)>5000:
        table.update(relation_status='pending',status='pending',relation_audit={'version':VERSION,
            'issues':[{'code':'table_relation_budget','message':'单表超过5000格，保留原结构但不生成完整关系结论'}]})
        return table
    occupied=set();issues=[]
    for index,c in enumerate(cells):
        c.setdefault('id',table.get('id','table')+f'-c{index}')
        values=[c.get('row'),c.get('column'),c.get('rowspan',1),c.get('colspan',1)]
        if any(type(v) is not int for v in values):raise ValueError('表格网格坐标类型无效')
        r,c0,rs,cs=values
        if min(r,c0)<0 or min(rs,cs)<1 or r+rs>rows or c0+cs>columns:raise ValueError('表格单元格跨度越界')
        slots={(y,x) for y in range(r,r+rs) for x in range(c0,c0+cs)}
        if occupied&slots:issues.append({'code':'overlapping_grid_cells','cell_id':c['id']})
        occupied|=slots
    if len({c['id'] for c in cells})!=len(cells):raise ValueError('表格单元格身份重复')
    if table.get('method') in {'docx_native','pdf_native_table'}:
        # An explicitly marked/native first header can extend through merged
        # multi-row bands. A numeric data row never becomes another header.
        headers=[c for c in cells if c.get('role') in COLUMN_ROLES]
        end=max((c['row']+c.get('rowspan',1) for c in headers),default=0)
        for row in range(1,min(rows,6)):
            items=[c for c in cells if c['row']==row]
            parent=any(c['row']<row<c['row']+c.get('rowspan',1) or
                       (c['row']+c.get('rowspan',1)==row and c.get('colspan',1)>1) for c in headers)
            if row>end or not parent or not items or not all(not c.get('text','').strip() or _header_text(c['text']) for c in items):break
            for c in items:
                if c.get('role') not in HEADER_ROLES:
                    c.update(role='column_header',role_verification='native_merged_header_band')
                    headers.append(c)
            end=max(end,row+1)
        header_end=max((c['row']+c.get('rowspan',1) for c in headers),default=0)
        for row in range(header_end,rows):
            line=sorted((c for c in cells if c['row']<=row<c['row']+c.get('rowspan',1)),key=lambda c:c['column'])
            # Infer leading stubs only for numerical rows. A text-valued field
            # is still data; its position alone cannot prove a row-name role.
            data=[c for c in line if c.get('role') not in HEADER_ROLES]
            numeric=any(_numeric(c.get('text','')) for c in data)
            if not numeric:continue
            for c in data:
                if c.get('role') in HEADER_ROLES:continue
                text=c.get('text','').strip()
                if not text or _numeric(text):break
                if c.get('role','data') in {'data','body'}:
                    c.update(role='row_header',role_verification='native_leading_text_before_values')
    physical=table.get('method') in {'visual_observed','pdf_native_table','ocr_geometry'}
    audit=geometry(table) if physical and table.get('box') else {'status':'not_checked','issues':[]}
    issues+=audit['issues']
    table['relation_audit']={'version':VERSION,'geometry':audit,'issues':issues,
        'occupied_slots':len(occupied),'missing_slots':rows*columns-len(occupied),
        'coverage_status':'complete' if len(occupied)==rows*columns else 'partial'}
    table['relation_status']='pending' if issues else 'consistent'
    if issues:table['status']='pending'
    headers=[c for c in cells if c.get('role') in HEADER_ROLES]
    headers+=table.get('external_header',{}).get('cells',[]) if table.get('external_header') else []
    for cell in cells:
        r,end,col,last=_bounds(cell)
        parents=[h for h in headers if h['row']+h.get('rowspan',1)<=r and h['column']<=col and h['column']+h.get('colspan',1)>=last]
        parents.sort(key=lambda c:(c['row'],c['column']))
        cell['column_header_ids']=[h['id'] for h in parents]
        cell['column_path']=[h.get('text','') for h in parents]
    return table


def row_views(table):
    """Yield only values with all required observed labels and stable identity."""
    prepare(table)
    if table['relation_status']=='pending':return
    cells=table['cells'];external=table.get('external_header') or {}
    by_id={c['id']:c for c in cells+external.get('cells',[])}
    has_headers=any(c.get('role') in COLUMN_ROLES for c in by_id.values())
    # Unknown column roles in a ruled scan cannot become invented label facts.
    if table.get('header_status')=='pending' and not has_headers:return
    if table.get('status')=='pending' and not has_headers and not any(c.get('label_review') for c in cells):return
    title=table.get('title') if table.get('title_status','confirmed')=='confirmed' else ''
    title=title or '表格结构视图'
    def withhold(cell,reason):
        item={'cell_id':cell['id'],'reason':reason}
        values=table['relation_audit'].setdefault('unbound_values',[])
        if item not in values:values.append(item)
        table['status']='pending'
    stub_columns={col for c in cells if c.get('role') in ROW_ROLES
                  for col in range(c['column'],c['column']+c.get('colspan',1))}
    first_data_row=min((c['row'] for c in cells if c.get('role','data') in {'data','body'}),default=table['rows'])
    unknown_headers=[c for c in cells if c.get('role')=='unknown' and c['row']<first_data_row
        and any(h.get('role') in COLUMN_ROLES and h['row']+h.get('rowspan',1)<=c['row'] and
                max(h['column'],c['column'])<min(h['column']+h.get('colspan',1),c['column']+c.get('colspan',1)) for h in by_id.values())]
    for row in range(table['rows']):
        active=sorted((c for c in cells if c['row']<=row<c['row']+c.get('rowspan',1)),key=lambda c:c['column'])
        stubs=[c for c in active if c.get('role') in ROW_ROLES]
        labelled_columns={col for c in stubs for col in range(c['column'],c['column']+c.get('colspan',1))}
        if stub_columns-labelled_columns or any(c.get('status','confirmed')!='confirmed' or not c.get('text','').strip() for c in stubs):
            for c in active:
                if c.get('role','data') in {'data','body','unknown'}:withhold(c,'unresolved_row_label')
            continue
        stub_headers=list(dict.fromkeys(hid for c in stubs for hid in c['column_header_ids'] if by_id[hid].get('text','').strip()))
        if any(by_id[hid].get('status','confirmed')!='confirmed' for hid in stub_headers):continue
        row_labels=[c.get('text','') for c in stubs if c.get('text','').strip()]
        bindings=[];pairs=[];source_ids=[]
        for cell in active:
            if cell.get('role')=='unknown':
                withhold(cell,'unknown_cell_role');continue
            if cell.get('role','data') not in {'data','body'} or cell.get('status','confirmed')!='confirmed' or not cell.get('text','').strip():continue
            if cell.get('rowspan',1)>1:
                # A shared value is not a separate measure for every named row.
                # Only expose it once, and only when all covered row identities
                # are identical. Ambiguous shared totals remain in the source.
                identities={tuple(c['id'] for c in sorted(cells,key=lambda c:c['column'])
                    if c.get('role') in ROW_ROLES and c['row']<=r<c['row']+c.get('rowspan',1))
                    for r in range(cell['row'],cell['row']+cell['rowspan'])}
                if len(identities)>1:
                    withhold(cell,'rowspan_multiple_row_names');continue
                if cell['row']!=row:continue
            # A value spanning distinct leaf columns has no unique column label.
            _,_,lo,hi=_bounds(cell)
            if any(max(lo,h['column'])<min(hi,h['column']+h.get('colspan',1)) for h in unknown_headers):
                withhold(cell,'unresolved_column_label');continue
            headers=[by_id[hid] for hid in cell['column_header_ids']]
            internal_headers=[h for h in by_id.values() if h.get('role') in HEADER_ROLES and h['row']>=0 and h['row']+h.get('rowspan',1)<=row]
            if internal_headers:
                first=min(h['row'] for h in internal_headers);last=max(h['row']+h.get('rowspan',1) for h in internal_headers)
                if any(not any(h['row']<=r<h['row']+h.get('rowspan',1) and h['column']<=col<h['column']+h.get('colspan',1) for h in internal_headers)
                       for r in range(first,last) for col in range(lo,hi)):continue
            crossing=[h for h in by_id.values() if h.get('role') in COLUMN_ROLES and h['row']+h.get('rowspan',1)<=row
                and max(lo,h['column'])<min(hi,h['column']+h.get('colspan',1)) and h not in headers]
            if crossing:
                withhold(cell,'colspan_multiple_column_headers');continue
            if any(h.get('status','confirmed')!='confirmed' for h in headers):continue
            # An explicit empty header is unknown, not a license to borrow a
            # neighbouring label. Partial sparse tables keep column ordinals.
            if has_headers and not headers:continue
            labels=[h['text'] for h in headers if h.get('text','').strip()]
            if any(not h.get('text','').strip() for h in headers):continue
            label=' / '.join(labels)
            if not label:label=f"第{cell['column']+1}列"
            pairs.append(label+'：'+cell['text'])
            ids=list(dict.fromkeys(stub_headers+[c['id'] for c in stubs]+[h['id'] for h in headers]+[cell['id']]))
            source_ids.extend(ids)
            bindings.append({'value_cell_id':cell['id'],'column_header_ids':[h['id'] for h in headers],
                'row_header_ids':[c['id'] for c in stubs],'row_header_label_ids':stub_headers,'column_path':labels,'row_path':row_labels,
                'row':row,'column':cell['column'],'colspan':cell.get('colspan',1),'rowspan':cell.get('rowspan',1)})
        if pairs:
            context=('行名：'+' / '.join(row_labels)+' | ') if row_labels else ''
            yield {'row':row,'text':title+f' · 第{row+1}行\n'+context+' | '.join(pairs),
                'cell_ids':list(dict.fromkeys(source_ids)),'cell_bindings':bindings}
