"""A same-number match cannot substitute for source row/column/unit identity."""
import copy

import pytest

from backend.table_bindings import check_table_bindings, table_relation_review_passed
from backend.table_context import context_for_span


def fixture():
    table = {'table_name': '统计表', 'header_rows': [1], 'header_basis': 'explicit',
             'selected_cell_ids': ['R2C2'], 'selection_state': 'source_span',
             'columns': [{'column': 1, 'header_path': [{'text': '地区'}]},
                         {'column': 2, 'header_path': [{'text': '2024年'}, {'text': '收入'}]},
                         {'column': 3, 'header_path': [{'text': '2023年'}, {'text': '收入'}]}],
             'row_headers': [{'row': 2, 'cell_id': 'R2C1', 'text': '甲地区'},
                             {'row': 3, 'cell_id': 'R3C1', 'text': '乙地区'}],
             'cells': [{'cell_id': 'R2C1', 'row': 2, 'col': 1, 'text': '甲地区'},
                       {'cell_id': 'R2C2', 'row': 2, 'col': 2, 'text': '-7.2'},
                       {'cell_id': 'R3C1', 'row': 3, 'col': 1, 'text': '乙地区'},
                       {'cell_id': 'R3C2', 'row': 3, 'col': 2, 'text': '-7.2'}],
             'units': [{'scope': 'column', 'column': 2, 'value': '万元', 'text': '万元'}],
             'footnotes': [{'text': '注：仅本地收入。'}]}
    record = {'id': 'E1', 'source_id': 's', 'sha256': 'hash', 'parser_version': 'test',
              'table_context': {'has_table': True, 'tables': [table], 'truncated': False}}
    text = '甲地区2024年收入为-7.2万元。'
    binding = {'evidence_id': 'E1', 'table_name': '统计表', 'cell_id': 'R2C2', 'value': '-7.2',
               'column_label': '2024年 / 收入', 'row_label': '甲地区', 'unit': '万元', 'statement': text}
    return record, table, text, binding


def test_exact_multilevel_fact_keeps_sign_unit_and_footnote_without_mutating_source():
    record, table, text, binding = fixture(); before = copy.deepcopy(record)
    checked = check_table_bindings(text, [binding], [record])
    assert checked['required'] and checked['valid'], checked['errors']
    assert checked['bindings'][0]['footnotes'] == table['footnotes']
    assert checked['bindings'][0]['sha256'] == 'hash' and record == before


@pytest.mark.parametrize('change', ['sign', 'row', 'period', 'table', 'covered', 'rowspan', 'colspan',
                                    'unknown', 'truncated', 'ambiguous', 'scale', 'ratio_unit', 'unbound_number'])
def test_same_value_cannot_bypass_its_real_relationship(change):
    record, table, text, binding = fixture()
    if change == 'sign': text = text.replace('-7.2', '7.2')
    elif change == 'row': text = text.replace('甲地区', '乙地区'); binding['row_label'] = '乙地区'
    elif change == 'period': text = text.replace('2024', '2023'); binding['column_label'] = '2023年 / 收入'
    elif change == 'table': binding['table_name'] = '另一张统计表'
    elif change == 'covered': table['cells'][1]['covered_by'] = [1, 2]
    elif change in {'rowspan', 'colspan'}: table['cells'][1][change] = 2
    elif change == 'unknown': table['header_basis'] = 'unknown'
    elif change == 'truncated': record['table_context']['truncated'] = True
    elif change == 'ambiguous': table['units'][0]['scope'] = 'ambiguous'
    elif change == 'scale': text = text.replace('万元', '元'); binding['unit'] = '元'
    elif change == 'ratio_unit': table['units'][0]['value'] = '万元/人'
    elif change == 'unbound_number': text += '乙地区收入为999万元。'
    binding['statement'] = text
    assert not check_table_bindings(text, [binding], [record])['valid']


def test_legacy_docx_context_is_not_flat_text_permission():
    source = {'format': 'docx', 'pages': [{'number': 1, 'text': '甲 | 10'}],
              'metadata': {'table_layouts': [{'name': '旧表'}]}}
    context = context_for_span(source, 1, 0, 6)
    checked = check_table_bindings('甲收入为10', None, [{'table_context': context}])
    assert checked['required'] and not checked['valid']
    # Defensive compatibility with an older adapter's table-level marker.
    context.pop('unresolved_structure', None)
    checked = check_table_bindings('甲收入为10', None, [{'table_context': context}])
    assert checked['required'] and not checked['valid']


def test_extra_fact_with_same_number_needs_its_own_complete_binding():
    record, table, text, binding = fixture()
    assert not check_table_bindings(text + '乙地区2024年收入也为-7.2万元。', [binding], [record])['valid']


def test_known_cell_cannot_cover_a_second_unknown_aggregate_table():
    record, table, text, binding = fixture()
    unknown = copy.deepcopy(record); unknown['id'] = 'E2'
    unknown['table_context']['tables'][0].update(table_name='旧表', header_basis='unknown', selected_cell_ids=[])
    unknown.update(lineage='table', query={'sql':'SELECT SUM(x) FROM 旧表'}, query_result={'rows':[[10]]})
    assert not check_table_bindings(text, [binding], [record, unknown])['valid']


def test_aggregate_scope_never_claims_fictitious_contributing_rows_and_can_be_replayed():
    record, table, _, _ = fixture(); table['selected_cell_ids'] = []; table['selection_state'] = 'table_only'
    record.update(lineage='table', query={'sql': 'SELECT SUM(收入) FROM 统计表', 'parameters': []},
                  query_result={'columns': ['总收入'], 'rows': [[10]]})
    checked = check_table_bindings('总收入为10。', None, [record])
    assert checked['required'] and checked['valid']
    scope = checked['bindings'][0]
    assert scope['kind'] == 'query_scope' and 'row' not in scope and 'cell_id' not in scope
    assert check_table_bindings('总收入为10。', checked['bindings'], [record])['valid']
    assert not check_table_bindings('总收入为-10。', None, [record])['valid']
    assert not check_table_bindings('总收入为10。', [{'cell_id': 'R2C2'}], [record])['valid']


@pytest.mark.parametrize('rows', [None, [], [{'id': 'C1', 'supported': True}],
    [{'id': 'C1', 'supported': True}, {'id': 'C1', 'supported': True}],
    [{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': False}]])
def test_every_relation_requires_unique_positive_review(rows):
    assert not table_relation_review_passed({'table_relations': rows}, ['C1', 'C2'])


def test_relationship_review_allows_exact_set_only():
    review = {'table_relations': [{'id': 'C2', 'supported': True}, {'id': 'C1', 'supported': True}]}
    assert table_relation_review_passed(review, ['C1', 'C2'])
    assert not table_relation_review_passed(review, ['C1'])


def anchored_fixture():
    record,table,text,binding=fixture()
    table.update(format='pdf',page=2,source_position={'page':2,'bbox':[.1,.2,.9,.9],'page_unit':'physical_page'},
                 caption={'text':'表1','evidence':{'page':2,'start':0,'end':2,'bbox':[.1,.1,.2,.15]}},
                 context=[{'text':'2024 年收入统计','side':'before',
                           'evidence':{'page':2,'start':3,'end':15,'bbox':[.3,.1,.8,.15]}}])
    return record,table,text,binding


def growth_fixture():
    record,table,_,binding=anchored_fixture()
    table['columns'][2]['header_path']=[{'text':'比上年增长'}]
    table['cells'][1].update(cell_id='R2C3',col=3)
    table['selected_cell_ids']=['R2C3']
    table['units']=[{'scope':'column','column':3,'text':'%','value':'%'}]
    binding.update(cell_id='R2C3',column_label='比上年增长',unit='%')
    text='甲地区2024年比上年增长为-7.2%。'
    binding['statement']=text
    return record,table,text,binding


def test_true_caption_page_and_same_table_period_are_positioned_annotations_not_permitted_numbers():
    record,table,text,binding=anchored_fixture();before=copy.deepcopy(record)
    text='表1（第2页）中'+text;binding['statement']=text
    checked=check_table_bindings(text,[binding],[record])
    assert checked['valid'],checked['errors']
    assert {a['kind'] for a in checked['context_annotations']}=={'table_caption','physical_page','table_period'}
    assert checked['bindings'][0]['context_annotations']==checked['context_annotations']
    assert len(checked['numeric_text'])==len(text) and '-7.2万元' in checked['numeric_text']
    for annotation in checked['context_annotations']:
        assert text[annotation['start']:annotation['end']]==annotation['text']
        assert annotation['table_name']=='统计表' and annotation['source_anchor']['page']==2
    assert '表1' not in checked['numeric_text'] and '2024年' not in checked['numeric_text']
    assert record==before


def test_explicit_annual_table_title_can_scope_growth_without_becoming_an_extra_value_column():
    record,_,text,binding=growth_fixture()
    checked=check_table_bindings(text,[binding],[record])
    assert checked['valid'],checked['errors']
    assert [a['text'] for a in checked['context_annotations']]==['2024年']
    assert checked['bindings'][0]['column_label']=='比上年增长'


@pytest.mark.parametrize('prefix',['表2中','第1页中','2025年'])
def test_false_table_page_or_year_annotation_is_never_suppressed(prefix):
    record,_,text,binding=anchored_fixture()
    text=prefix+text;binding['statement']=text
    checked=check_table_bindings(text,[binding],[record])
    assert not checked['valid']
    assert prefix in checked['numeric_text']


@pytest.mark.parametrize('suffix',['补助为1万元。','另有2万元。','另有2024万元。'])
def test_real_location_or_year_number_cannot_reappear_as_a_business_value(suffix):
    record,_,text,binding=anchored_fixture()
    text='表1第2页中'+text+suffix;binding['statement']=text
    checked=check_table_bindings(text,[binding],[record])
    assert not checked['valid']
    assert suffix in checked['numeric_text']


@pytest.mark.parametrize('change',['wrong_table','year_from_other_table','unanchored','wrong_page_anchor','footnote','publication','range'])
def test_context_must_belong_to_the_successfully_bound_table_and_explicit_anchored_period(change):
    record,table,text,binding=growth_fixture();records=[record]
    if change=='wrong_table':
        other=copy.deepcopy(record);other['id']='E2'
        other['table_context']['tables'][0].update(table_name='另一张表',caption={'text':'表2','evidence':{'page':2,'start':0,'end':2}})
        records.append(other);text='表2中'+text;binding['statement']=text
    elif change=='year_from_other_table':
        other=copy.deepcopy(record);other['id']='E2';other['table_context']['tables'][0]['table_name']='另一张表'
        records.append(other);table['context']=[]
    elif change=='unanchored':table['context'][0]['evidence']={'page':2}
    elif change=='wrong_page_anchor':table['context'][0]['evidence']['page']=3
    elif change=='footnote':table['footnotes']=table.pop('context')
    elif change=='publication':table['context'][0]['text']='2024年发布'
    elif change=='range':table['context'][0]['text']='2023—2024年统计'
    assert not check_table_bindings(text,[binding],records)['valid']


def test_annotations_do_not_bypass_original_sign_unit_row_or_statement_contracts():
    record,_,text,binding=anchored_fixture()
    text='表1中'+text.replace('-7.2','7.2');binding['statement']=text
    assert not check_table_bindings(text,[binding],[record])['valid']
    record,_,text,binding=anchored_fixture();text='表1中'+text
    binding['statement']='完全不同的子句'
    assert not check_table_bindings(text,[binding],[record])['valid']


def test_non_table_contract_keeps_text_and_has_no_context_annotations():
    text='第2页表1记载2024年收入。'
    checked=check_table_bindings(text,None,[{'id':'E1','quote':text}])
    assert checked['valid'] and not checked['required']
    assert checked['numeric_text']==text and checked['context_annotations']==[]


def task_title_fixture():
    def anchor(start, end, **extra):
        return {'page': 1, 'start': start, 'end': end, 'page_unit': 'logical_section', **extra}
    titles = ['石景山区人形机器人产业发展', '实施方案（2024年）重点任务分工']
    columns = [{'column': col, 'header_path': [{'text': label,
        'evidence': anchor(100 + col * 10, 104 + col * 10, row=1, col=col)}]}
        for col, label in enumerate(['序号', '牵头单位', '配合单位', '任务分工'], 1)]
    table = {'table_name': '表格 1', 'format': 'docx', 'page': 1,
        'source_position': anchor(100, 400, body_index=4), 'header_rows': [1], 'header_basis': 'explicit',
        'selected_cell_ids': ['R6C1', 'R6C2', 'R6C3', 'R6C4'], 'columns': columns,
        'context': [{'text': titles[0], 'side': 'before', 'evidence': anchor(10, 23, body_index=1)},
                    {'text': titles[1], 'side': 'before', 'evidence': anchor(24, 41, body_index=2)}],
        'row_headers': [{'row': 6, 'column': 1, 'cell_id': 'R6C1', 'text': '5',
            'role': 'row_key', 'role_basis': 'explicit_key', 'evidence': anchor(200, 201, row=6, col=1)}],
        'cells': [{'cell_id': f'R6C{col}', 'row': 6, 'col': col, 'text': value}
                  for col, value in enumerate(['5', '甲单位', '乙单位', '搭建平台完整任务说明'], 1)],
        'units': [], 'footnotes': []}
    record = {'id': 'E1', 'source_id': 's', 'sha256': 'h', 'parser_version': 'test',
              'table_context': {'has_table': True, 'tables': [table]}}
    text = '在《' + ''.join(titles) + '》表中，序号5任务的牵头单位为甲单位。'
    binding = {'evidence_id': 'E1', 'table_name': '表格 1', 'cell_id': 'R6C2', 'value': '甲单位',
               'column_label': '牵头单位', 'row_label': '5', 'statement': text}
    return record, table, text, binding


def test_exact_consecutive_table_title_and_current_row_key_are_locators_only():
    record, table, text, binding = task_title_fixture(); before = copy.deepcopy(record)
    checked = check_table_bindings(text, [binding], [record])
    assert checked['valid'], checked['errors']
    annotations = {a['kind']: a for a in checked['context_annotations']}
    assert set(annotations) == {'table_title', 'row_key'}
    assert len(annotations['table_title']['source_anchors']) == 2
    assert annotations['table_title']['source_anchor']['start'] == 10
    assert annotations['table_title']['source_anchor']['end'] == 41
    assert annotations['row_key']['cell_id'] == 'R6C1'
    assert annotations['row_key']['header_anchors'][0]['col'] == 1
    assert '牵头单位为甲单位' in checked['numeric_text']
    assert '任务分工' not in checked['numeric_text'] and '序号5' not in checked['numeric_text']
    assert len(checked['numeric_text']) == len(text) and record == before
    for item in checked['context_annotations']:
        assert text[item['start']:item['end']] == item['text']


@pytest.mark.parametrize('replacement', ['序号50', '序号2025', '序号5.2', '序号6', '序号5万元'])
def test_row_key_annotation_needs_complete_current_value_with_numeric_boundary(replacement):
    record, _, text, binding = task_title_fixture()
    text = text.replace('序号5', replacement); binding['statement'] = text
    checked = check_table_bindings(text, [binding], [record])
    assert not checked['valid']
    assert not any(a['kind'] == 'row_key' for a in checked['context_annotations'])


@pytest.mark.parametrize('change', ['year', 'injected_amount', 'truncated_title', 'gap', 'body_gap',
                                   'wrong_page', 'after', 'key_anchor', 'key_header_anchor'])
def test_title_and_key_are_not_fuzzy_or_unanchored_metadata_permissions(change):
    record, table, text, binding = task_title_fixture()
    if change == 'year': text = text.replace('2024年', '2025年')
    elif change == 'injected_amount': text = text.replace('重点任务分工', '重点任务分工5万元')
    elif change == 'truncated_title': text = text.replace('石景山区人形机器人产业发展', '')
    elif change == 'gap': table['context'][1]['evidence'].update(start=26, end=43)
    elif change == 'body_gap': table['context'][1]['evidence']['body_index'] = 3
    elif change == 'wrong_page': table['context'][1]['evidence']['page'] = 2
    elif change == 'after': table['context'][1]['side'] = 'after'
    elif change == 'key_anchor': table['row_headers'][0]['evidence']['col'] = 2
    elif change == 'key_header_anchor': table['columns'][0]['header_path'][0]['evidence']['page'] = 2
    binding['statement'] = text
    assert not check_table_bindings(text, [binding], [record])['valid']


@pytest.mark.parametrize('suffix', ['任务分工为甲单位。', '另有2024万元。', '另有5万元。'])
def test_exact_title_or_key_does_not_exempt_business_text_elsewhere(suffix):
    record, _, text, binding = task_title_fixture()
    text += suffix; binding['statement'] = text
    checked = check_table_bindings(text, [binding], [record])
    assert not checked['valid']
    assert suffix in checked['numeric_text']


def test_quoted_title_normalization_removes_only_whitespace_and_never_borrows_other_table():
    record, table, text, binding = task_title_fixture()
    text = text.replace('发展实施', '发展\n 实施'); binding['statement'] = text
    assert check_table_bindings(text, [binding], [record])['valid']
    other = copy.deepcopy(record); other['id'] = 'E2'
    other['table_context']['tables'][0]['table_name'] = '另一表'
    table['context'] = []
    assert not check_table_bindings(text, [binding], [record, other])['valid']


@pytest.mark.parametrize('prefix,opening,closing', [('附表', '“', '”'), ('表题：', '“', '”'),
                                                   ('附表', '"', '"'), ('表题:', '"', '"')])
@pytest.mark.parametrize('separator', ['为', '：', ':'])
def test_explicit_double_quoted_title_and_separated_row_key_are_exact_locators(prefix, opening, closing, separator):
    record, _, text, binding = task_title_fixture()
    text = text.replace('《', prefix + opening).replace('》', closing).replace('序号5', '序号' + separator + '5')
    binding['statement'] = text
    checked = check_table_bindings(text, [binding], [record])
    assert checked['valid'], checked['errors']
    assert {a['kind'] for a in checked['context_annotations']} == {'row_key', 'table_title'}
    for item in checked['context_annotations']:
        assert text[item['start']:item['end']] == item['text']
    assert '甲单位' in checked['numeric_text'] and len(checked['numeric_text']) == len(text)


@pytest.mark.parametrize('change', ['plain_quote', 'distant_prefix', 'wrong_title', 'wrong_year', 'title_amount',
                                   'key50', 'key2025', 'key6', 'key_decimal', 'key_amount', 'key_days',
                                   'extra_key_amount', 'extra_year_amount'])
def test_double_quotes_and_key_separators_never_create_metadata_or_business_values(change):
    record, _, text, binding = task_title_fixture()
    text = text.replace('《', '附表“').replace('》', '”').replace('序号5', '序号为5')
    if change == 'plain_quote': text = text.replace('附表“', '“')
    elif change == 'distant_prefix': text = text.replace('附表“', '附表说明中提到“')
    elif change == 'wrong_title': text = text.replace('人形机器人', '工业机器人')
    elif change == 'wrong_year': text = text.replace('2024年', '2025年')
    elif change == 'title_amount': text = text.replace('任务分工”', '任务分工5万元”')
    elif change.startswith('key'):
        value = {'key50': '50', 'key2025': '2025', 'key6': '6', 'key_decimal': '5.2',
                 'key_amount': '5万元', 'key_days': '5天'}[change]
        text = text.replace('序号为5', '序号为' + value)
    elif change == 'extra_key_amount': text += '另有5万元。'
    elif change == 'extra_year_amount': text += '另有2024万元。'
    binding['statement'] = text
    assert not check_table_bindings(text, [binding], [record])['valid']


@pytest.mark.parametrize('unit', ['小时', '天', '千克', '公斤', '公里', '平方米'])
@pytest.mark.parametrize('locator', ['row_key', 'caption', 'page', 'period', 'full_title'])
def test_every_location_annotation_rejects_the_complete_business_unit_set(unit, locator):
    if locator in {'row_key', 'full_title'}:
        record, table, _, binding = task_title_fixture()
        table['columns'][1]['header_path'][0]['text'] = '工时'
        table['cells'][1]['text'] = '10'
        table['units'] = [{'scope': 'column', 'column': 2, 'value': unit}]
        binding.update(value='10', column_label='工时', unit=unit)
        if locator == 'row_key':
            text = f'序号5{unit}的搭建平台完整任务说明工时为10{unit}。'
        else:
            title = ''.join(entry['text'] for entry in table['context'])
            text = f'附表“{title}”{unit}中序号为5的任务工时为10{unit}。'
    else:
        record, table, text, binding = anchored_fixture()
        table['units'][0]['value'] = unit
        binding['unit'] = unit
        text = text.replace('万元', unit)
        prefix = {'caption': '表1', 'page': '第2页', 'period': '2024年'}[locator]
        text = prefix + unit + '中' + text
    binding['statement'] = text
    checked = check_table_bindings(text, [binding], [record])
    assert not checked['valid']
    assert (f'5{unit}' if locator == 'row_key' else unit) in checked['numeric_text']
