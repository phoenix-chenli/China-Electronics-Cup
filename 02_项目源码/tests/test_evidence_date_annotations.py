"""Actual-citation calendar translation; synthetic source grids, zero API."""
import copy

import pytest

from backend.evidence_date_annotations import annotate_evidence_date_ranges
from backend.table_bindings import check_table_bindings
from backend.table_context import context_for_span, split_evidence_span
from test_deferred_table_context import fixture as source_fixture


PERIOD = '2005年10月至2006年9月'
ENGLISH = 'October 2005 to September\n2006'


def fixture(quote=None):
    source = source_fixture()
    page = source['pages'][0]['text']
    span = next(item for item in split_evidence_span(source, 2, 0, len(page), 'TN') if item['kind'] == 'table_rows')
    identity = {'source_id': 's', 'sha256': 'hash', 'parser_version': 'v1'}
    table = {**identity, 'id': 'E1', 'kind': 'text', 'page': 2,
             'start': span['start'], 'end': span['end'], 'quote': page[span['start']:span['end']],
             'table_context': context_for_span(source, 2, span['start'], span['end'])}
    quote = quote or 'The evaluation period was ' + ENGLISH + '. The design flow was 7 MGD.'
    body = {**identity, 'id': 'E2', 'kind': 'text', 'page': 1, 'start': 100,
            'end': 100 + len(quote), 'quote': quote, 'table_context': {'has_table': False, 'tables': []}}
    text = '研究期（' + PERIOD + '）内，Influent TN (mg/L)的Average为31.2 mg/L。'
    binding = {'evidence_id': 'E1', 'table_name': '第2页_表65', 'cell_id': 'R2C2',
               'value': '31.2', 'column_label': 'Average', 'row_label': 'Influent TN (mg/L)',
               'unit': 'mg/L', 'statement': text}
    return table, body, text, binding


def checked(table, body, text, binding, *, cite=True):
    binding = {**binding, 'statement': text}
    return check_table_bindings(text, [binding], [table, body] if cite else [table])


def test_exact_bilingual_range_has_occurrence_and_original_page_anchors_only():
    table, body, text, binding = fixture()
    before = copy.deepcopy((table, body, text, binding))
    result = checked(table, body, text, binding)
    assert result['valid'], result['errors']
    date = next(item for item in result['context_annotations'] if item['kind'] == 'evidence_date_range')
    assert text[date['start']:date['end']] == PERIOD
    assert date['source_quote'] == ENGLISH and date['evidence_id'] == 'E2'
    assert date['calendar_range'] == {'start': [2005, 10], 'end': [2006, 9]}
    start = body['quote'].index('October')
    assert date['source_anchor'] == {'page': 1, 'start': 100 + start, 'end': 100 + start + len(ENGLISH)}
    assert body['quote'][date['quote_relative_start']:date['quote_relative_end']] == ENGLISH
    assert date['semantic_review_required'] and date['scope'] == 'calendar_expression_equivalence_only'
    assert len(result['numeric_text']) == len(text) and '31.2 mg/L' in result['numeric_text']
    assert '2005' not in result['numeric_text'] and '10月' not in result['numeric_text']
    assert (table, body, text, binding) == before


@pytest.mark.parametrize('period', ['2005年11月至2006年9月', '2004年10月至2006年9月',
                                  '2006年9月至2005年10月', '2005年13月至2006年9月'])
def test_wrong_full_range_rejected_even_when_other_prose_contains_its_numbers(period):
    table, body, text, binding = fixture('The period was ' + ENGLISH + '. Other counters: 9 10 11 13 2004.')
    result = checked(table, body, text.replace(PERIOD, period), binding)
    assert not result['valid'] and result['date_scope_errors']
    assert not any(item['kind'] == 'evidence_date_range' for item in result['context_annotations'])


@pytest.mark.parametrize('addition', ['另一个指标为9 mg/L。', '另一个指标为10 mg/L。'])
def test_supported_date_cannot_authorize_other_business_numbers(addition):
    table, body, text, binding = fixture()
    result = check_table_bindings(text + addition, [binding], [table, body])
    assert not result['valid']
    assert addition in result['numeric_text']
    assert all(item['end'] <= len(text) for item in result['context_annotations'])


@pytest.mark.parametrize('change', ['not_cited', 'other_source', 'other_sha', 'other_parser',
                                  'missing_anchor', 'bad_length', 'stale', 'table_text', 'visual_transcription'])
def test_date_needs_cited_plain_span_of_the_bound_source(change):
    table, body, text, binding = fixture()
    if change == 'other_source': body['source_id'] = 'other'
    elif change == 'other_sha': body['sha256'] = 'different'
    elif change == 'other_parser': body['parser_version'] = 'v2'
    elif change == 'missing_anchor': body.pop('start')
    elif change == 'bad_length': body['end'] += 1
    elif change == 'stale': body['stale'] = True
    elif change == 'table_text': body['table_context']['has_table'] = True
    elif change == 'visual_transcription': body['kind'] = 'chart_observation'
    result = checked(table, body, text, binding, cite=change != 'not_cited')
    assert not result['valid']


@pytest.mark.parametrize('negation', ['研究期不是', '研究期并非', '研究期尚未确认是'])
def test_negated_candidate_date_is_not_licensed(negation):
    table, body, text, binding = fixture()
    result = checked(table, body, text.replace('研究期（', negation + '（'), binding)
    assert not result['valid'] and result['date_scope_errors']


@pytest.mark.parametrize('quote', ['The period was not ' + ENGLISH + '.',
                                 'The period excludes ' + ENGLISH + '.',
                                 '研究期不是2005年10月至2006年9月。'])
def test_negated_source_cannot_support_positive_date(quote):
    table, body, text, binding = fixture(quote)
    result = checked(table, body, text, binding)
    assert not result['valid'] and result['date_scope_errors']


def test_unrelated_permit_denial_does_not_negate_the_study_period():
    table, body, text, binding = fixture()
    text = text.replace('）内，', '）内，') + '这些数值不是许可限值。'
    result = checked(table, body, text, binding)
    assert result['valid'], result['errors']


@pytest.mark.parametrize('change', ['wrong_value', 'wrong_cell', 'wrong_row', 'conflicting_unit'])
def test_calendar_translation_never_bypasses_existing_cell_relationships(change):
    table, body, text, binding = fixture()
    if change == 'wrong_value': text = text.replace('31.2 mg/L', '31.3 mg/L')
    elif change == 'wrong_cell': binding['cell_id'] = 'invented'
    elif change == 'wrong_row': binding['row_label'] = 'Effluent TN (mg/L)'
    elif change == 'conflicting_unit':
        table['table_context']['tables'][0]['units'].append({'scope': 'column', 'column': 2, 'value': 'MGD'})
    assert not checked(table, body, text, binding)['valid']


def test_plain_body_date_gets_same_mask_but_wrong_month_explicitly_fails():
    _, body, _, _ = fixture()
    text = '研究期为' + PERIOD + '。'
    valid = check_table_bindings(text, None, [body])
    assert valid['valid'] and not valid['required'] and len(valid['context_annotations']) == 1
    invalid = check_table_bindings(text.replace('10月', '11月'), None, [body])
    assert not invalid['valid'] and invalid['date_scope_errors']


def test_chinese_source_and_english_output_share_the_same_complete_calendar_tuple():
    _, body, _, _ = fixture('研究期为2005年10月至2006年9月。')
    result = annotate_evidence_date_ranges('The period was October 2005 to September 2006.', [body])
    assert not result['unsupported'] and len(result['annotations']) == 1
    assert result['annotations'][0]['source_language'] == 'zh'


def test_each_occurrence_is_checked_and_partial_range_is_not_guessed():
    _, body, _, _ = fixture()
    text = PERIOD + '；另一个范围2005年11月至2006年9月。'
    result = annotate_evidence_date_ranges(text, [body])
    assert len(result['annotations']) == len(result['unsupported']) == 1
    assert result['annotations'][0]['end'] < result['unsupported'][0]['start']
    partial = annotate_evidence_date_ranges('2005年10月至9月', [body])
    assert partial['annotations'] == []


def test_source_month_case_and_whitespace_preserve_exact_anchor():
    _, body, _, _ = fixture('Period: OCTOBER  2005\nto  september 2006.')
    result = annotate_evidence_date_ranges(PERIOD, [body])
    assert not result['unsupported'] and len(result['annotations']) == 1
    assert result['annotations'][0]['source_quote'] == 'OCTOBER  2005\nto  september 2006'


def test_non_date_legacy_table_and_pure_sql_do_not_gain_new_metadata_requirements():
    from test_table_bindings import fixture as old_fixture
    record, _, text, binding = old_fixture()
    assert check_table_bindings(text, [binding], [record])['valid']
    query = {'id': 'E1', 'kind': 'query', 'query_result': {'rows': [[1]]}}
    result = check_table_bindings('2005年10月至2006年9月查询结果为1。', None, [query])
    assert not result['required'] and result['valid'] and not result['context_annotations']
    _, body, _, _ = fixture()
    mixed = check_table_bindings('2005年10月至2006年9月查询结果为1。', None, [query, body])
    assert mixed['valid'] and not mixed['context_annotations'] and not mixed.get('date_scope_errors')


def test_plain_prose_without_an_explicit_complete_interval_keeps_existing_route():
    _, body, _, _ = fixture('起始月份为2005年10月。结束月份为2006年9月。')
    result = check_table_bindings('研究期为' + PERIOD + '。', None, [body])
    assert result['valid'] and not result['context_annotations'] and not result.get('date_scope_errors')


def test_date_as_validated_table_header_keeps_original_relationship_path():
    table, _, text, binding = fixture()
    column = table['table_context']['tables'][0]['columns'][1]
    column['header_path'][0]['text'] = PERIOD
    text = 'Influent TN (mg/L)的' + PERIOD + '平均浓度为31.2 mg/L。'
    binding.update(column_label=PERIOD, statement=text)
    assert check_table_bindings(text, [binding], [table])['valid']


def test_another_bound_source_cannot_lend_its_date_to_this_occurrence():
    _, body, text, _ = fixture()
    identity_a = ('s', 'hash', 'v1')
    identity_b = ('other', 'other-hash', 'v1')
    result = annotate_evidence_date_ranges(text, [body], allowed_identities={identity_a, identity_b},
        source_scopes=[(0, len(text), identity_b)])
    assert not result['annotations'] and result['unsupported']
