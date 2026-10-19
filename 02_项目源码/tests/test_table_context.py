"""Exact offsets, physical boxes, and genuine SQL row mappings select table cells."""
import copy
import json

import pytest

from backend.table_context import context_for_span, context_for_table, table_text_spans
from backend.table_semantics import build_table_semantics
from backend.table_bindings import check_table_bindings


def doc_source():
    text='另有预算10万元。\n项目 | 金额（万元）\n甲 | 10\n乙 | 20\n注：仅含本期。'
    cells=[]
    # Positions are supplied by parsing, not discovered by the context module.
    offset=len('另有预算10万元。\n')
    for row,values in enumerate([['项目','金额（万元）'],['甲','10'],['乙','20']],1):
        for col,value in enumerate(values,1):
            cells.append({'row':row,'col':col,'text':value,'start':offset,'end':offset+len(value),
                          'rowspan':1,'colspan':1,'covered_by':None})
            offset+=len(value)+(3 if col==1 else 1)
    start=cells[0]['start'];end=cells[-1]['end']
    table=build_table_semantics({'table_name':'表格 1','row_count':3,'column_count':2,'cells':cells},
        format='docx',source_position={'page':1,'start':start,'end':end,'page_unit':'logical_section'},
        explicit_header_rows=[1],after=[{'text':'注：仅含本期。','evidence':{'page':1,'start':end+1,'end':len(text)}}])
    return {'format':'docx','pages':[{'number':1,'text':text,'blocks':[]}],
            'metadata':{'table_semantics':[table],'table_layouts':[{'name':'表格 1'}]}},table


def test_exact_span_selects_only_its_real_cell_and_keeps_row_header_unit_and_note():
    source,table=doc_source();cell=next(c for c in table['cells'] if c['cell_id']=='R2C2')
    before=copy.deepcopy(source)
    context=context_for_span(source,1,cell['start'],cell['end'])
    t=context['tables'][0]
    assert t['selected_cell_ids']==['R2C2']
    assert {c['cell_id'] for c in t['cells']}=={'R1C1','R1C2','R2C1','R2C2'}
    assert t['row_headers'][0]['text']=='甲'
    assert [row['row'] for row in t['row_headers']]==[2]
    assert t['columns'][1]['header_path'][0]['text']=='金额（万元）'
    assert t['units'][0]['value']=='万元' and t['footnotes'][0]['text']=='注：仅含本期。'
    assert t['footnotes'][0]['evidence']['start']==table['source_position']['end']+1
    assert source==before


def test_same_number_outside_table_cannot_be_matched_by_text_value():
    source,_=doc_source()
    context=context_for_span(source,1,4,6)
    assert not context['has_table'] and context['tables']==[]


def test_covered_cell_is_not_selected_value_but_real_anchor_is_present():
    source,table=doc_source()
    table['cells'].append({'cell_id':'R3C3','row':3,'col':3,'text':None,'covered_by':[2,2],
                           'rowspan':1,'colspan':1,'start':100,'end':101})
    table['source_position']['end']=101
    context=context_for_span(source,1,100,101)
    selected=context['tables'][0]
    assert selected['selected_cell_ids']==[]
    assert context['unresolved_structure']
    verdict=check_table_bindings('甲的金额为10万元。',None,[{'id':'e1','table_context':context}])
    assert verdict['required'] and not verdict['valid']
    assert any(c['cell_id']=='R2C2' and c['text']=='10' for c in selected['cells'])
    assert any(c['cell_id']=='R3C3' and c['text'] is None for c in selected['cells'])


def test_physical_blocks_match_real_bounding_boxes_not_same_text():
    table=build_table_semantics({'table_name':'第1页_表1','page':1,'bbox':[.1,.2,.9,.7],
        'row_count':2,'column_count':2,'cells':[
            {'row':1,'col':1,'text':'项目','bbox':[.1,.2,.5,.4]},
            {'row':1,'col':2,'text':'金额','bbox':[.5,.2,.9,.4]},
            {'row':2,'col':1,'text':'甲','bbox':[.1,.4,.5,.7]},
            {'row':2,'col':2,'text':'10','bbox':[.5,.4,.9,.7]}]},format='pdf',explicit_header_rows=[1])
    source={'pages':[{'number':1,'text':'10 10','blocks':[
        {'start':0,'end':2,'text':'10','bbox':[.1,.8,.2,.9]},
        {'start':3,'end':5,'text':'10','bbox':[.6,.5,.7,.6]}]}],
        'metadata':{'table_semantics':[table]}}
    assert context_for_span(source,1,0,2)['tables']==[]
    context=context_for_span(source,1,3,5)
    assert context['tables'][0]['selected_cell_ids']==['R2C2']


def test_xlsx_sql_rows_map_to_original_rows_and_aggregate_has_no_fictitious_lineage():
    source,table=doc_source()
    source['format']=table['format']='xlsx'
    table['sql_row_mapping']={'runs':[[1,2,1],[2,3,1]],'sql_row_count':2,'header_source_row':1}
    context=context_for_table(source,'表格 1',[2])
    assert context['tables'][0]['selected_cell_ids']==['R3C1','R3C2']
    aggregate=context_for_table(source,'表格 1',[])
    assert aggregate['tables'][0]['selection_state']=='table_only'
    assert aggregate['tables'][0]['selected_cell_ids']==[]
    assert aggregate['tables'][0]['row_headers']==[]
    assert all(c['row']==1 for c in aggregate['tables'][0]['cells'])
    unknown=context_for_table(source,'表格 1',[100])
    assert unknown['truncated'] and unknown['requires_review']
    assert not unknown['tables'][0]['selected_cell_ids']


def test_docx_sql_keeps_source_row_number_including_header():
    source,_=doc_source()
    context=context_for_table(source,'表格 1',[2])
    assert context['tables'][0]['selected_cell_ids']==['R2C1','R2C2']


@pytest.mark.parametrize('budget',[128,250,1000])
def test_budget_cannot_silently_remove_unit_footnote_or_structure_and_still_allow_answer(budget):
    source,table=doc_source();cell=table['cells'][-1]
    result=context_for_span(source,1,cell['start'],cell['end'],max_chars=budget)
    assert result['has_table'] and result['truncated'] and result['requires_review']
    assert len(json.dumps(result,ensure_ascii=False,separators=(',',':')))<=budget
    assert all(not t['selected_cell_ids'] for t in result['tables'])


def test_legacy_docx_without_positions_is_explicitly_unknown_and_needs_reparse():
    source,_=doc_source();source['metadata'].pop('table_semantics')
    context=context_for_span(source,1,4,6)
    assert context['has_table'] and context['requires_review']
    assert context['tables'][0]['structure_unknown']
    assert context['unresolved_structure']
    verdict=check_table_bindings('甲的金额为10万元。',None,[{'id':'e1','table_context':context}])
    assert verdict['required'] and not verdict['valid']
    assert not context['tables'][0]['selected_cell_ids']


def test_legacy_pdf_table_region_uses_real_bbox_or_marks_missing_location():
    source={'pages':[{'number':1,'text':'10','blocks':[{'start':0,'end':2,'bbox':[.2,.2,.4,.4]}]}],
            'metadata':{'table_regions':[{'table_name':'表','page':1,'bbox':[.1,.1,.5,.5]}]}}
    assert context_for_span(source,1,0,2)['tables'][0]['structure_unknown']
    source['pages'][0]['blocks']=[]
    assert context_for_span(source,1,0,2)['tables'][0]['structure_unknown']


def test_text_table_with_no_offsets_or_page_boxes_cannot_bypass_structural_binding():
    source,table=doc_source()
    table['source_position']={'page':1,'bbox':[0,0,1,1]}
    for cell in table['cells']:
        cell.pop('start');cell.pop('end')
    result=context_for_span(source,1,4,6)
    assert result['has_table'] and result['tables'][0]['structure_unknown']


def test_source_span_between_cells_cannot_be_reclassified_as_ordinary_evidence():
    source,table=doc_source()
    end=table['cells'][0]['end']
    context=context_for_span(source,1,end,end+1)
    assert context['has_table'] and context['unresolved_structure']
    assert context['tables'][0]['selection_state']=='source_span'
    verdict=check_table_bindings('甲的金额为10万元。',[],[{'id':'e1','table_context':context}])
    assert verdict['required'] and not verdict['valid']


def test_row_spans_are_exact_original_docx_intervals_and_not_joined_values():
    source,table=doc_source()
    result=table_text_spans(source,1)
    assert [(r['row'],source['pages'][0]['text'][r['start']:r['end']]) for r in result['rows']]==[(2,'甲 | 10'),(3,'乙 | 20')]
    assert result['regions']==[{'start':table['source_position']['start'],'end':table['source_position']['end']}]
    limited=table_text_spans(source,1,result['rows'][0]['end'])
    assert [r['row'] for r in limited['rows']]==[2]


def test_context_deduplicates_only_exact_specialized_text_and_position():
    source,table=doc_source()
    note=copy.deepcopy(table['footnotes'][0]);table['context']=[note,copy.deepcopy(note),
        {'text':note['text'],'evidence':{'page':1,'start':200,'end':209}}]
    result=context_for_table(source,'表格 1',[2])['tables'][0]
    assert result['footnotes']==[note]
    assert result['context']==[{'text':note['text'],'evidence':{'page':1,'start':200,'end':209}}]


def test_physical_font_edge_spill_does_not_select_adjacent_row_and_wide_block_is_unknown():
    table=build_table_semantics({'table_name':'表','page':1,'bbox':[0,0,1,1],
        'row_count':3,'column_count':2,'cells':[
            {'row':row,'col':col,'text':value,'bbox':[(col-1)/2,(row-1)/3,col/2,row/3]}
            for row,values in enumerate([['对象','金额'],['甲','10'],['乙','20']],1)
            for col,value in enumerate(values,1)]},format='pdf',explicit_header_rows=[1])
    source={'pages':[{'number':1,'text':'10\n跨两个格的整行文字','blocks':[
        {'start':0,'end':2,'bbox':[.6,.4,.8,.668]},
        {'start':3,'end':12,'bbox':[.01,.4,.99,.6]}]}], 'metadata':{'table_semantics':[table]}}
    precise=context_for_span(source,1,0,2)
    assert precise['tables'][0]['selected_cell_ids']==['R2C2'] and not precise.get('unresolved_structure')
    wide=context_for_span(source,1,3,12)
    assert wide['unresolved_structure'] and not wide['tables'][0]['selected_cell_ids']
    spans=table_text_spans(source,1)
    assert any(r['start']==3 and r['end']==12 and r['row'] is None for r in spans['rows'])
