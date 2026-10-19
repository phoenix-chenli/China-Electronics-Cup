"""Chart text is source-anchored; geometry never supplies an unprinted value."""
import copy
import json

import pytest

from backend.chart_context import (VERSION, chart_relation_review_passed,
                                   check_chart_bindings, context_for_page)


def block(text, box, **extra):
    return {'text': text, 'bbox': box, **extra}


def source(blocks=None):
    rows = blocks or [block('图1 2024年销售额', [.1, .05, .7, .10]),
                      block('甲12万元', [.2, .3, .4, .35]),
                      block('乙9万元', [.6, .3, .8, .35])]
    text = '\n'.join(row['text'] for row in rows)
    cursor = 0
    for row in rows:
        row.update(start=cursor, end=cursor + len(row['text']))
        cursor += len(row['text']) + 1
    return {'id': 's1', 'sha256': 'sha1', 'parser_version': '2.2',
            'pages': [{'number': 1, 'text': text, 'blocks': rows}]}


def fixture():
    src = source()
    record = {'id': 'E1', 'source_id': 's1', 'sha256': 'sha1', 'parser_version': '2.2',
              'page': 1, 'chart_context': context_for_page(src, 1)}
    text = '甲的销售额为12万元。'
    binding = {'kind': 'printed_point', 'evidence_id': 'E1', 'statement': text,
               'category': '甲', 'category_observation_id': 'P1B1',
               'value': '12', 'value_observation_id': 'P1B1',
               'unit': '万元', 'unit_observation_id': 'P1B1', 'association': 'same_line'}
    return record, text, binding


def test_exact_point_is_replayable_and_keeps_real_source_offsets():
    record, text, binding = fixture(); before = copy.deepcopy(record)
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'], checked['errors']
    point = checked['bindings'][0]
    assert point['field_observations']['value']['text'] == '甲12万元'
    assert point['field_observations']['value']['bbox'] == [.2, .3, .4, .35]
    assert point['field_observations']['value']['start'] >= 0
    assert point['requires_visual_review'] and record == before
    # Normalized records deliberately retain source ids and field ids for replay.
    assert point['source_id'] == 's1' and point['sha256'] == 'sha1'
    assert check_chart_bindings(text, checked['bindings'], [record], required=True)['valid']


@pytest.mark.parametrize('change', ['source', 'page', 'observation', 'category', 'sign', 'percent',
                                    'partial_number', 'unit_scale', 'invented_number', 'pending',
                                    'unknown_version', 'statement', 'uncovered'])
def test_literal_values_cannot_bypass_source_or_fact_boundaries(change):
    record, text, binding = fixture()
    if change == 'source': record['chart_context']['source_id'] = 'other'
    elif change == 'page': record['chart_context']['page'] = 2
    elif change == 'observation': binding['value_observation_id'] = 'P1B99'
    elif change == 'category': binding['category'] = '乙'; text = text.replace('甲', '乙')
    elif change == 'sign': text = text.replace('12', '-12')
    elif change == 'percent': text = text.replace('12', '12%')
    elif change == 'partial_number': binding['value'] = '2'; text = text.replace('12', '2')
    elif change == 'unit_scale': binding['unit'] = '元'; text = text.replace('万元', '元')
    elif change == 'invented_number': binding['value'] = '13'; text = text.replace('12', '13')
    elif change == 'pending': record['chart_context']['observations'][1]['status'] = 'pending'
    elif change == 'unknown_version': record['chart_context']['version'] = 'legacy'
    elif change == 'statement': binding['statement'] = '乙的销售额为9万元。'
    elif change == 'uncovered': text += '乙的销售额为12万元。'
    if change not in {'statement', 'uncovered'}: binding['statement'] = text
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']


def test_same_page_automatic_resolution_does_not_cross_source_or_page():
    record, text, binding = fixture(); binding.pop('evidence_id')
    same = copy.deepcopy(record); same['id'] = 'E2'
    checked = check_chart_bindings(text, [binding], [record, same], required=True)
    assert checked['valid'] and checked['bindings'][0]['evidence_id'] == 'E1'
    same['source_id'] = same['chart_context']['source_id'] = 's2'
    same['sha256'] = same['chart_context']['sha256'] = 'sha2'
    assert not check_chart_bindings(text, [binding], [record, same], required=True)['valid']
    binding['evidence_id'] = 'E1'
    assert check_chart_bindings(text, [binding], [record, same], required=True)['valid']


def test_missing_graph_metadata_can_only_supply_visual_qualitative_claim():
    record = {'id': 'E1', 'source_id': 's1', 'page': 1,
              'chart_context': context_for_page({'id': 's1', 'pages': [{'number': 1}]}, 1)}
    text = '原图未给出可读的精确标签，无法核定精确金额。'
    binding = {'kind': 'visual_observation', 'statement': text, 'observation_type': 'missing_printed_values',
               'observation_ids': []}
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'] and checked['bindings'][0]['requires_visual_review']
    assert not record['chart_context']['has_candidate']
    assert not chart_relation_review_passed({}, ['C1'])
    assert chart_relation_review_passed({'chart_relations': [{'id': 'C1', 'supported': True}]}, ['C1'])
    binding['statement'] = text + '金额为12万元。'
    assert not check_chart_bindings(binding['statement'], [binding], [record], required=True)['valid']


def test_empty_chart_binding_is_not_permission_to_complete_a_chart_answer():
    assert not check_chart_bindings('整体上升。', [], [], required=True)['valid']
    assert check_chart_bindings('普通文字。', None, [], required=False)['valid']


@pytest.mark.parametrize('rows', [None, [], [{'id': 'C1', 'supported': False}],
    [{'id': 'C1', 'supported': 1}], [{'id': 'C1', 'supported': True}, {'id': 'C1', 'supported': True}],
    [{'id': 'C1', 'supported': True}, {'id': 'C2', 'supported': True}], [{'id': [], 'supported': True}]])
def test_relation_review_must_explicitly_cover_each_required_claim_once(rows):
    assert not chart_relation_review_passed({'chart_relations': rows}, ['C1'])


def aligned_fixture():
    src = source([block('图1 销售额', [.1, .02, .7, .07]),
                  block('单位：万元', [.02, .1, .2, .15]),
                  block('甲', [.4, .8, .5, .85]),
                  block('12', [.42, .3, .48, .35])])
    record = {'id': 'E1', 'source_id': 's1', 'sha256': 'sha1', 'parser_version': '2.2',
              'page': 1, 'chart_context': context_for_page(src, 1)}
    text = '甲的销售额为12万元。'
    binding = {'kind': 'printed_point', 'evidence_id': 'E1', 'statement': text,
               'category': '甲', 'category_observation_id': 'P1B2', 'value': '12',
               'value_observation_id': 'P1B3', 'unit': '万元', 'unit_observation_id': 'P1B1',
               'association': 'aligned_vertical'}
    return record, text, binding


def test_unique_geometry_is_only_a_proposal_for_independent_visual_review():
    record, text, binding = aligned_fixture()
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'] and checked['bindings'][0]['requires_visual_review'], checked['errors']
    # The second equally aligned number may be a different series or an axis tick.
    record['chart_context']['observations'].append({'id': 'P1B4', 'text': '18',
                                                  'bbox': [.42, .5, .48, .55], 'status': 'observed'})
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']


@pytest.mark.parametrize('change', ['wrong_position', 'separate_series', 'dual_axis', 'compound_unit'])
def test_geometry_and_axis_ambiguity_cannot_supply_exact_point(change):
    record, text, binding = aligned_fixture()
    rows = record['chart_context']['observations']
    if change == 'wrong_position': next(row for row in rows if row['id'] == 'P1B3')['bbox'] = [.7, .3, .8, .35]
    elif change == 'separate_series':
        rows.append({'id': 'P1B4', 'text': '上年', 'bbox': [.6, .1, .7, .15], 'status': 'observed'})
        binding.update(series='上年', series_observation_id='P1B4')
        text = '上年' + text
    elif change == 'dual_axis':
        rows.append({'id': 'P1B4', 'text': '单位：%', 'bbox': [.8, .1, .9, .15], 'status': 'observed'})
    else: next(row for row in rows if row['id'] == 'P1B1')['text'] = '单位：万元/户'
    binding['statement'] = text
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']


def test_only_true_title_and_period_are_masked_with_actual_statement_offsets():
    record, _, binding = fixture()
    text = '图 1的甲在2024年销售额为12万元。'
    binding.update(statement='图1的甲在2024年销售额为12万元。', title='图1',
                   title_observation_id='P1B0', period='2024年', period_observation_id='P1B0')
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'], checked['errors']
    assert '12万元' in checked['numeric_text'] and '2024' not in checked['numeric_text']
    assert len(checked['numeric_text']) == len(text)
    for annotation in checked['context_annotations']:
        assert text[annotation['start']:annotation['end']] == annotation['text']
    binding['statement'] = text.replace('2024', '2025')
    assert not check_chart_bindings(binding['statement'], [binding], [record], required=True)['valid']


def test_title_number_cannot_be_reused_as_a_business_value():
    record, _, binding = fixture()
    text = '图1小时内甲销售额为12万元。'
    binding.update(statement=text, title='图1', title_observation_id='P1B0')
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert not checked['valid'] and not checked['context_annotations']


def test_real_caption_is_a_hint_not_a_chart_rectangle_or_complete_structure():
    src = source(); before = copy.deepcopy(src)
    context = context_for_page(src, 1)
    assert context['has_candidate'] and context['scope_basis'] == 'page'
    assert context['scope_bbox'] == [0, 0, 1, 1] and context['version'] == VERSION
    assert context['hints']['title_ids'] == ['P1B0'] and src == before
    assert 'charts' not in context and 'points' not in context


def test_budget_is_serialized_whole_observations_and_local_point_can_survive_truncation():
    src = source()
    src['pages'][0]['blocks'] += [block('正文' * 500, [.1, .6, .9, .7]) for _ in range(20)]
    context = context_for_page(src, 1, max_chars=1600)
    assert len(json.dumps(context, ensure_ascii=False)) <= 1600 and context['truncated']
    assert all(len(row['text']) in {len('图1 2024年销售额'), len('甲12万元'), len('乙9万元')} for row in context['observations'])
    record, text, binding = fixture(); record['chart_context'] = context
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'] and checked['bindings'][0]['context_truncated'], checked['errors']
    minimal = context_for_page(src, 1, max_chars=128)
    assert len(json.dumps(minimal, ensure_ascii=False)) <= 128 and minimal['truncated']
    record['chart_context'] = minimal
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']


def test_dedicated_ocr_is_prioritized_and_only_same_text_same_location_is_deduplicated():
    src = source()
    src['pages'][0]['chart_observations'] = [block('甲12万元', [.2, .3, .4, .35], confidence=.98),
        block('甲12万元', [.2, .6, .4, .65], confidence=.98),
        block('乙9万元', [.6, .3, .8, .35], confidence=.3),
        block('模型编造99万元', [.2, .7, .4, .75], confidence=.99, source_method='model')]
    context = context_for_page(src, 1)
    ids = {row['id'] for row in context['observations']}
    assert {'P1O0', 'P1O1', 'P1B2'} <= ids and not {'P1B1', 'P1O2', 'P1O3'} & ids
    assert all('start' not in row for row in context['observations'] if row['origin'] == 'chart_ocr')
    assert next(row for row in context['observations'] if row['id'] == 'P1O0')['status'] == 'observed'


@pytest.mark.parametrize('extra', [{'confidence': .2}, {'confidence': None}, {'confidence': float('nan')},
    {'confidence': .99, 'ocr_disagreement': True}, {'confidence': .99, 'source_pending_reason': 'new text'},
    {'confidence': .99, 'requires_review': True}])
def test_uncertain_ocr_does_not_become_a_verified_printed_value(extra):
    src = source([block('普通正文', [.1, .1, .9, .2])])
    src['pages'][0]['chart_observations'] = [block('甲12万元', [.2, .3, .4, .35], **extra)]
    record, text, binding = fixture(); record['chart_context'] = context_for_page(src, 1)
    for key in ('category', 'value', 'unit'): binding[key + '_observation_id'] = 'P1O0'
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert not checked['valid']


def test_crop_uses_original_coordinates_and_omits_outside_or_model_derived_blocks():
    src = source()
    src['pages'][0]['blocks'].append(block('臆造77万元', [.2, .3, .4, .35], derived_view=True))
    context = context_for_page(src, 1, bbox=[.15, .25, .45, .4])
    assert [row['id'] for row in context['observations']] == ['P1B1']
    assert context['observations'][0]['bbox'] == [.2, .3, .4, .35]
    assert context['scope_basis'] == 'requested_region' and not context['has_candidate']


def test_duplicate_observation_id_does_not_authorize_either_value():
    record, text, binding = fixture()
    record['chart_context']['observations'].append({'id': 'P1B1', 'text': '乙12万元',
                                                  'bbox': [.6, .3, .8, .35], 'status': 'observed'})
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']


def grid_fixture():
    # Selected actual page-5 OCR observations, retaining its uncorrected 收人 label.
    rows = [block('图2', [.1332, .1654, .1745, .1873]),
            block('亿元', [.3419, .2106, .377, .2246]),
            block('%', [.7847, .2094, .8014, .2304]),
            block('2022年', [.5523, .4302, .6099, .4419]),
            block('2023年', [.6323, .4299, .6901, .4422]),
            block('一般公共预算收人', [.2587, .4448, .3799, .458]),
            block('73.8', [.5626, .446, .5996, .458]),
            block('80.2', [.6427, .446, .6797, .458]),
            block('增速', [.2279, .462, .2895, .474]),
            block('0.6', [.5688, .462, .5955, .474]),
            block('8.7', [.6468, .462, .6756, .474])]
    src = source(rows)
    record = {'id': 'E1', 'source_id': 's1', 'page': 1, 'chart_context': context_for_page(src, 1)}
    text = '2022年一般公共预算收人为73.8亿元。'
    binding = {'kind': 'printed_point', 'statement': text, 'evidence_id': 'E1',
               'category': '2022年', 'category_observation_id': 'P1B3',
               'series': '一般公共预算收人', 'series_observation_id': 'P1B5',
               'value': '73.8', 'value_observation_id': 'P1B6',
               'unit': '亿元', 'unit_observation_id': 'P1B1',
               'title': '图2', 'title_observation_id': 'P1B0',
               'association': 'grid_point', 'axis_side': 'left'}
    return record, text, binding


def test_two_series_grid_uses_real_row_and_column_intersection_without_guessing_color():
    record, text, binding = grid_fixture()
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'], checked['errors']
    assert checked['bindings'][0]['axis_side'] == 'left'
    assert checked['bindings'][0]['requires_visual_review']
    assert check_chart_bindings(text, checked['bindings'], [record], required=True)['valid']
    # A percent axis may supply the ratio glyph, but never the point's digits.
    binding.update(series='增速', series_observation_id='P1B8', value='0.6', value_observation_id='P1B9',
                   unit='%', unit_observation_id='P1B2', axis_side='right', statement='2022年增速为0.6%。')
    checked = check_chart_bindings(binding['statement'], [binding], [record], required=True)
    assert checked['valid'], checked['errors']


@pytest.mark.parametrize('change', ['wrong_column', 'wrong_row', 'wrong_axis', 'missing_year',
                                    'missing_title', 'wrong_title', 'ambiguous_cell', 'pending_series', 'partial_series'])
def test_grid_rejects_missing_or_incompatible_relationship_anchors(change):
    record, text, binding = grid_fixture()
    rows = record['chart_context']['observations']
    if change == 'wrong_column':
        binding.update(category='2023年', category_observation_id='P1B4'); text = text.replace('2022', '2023')
    elif change == 'wrong_row':
        binding.update(series='增速', series_observation_id='P1B8'); text = text.replace('一般公共预算收人', '增速')
    elif change == 'wrong_axis': binding['axis_side'] = 'right'
    elif change == 'missing_year':
        binding.update(category='2021年', category_observation_id='P1B99'); text = text.replace('2022', '2021')
    elif change == 'missing_title': binding.pop('title'); binding.pop('title_observation_id')
    elif change == 'wrong_title':
        rows.append({'id': 'P1B11', 'text': '图3', 'bbox': [.13, .2, .18, .22], 'status': 'observed'})
    elif change == 'ambiguous_cell':
        rows.append({'id': 'P1B11', 'text': '99.9', 'bbox': [.5626, .446, .5996, .458], 'status': 'observed'})
    elif change == 'pending_series': next(row for row in rows if row['id'] == 'P1B5')['status'] = 'pending'
    else: binding['series'] = '公共预算'
    binding['statement'] = text
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']


def pie_fixture():
    # The real page-9 caption is low-confidence; it is not promoted or required
    # for a wholly observed, unique adjacent label pair without a figure-number claim.
    src = source([block('图5', [.1335, .5261, .1766, .5451], confidence=.5),
                  block('其他用品及服务', [.3491, .5914, .4682, .6061], confidence=1.),
                  block('2.9%', [.386, .6076, .4312, .6207], confidence=1.),
                  block('口 居住', [.6591, .7876, .727, .801], confidence=.3),
                  block('38.4%', [.6796, .8037, .7372, .8155], confidence=1.)])
    record = {'id': 'E1', 'source_id': 's1', 'page': 1, 'chart_context': context_for_page(src, 1)}
    text = '其他用品及服务占比为2.9%。'
    binding = {'kind': 'printed_point', 'statement': text, 'evidence_id': 'E1',
               'category': '其他用品及服务', 'category_observation_id': 'P1B1',
               'value': '2.9%', 'value_observation_id': 'P1B2', 'unit': '%', 'unit_observation_id': 'P1B2',
               'association': 'adjacent_label'}
    return record, text, binding


def test_pie_label_below_category_passes_without_inventing_chart_regions():
    record, text, binding = pie_fixture()
    checked = check_chart_bindings(text, [binding], [record], required=True)
    assert checked['valid'], checked['errors']
    assert checked['bindings'][0]['requires_visual_review']
    assert 'region' not in checked['bindings'][0]


@pytest.mark.parametrize('change', ['wrong_value', 'pending_category', 'pending_title',
                                    'ambiguous_value', 'ambiguous_category', 'borrowed_unit'])
def test_pie_never_reassigns_other_label_or_promotes_uncertain_ocr(change):
    record, text, binding = pie_fixture(); rows = record['chart_context']['observations']
    if change == 'wrong_value':
        binding.update(value='38.4%', value_observation_id='P1B4', unit_observation_id='P1B4')
        text = text.replace('2.9%', '38.4%')
    elif change == 'pending_category':
        binding.update(category='口 居住', category_observation_id='P1B3', value='38.4%',
                       value_observation_id='P1B4', unit_observation_id='P1B4')
        text = '口 居住占比为38.4%。'
    elif change == 'pending_title': binding.update(title='图5', title_observation_id='P1B0')
    elif change == 'ambiguous_value':
        rows.append({'id': 'P1B5', 'text': '5.8%', 'bbox': [.386, .577, .4312, .59], 'status': 'observed'})
    elif change == 'ambiguous_category':
        rows.append({'id': 'P1B5', 'text': '另一类别', 'bbox': [.3491, .623, .4682, .637], 'status': 'observed'})
    else: binding['unit_observation_id'] = 'P1B4'
    binding['statement'] = text
    assert not check_chart_bindings(text, [binding], [record], required=True)['valid']
