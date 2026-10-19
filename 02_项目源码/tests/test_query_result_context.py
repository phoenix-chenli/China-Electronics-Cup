import copy
import pytest
from backend.query_result_context import build_query_result_context, check_query_bindings


def evidence(rows=None):
    e={'id':'E1','kind':'table','source_id':'s1','sha256':'a'*64,'parser_version':'p1','semantic_revision':3,
        'stale':False,'direct_columns':{c:'measurements.'+c for c in ['period','site','checks','unit']},'query':{'sql':'SELECT period, site, checks, unit FROM measurements','parameters':[]},
        'query_result':{'columns':['period','site','checks','unit'],'rows':rows or [
            ['2025-02','东岸',30,'次'],['2025-02','西岸',30,'次']], 'truncated':False}}
    e['query_result_context']=build_query_result_context(e)
    return e


def bound(e,row=0,text='2025-02东岸巡检30次。'):
    ctx=e['query_result_context'];cells=ctx['rows'][row]['cells']
    return {'evidence_id':e['id'],'context_id':ctx['context_id'],'cell_id':cells[2]['cell_id'],
        'row_index':row,'column_index':2,'column_label':'checks','value':30,
        'entity_cells':[cells[0]['cell_id'],cells[1]['cell_id']],'unit':'次','unit_column':'unit','statement':text}


def test_success_is_output_scope_and_does_not_mutate():
    e=evidence();b=bound(e);before=copy.deepcopy((e,b));r=check_query_bindings(b['statement'],[b],[e])
    assert r['required'] and r['valid'] and r['bindings'][0]['semantic_review_required']
    assert r['bindings'][0]['localization_scope']=='query_output_not_original_cell'
    assert '30' not in r['numeric_text'] and '2025' not in r['numeric_text']
    assert (e,b)==before


@pytest.mark.parametrize('field,value',[('sha256','b'*64),('source_id','other'),('parser_version','p2'),('semantic_revision',4)])
def test_version_change_invalidates_existing_binding(field,value):
    e=evidence();b=bound(e);e[field]=value
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


@pytest.mark.parametrize('mutate',[
    lambda e:e['query']['parameters'].append('东岸'),
    lambda e:e['query'].update(sql='SELECT period, site, checks, unit FROM measurements WHERE 1=1'),
    lambda e:e['query_result']['rows'][0].__setitem__(2,31),
    lambda e:e['query_result']['rows'].reverse(),
    lambda e:e['query_result']['columns'].__setitem__(2,'rechecks'),
    lambda e:e['query_result'].__setitem__('truncated',True),
])
def test_query_or_result_change_invalidates(mutate):
    e=evidence();b=bound(e);mutate(e)
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_formatting_does_not_change_query_identity():
    e=evidence();original=e['query_result_context']['context_id']
    e['query']['sql']='SELECT  period, site, checks, unit\nFROM measurements'
    assert build_query_result_context(e)['context_id']==original


@pytest.mark.parametrize('field,value',[('cell_id','r1c3'),('context_id','invented'),('evidence_id','E2'),
    ('row_index',1),('column_index',1),('column_label','rechecks'),('value',-30),('unit','元')])
def test_wrong_identity_field_or_value_rejected(field,value):
    e=evidence();b=bound(e);b[field]=value
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_same_value_wrong_row_cannot_borrow_entity():
    e=evidence();b=bound(e,row=1);b['entity_cells']=[e['query_result_context']['rows'][0]['cells'][1]['cell_id']]
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_same_value_other_row_requires_its_actual_entity_in_statement():
    e=evidence();b=bound(e,row=1)
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_repeated_unit_is_not_unique_entity():
    e=evidence();b=bound(e);b['entity_cells']=[e['query_result_context']['rows'][0]['cells'][3]['cell_id']]
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_composite_entity_includes_period_when_site_repeats():
    e=evidence([['2025-01','东岸',30,'次'],['2025-02','东岸',30,'次']]);b=bound(e,1)
    b['entity_cells']=[e['query_result_context']['rows'][1]['cells'][1]['cell_id']]
    assert not check_query_bindings(b['statement'],[b],[e])['valid']
    b['entity_cells'].append(e['query_result_context']['rows'][1]['cells'][0]['cell_id'])
    assert check_query_bindings(b['statement'],[b],[e])['valid']


@pytest.mark.parametrize('statement',['2025-02东岸巡检-30次。','2025-02东岸巡检300次。',
    '2025-02东岸巡检30次，另有30元。','2025-02东岸巡检30次，另有2025元。'])
def test_extra_numeric_fact_or_changed_sign_is_not_authorized(statement):
    e=evidence();b=bound(e,text=statement)
    assert not check_query_bindings(statement,[b],[e])['valid']


def test_statement_must_be_exact_and_unambiguous():
    e=evidence();b=bound(e)
    assert not check_query_bindings('东岸巡检30次。',[b],[e])['valid']
    assert not check_query_bindings(b['statement']*2,[b],[e])['valid']


def test_numeric_entity_substring_cannot_bind_different_identifier():
    e=evidence([['2025-02','A01',30,'次'],['2025-02','A010',30,'次']]);b=bound(e,text='2025-02 A010巡检30次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_single_row_can_omit_entity_but_semantic_review_remains_required():
    e=evidence([['2025-02','东岸',30,'次']]);b=bound(e,text='巡检30次。');b['entity_cells']=[]
    assert check_query_bindings(b['statement'],[b],[e])['bindings'][0]['semantic_review_required']


def test_no_auto_binding_or_original_cell_translation():
    e=evidence();r=check_query_bindings('巡检30次。',[],[e]);assert r['required'] and not r['valid']
    assert r['bindings']==[] and r['numeric_text']=='巡检30次。'


def test_unrelated_ordinary_text_and_negative_scope_are_not_rewritten():
    text='缺少税率，无法给出含税金额。';r=check_query_bindings(text,[],[evidence()])
    assert not r['required'] and r['valid'] and r['numeric_text']==text
    assert check_query_bindings('原文有30人',[],[])['numeric_text']=='原文有30人'


def test_empty_results_have_no_fabricated_cells():
    e=evidence();e['query_result']['rows']=[];ctx=build_query_result_context(e)
    assert ctx['usable'] and ctx['rows']==[] and ctx['row_count']==0


def test_row_budget_only_exposes_complete_rows_and_marks_omissions():
    e=evidence();ctx=build_query_result_context(e,max_rows=1);e['query_result_context']=ctx
    assert ctx['projection_truncated'] and ctx['omitted_rows']==1 and len(ctx['rows'][0]['cells'])==4
    b=bound(e);assert check_query_bindings(b['statement'],[b],[e])['valid']
    full=build_query_result_context(e);b=bound({**e,'query_result_context':full},row=1,text='2025-02西岸巡检30次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_projected_uniqueness_checks_full_result_not_only_visible_rows():
    e=evidence([['2025-01','东岸',30,'次'],['2025-02','东岸',30,'次']]);e['query_result_context']=build_query_result_context(e,max_rows=1)
    b=bound(e,text='东岸巡检30次。');b['entity_cells']=[e['query_result_context']['rows'][0]['cells'][1]['cell_id']]
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_oversized_cell_is_never_silently_cut():
    e=evidence([['2025-02','东岸'*4000,30,'次']]);ctx=build_query_result_context(e)
    assert not ctx['usable'] and not ctx['rows'] and ctx['projection_truncated']


@pytest.mark.parametrize('mutation',[
    lambda e:e.update(stale=True),lambda e:e.pop('parser_version'),
    lambda e:e['query_result'].pop('truncated'),lambda e:e['query_result']['rows'][0].append(1),
    lambda e:e['query_result']['rows'][0].__setitem__(2,float('nan')),
])
def test_invalid_result_fails_closed(mutation):
    e=evidence();mutation(e);assert not build_query_result_context(e)['usable']


def test_null_is_not_a_published_zero():
    e=evidence([['2025-02','东岸',None,'次']]);b=bound(e,text='东岸巡检0次。');b['value']=0
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_no_global_number_authorization_outside_bound_statement():
    e=evidence();b=bound(e);text=b['statement']+'另有30万元。';r=check_query_bindings(text,[b],[e])
    assert r['valid'] and '30万元' in r['numeric_text']

@pytest.mark.parametrize('field,value',[('cell_id',{}),('evidence_id',None),('context_id',[]),('entity_cells',[{}]),('entity_cells','R1C1')])
def test_malformed_model_fields_are_errors_not_exceptions(field,value):
    e=evidence();b=bound(e);b[field]=value
    r=check_query_bindings(b['statement'],[b],[e]);assert not r['valid'] and r['errors']


def test_ordinary_number_cannot_become_percentage():
    e=evidence();b=bound(e,text='2025-02东岸巡检30%，单位为次。')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_actual_r4_plain_csv_shape_can_use_query_cells_without_fake_original_grid():
    e={'id':'E3','kind':'table','source_id':'source-tariff','sha256':'f'*64,'parser_version':'p1',
       'query':{'sql':'SELECT * FROM \"数据表\"','parameters':[]},
       'direct_columns':{c:'数据表.'+c for c in ['时段','对象','用电量','计量单位','适用范围']},
       'query_result':{'columns':['时段','对象','用电量','计量单位','适用范围'],
          'rows':[['2025-07-01','全站','2610','kWh','不含税'],['2025-07-02','全站','2400','kWh','不含税'],['2025-07-01','泵P-17','180','kWh','不含税']], 'truncated':False}}
    e['query_result_context']=build_query_result_context(e);ctx=e['query_result_context'];cells=ctx['rows'][2]['cells']
    text='2025-07-01泵P-17用电量为180kWh。'
    b={'evidence_id':'E3','context_id':ctx['context_id'],'cell_id':cells[2]['cell_id'],
       'row_index':2,'column_index':2,'column_label':'用电量','value':'180',
       'entity_cells':[cells[0]['cell_id'],cells[1]['cell_id']],'unit':'kWh','unit_column':'计量单位','statement':text}
    r=check_query_bindings(text,[b],[e]);assert r['valid']
    assert r['bindings'][0]['localization_scope']=='query_output_not_original_cell'
    assert not r['bindings'][0].get('source_cell') and '180' not in r['numeric_text']


def test_constant_unit_alias_is_not_source_unit_proof():
    e=evidence();e['query']['sql']="SELECT period,site,checks,'元' AS unit FROM measurements"
    for row in e['query_result']['rows']:row[3]='元'
    e['direct_columns'].pop('unit');e['query_result_context']=build_query_result_context(e)
    b=bound(e,text='2025-02东岸巡检30元。');b['unit']='元'
    r=check_query_bindings(b['statement'],[b],[e]);assert not r['valid'] and '直接字段' in ' '.join(r['errors'])


def test_unit_from_other_table_cannot_be_borrowed():
    e=evidence();e['direct_columns']['unit']='unrelated.unit';e['query_result_context']=build_query_result_context(e)
    b=bound(e);assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_without_unit_column_arbitrary_row_unit_cannot_be_borrowed():
    e=evidence();b=bound(e);b.pop('unit_column')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_supported_output_column_unit_is_valid_without_extra_unit_column():
    e=evidence();e['column_units']={'checks':{'unit':'次','source_columns':['measurements.checks_次数'],'method':'supported_sql_expression'}}
    e['query_result_context']=build_query_result_context(e);b=bound(e);b.pop('unit_column')
    assert check_query_bindings(b['statement'],[b],[e])['valid']


def test_unit_provenance_change_invalidates_context():
    e=evidence();b=bound(e);e['direct_columns'].pop('unit')
    assert not check_query_bindings(b['statement'],[b],[e])['valid']


def test_percent_source_has_exact_percentage_unit_proof():
    e=evidence([['2025-02','东岸',30,'%']]);b=bound(e,text='2025-02东岸比例为30%。');b['unit']='%'
    assert check_query_bindings(b['statement'],[b],[e])['valid']
