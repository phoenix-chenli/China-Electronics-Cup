import copy
import json
import pytest

from backend import table_context as m
from backend.table_bindings import check_table_bindings

def fixture():
    text='设计能力 7 MGD，实际平均 4.12 MGD。\nTable 3. Measurements\n'
    blocks=[{'start':0,'end':25,'text':text[:25],'bbox':[.05,.01,.9,.05]}]
    rows=[['Parameter','Average','Max'],['Influent TN (mg/L)','31.2','42.7'],['Effluent TN (mg/L)','2.14','2.77']]
    cells=[]
    for row,values in enumerate(rows,1):
        for col,value in enumerate(values,1):
            bbox=[(col-1)/3,.1+(row-1)*.2,col/3,.1+row*.2]
            cells.append({'row':row,'col':col,'text':value,'bbox':bbox,'rowspan':1,'colspan':1,'covered_by':None})
            start=len(text);text+=value+'\n';blocks.append({'start':start,'end':len(text)-1,'text':value,'bbox':[bbox[0]+.01,bbox[1]+.02,bbox[2]-.01,bbox[3]-.02]})
    text+='Notes: study averages, not permit limits.'
    region={'table_name':'第2页_表65','page':2,'bbox':[0,.1,1,.7],'row_count':3,'column_count':3,'cells':cells}
    source={'format':'pdf','id':'s','sha256':'hash','pages':[{'number':2,'text':text,'blocks':blocks}],
            'metadata':{'table_semantics':[{'table_name':f't{i}','page':1} for i in range(64)],'table_regions':[region]}}
    return source

def test_late_grid_recovers_actual_cells_without_changing_source():
    s=fixture();before=copy.deepcopy(s);spans=m.split_evidence_span(s,2,0,len(s['pages'][0]['text']),'TN平均浓度')
    row=next(x for x in spans if x['kind']=='table_rows')
    context=m.context_for_span(s,2,row['start'],row['end']);t=context['tables'][0]
    assert not context['truncated'] and t['header_basis']=='inferred' and t['requires_review']
    assert t['table_name']=='第2页_表65' and 'R2C2' in t['selected_cell_ids']
    assert next(c['text'] for c in t['cells'] if c['cell_id']=='R2C2')=='31.2'
    assert s==before

def test_mixed_prose_keeps_exact_offsets_and_never_invents_cells():
    s=fixture();text=s['pages'][0]['text'];spans=m.split_evidence_span(s,2,0,len(text),'设计能力 TN')
    prose=next(x for x in spans if x['kind']=='prose' and x['start']==0)
    assert '7 MGD' in text[prose['start']:prose['end']]
    context=m.context_for_span(s,2,prose['start'],prose['end'])
    assert not context['has_table']
    record={'id':'E1','page':2,'quote':text[prose['start']:prose['end']],'table_context':context}
    assert check_table_bindings('设计能力7 MGD。',None,[record])['valid']
    forged={'evidence_id':'E1','table_name':'第2页_表65','cell_id':'intro_capacity','value':'7','statement':'设计能力7 MGD。','row_label':'正文','column_label':'容量'}
    assert not check_table_bindings('设计能力7 MGD。',[forged],[record])['valid']
    assert all(0<=x['start']<x['end']<=len(text) for x in spans)

def test_true_row_binding_valid_but_wrong_cell_value_and_header_rejected():
    s=fixture();text=s['pages'][0]['text'];row=next(x for x in m.split_evidence_span(s,2,0,len(text),'TN') if x['kind']=='table_rows')
    context=m.context_for_span(s,2,row['start'],row['end']);record={'id':'E1','page':2,'quote':text[row['start']:row['end']],'table_context':context}
    statement='Influent TN (mg/L) Average为31.2 mg/L。'
    binding={'evidence_id':'E1','table_name':'第2页_表65','cell_id':'R2C2','value':'31.2','statement':statement,'row_label':'Influent TN (mg/L)','column_label':'Average','unit':'mg/L'}
    assert check_table_bindings(statement,[binding],[record])['valid']
    for key,value in [('cell_id','p2_fake_average'),('value','42.7'),('column_label','Max')]:
        assert not check_table_bindings(statement,[{**binding,key:value}],[record])['valid']

def test_unlocated_legacy_table_never_becomes_prose():
    s=fixture();s['pages'][0]['blocks']=[]
    spans=m.split_evidence_span(s,2,0,30,'设计能力')
    assert all(x['kind']=='unresolved_table' for x in spans)
    assert m.context_for_span(s,2,0,30)['requires_review']

def test_partial_row_does_not_expand_original_chunk_and_duplicate_spans_removed():
    s=fixture();text=s['pages'][0]['text'];start=text.index('31.2');end=start+4
    spans=m.split_evidence_span(s,2,start,end,'TN')
    assert spans==[{'start':start,'end':end,'kind':'table_rows','priority':10,'table_name':'第2页_表65'}]
    context=m.context_for_span(s,2,start,end)
    assert context['tables'][0]['selected_cell_ids']==['R2C2']

def test_bad_grid_stays_unknown():
    s=fixture();s['metadata']['table_regions'][0]['cells']=[{'text':'31.2'}]
    assert m.context_for_span(s,2,50,100)['unresolved_structure']

@pytest.mark.parametrize('error',['wrong_row_unit','missing_anchor','column_conflict','fake_label','missing_unit','wrong_unit'])
def test_row_unit_cannot_be_borrowed_or_conflicted_or_omitted(error):
    s=fixture();text=s['pages'][0]['text'];row=next(x for x in m.split_evidence_span(s,2,0,len(text),'TN') if x['kind']=='table_rows')
    context=m.context_for_span(s,2,row['start'],row['end']);table=context['tables'][0]
    statement='Influent TN (mg/L) Average为31.2 mg/L。'
    binding={'evidence_id':'E1','table_name':'第2页_表65','cell_id':'R2C2','value':'31.2','statement':statement,'row_label':'Influent TN (mg/L)','column_label':'Average','unit':'mg/L'}
    if error=='wrong_row_unit':table['units'][0]['row']=3
    elif error=='missing_anchor':table['units'][0]['evidence']={}
    elif error=='column_conflict':table['units'].append({'scope':'column','column':2,'value':'MGD'})
    elif error=='fake_label':table['units'][0]['cell_id']='invented_unit_cell'
    elif error=='missing_unit':binding.pop('unit')
    elif error=='wrong_unit':binding['unit']='MGD';binding['statement']=statement=statement.replace('31.2 mg/L','31.2 MGD')
    record={'id':'E1','page':2,'quote':text[row['start']:row['end']],'table_context':context}
    assert not check_table_bindings(statement,[binding],[record])['valid']
