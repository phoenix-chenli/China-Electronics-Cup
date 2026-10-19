"""Independent visual facts never impersonate corrected OCR or replay failed POSTs."""
import base64
import copy
import io
import json
import hashlib

from PIL import Image
import pytest

from backend import chart_reading
from backend.chart_reading import read_chart_page
from backend.model_gateway import ModelError


@pytest.fixture
def page(monkeypatch):
    buffer = io.BytesIO()
    Image.new('RGB', (120, 80), 'white').save(buffer, format='JPEG')
    pixels = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode(), 'detail': 'high'}}
    calls = []
    def render(source, number, bbox=None):
        calls.append(number)
        return [{'type': 'text', 'text': '原页图像'}, copy.deepcopy(pixels)]
    monkeypatch.setattr(chart_reading, 'page_content', render)
    source = {'id': 'source-1', 'name': '公开统计.pdf', 'sha256': 'a' * 64, 'parser_version': '2.3',
              'format': 'pdf', 'original_path': '/private/source.pdf',
              'pages': [{'number': 1, 'text': '低置信错字', 'blocks': [{'text': '猜测999', 'confidence': .2}]}]}
    return source, pixels, calls


def fact(identifier='V1', value='73.3'):
    return {'id': identifier, 'type': 'printed_value', 'text': f'2021年一般公共预算收入为{value}亿元。',
            'bbox': [.1, .1, .9, .5], 'value': value, 'unit': '亿元', 'category': '2021年',
            'series': '一般公共预算收入'}


def verdict(*pairs):
    return {'items': [{'id': identifier, 'supported': supported} for identifier, supported in pairs]}


class Gateway:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls, self.attempts = [], []
        self.config = {'models': {'document': 'reader-requested', 'verify': 'review-requested'},
                       'api_key': 'CONFIG_SECRET_MUST_NOT_APPEAR', 'endpoint': 'https://private.invalid'}

    def __call__(self, messages, purpose):
        self.calls.append((copy.deepcopy(messages), purpose))
        self.attempts.append({'purpose': purpose, 'requested_model': self.config['models'][purpose],
                              'reported_model': purpose + '-reported', 'status': 'completed',
                              'elapsed_ms': 3, 'api_key': 'ATTEMPT_SECRET_MUST_NOT_APPEAR'})
        if not self.responses:
            raise AssertionError('Unexpected model call')
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(messages, purpose)
        return copy.deepcopy(response)


def test_exact_fact_is_re_read_on_same_pixels_with_independent_role_and_immutable_source(page):
    source, pixels, renders = page; before = copy.deepcopy(source)
    gateway = Gateway({'items': [fact()]}, verdict(('V1', True)))
    progress = []
    result = read_chart_page(source, 1, '2021年的预算收入是多少？', gateway, progress=progress.append)
    assert result['status'] == 'reviewed' and result['review']['valid']
    assert [purpose for _, purpose in gateway.calls] == ['document', 'verify'] and renders == [1]
    assert all(messages[1]['content'][-1] == pixels for messages, _ in gateway.calls)
    assert all([message['role'] for message in messages] == ['system', 'user'] for messages, _ in gateway.calls)
    review_payload = json.loads(gateway.calls[1][0][1]['content'][0]['text'])
    assert review_payload['candidates'] == [{**fact(), 'bbox': [0, 0, 1, 1], 'localization_scope': 'original_page', 'read_attempt': 1}]
    assert source == before and progress
    item = result['items'][0]
    assert item['provenance'] == 'separate_visual_read_and_review' and item['sha256'] == source['sha256']
    assert item['page'] == 1 and item['bbox'] == [0, 0, 1, 1] and item['review'] == {'supported': True}
    assert item['localization_scope'] == 'original_page'
    assert 'confidence' not in item and 'ocr' not in item
    assert len(result['audit']['original_page_image_sha256']) == 64
    requests = [row['request'] for row in result['audit']['attempts']]
    assert requests[0]['requested_model'] == 'reader-requested' and requests[0]['reported_model'] == 'document-reported'
    serialized = json.dumps(result)
    assert 'SECRET_MUST_NOT_APPEAR' not in serialized and 'private.invalid' not in serialized and 'base64' not in serialized
    assert '低置信错字' not in str(gateway.calls) and '猜测999' not in str(gateway.calls)


def test_only_supported_items_are_published_and_rejected_items_do_not_trigger_regeneration(page):
    source, _, _ = page
    gateway = Gateway({'items': [fact(), fact('V2', '80.2')]}, verdict(('V2', False), ('V1', True)))
    result = read_chart_page(source, 1, '核对收入。', gateway)
    assert [row['id'] for row in result['items']] == ['V1']
    assert result['review']['rejected_ids'] == ['V2'] and len(gateway.calls) == 2
    assert result['audit']['supporting_evidence'] is False
    assert result['audit']['attempts'][0]['candidates_are_evidence'] is False


def test_supported_missing_precision_is_a_reviewed_fact_not_automatic_business_refusal(page):
    source, _, _ = page
    item = {'id': 'V1', 'type': 'missing_precision', 'text': '图中未给出该柱的精确标签，无法由柱高确定精确值。', 'bbox': [.1, .2, .9, .8]}
    result = read_chart_page(source, 1, '能否精确读出柱值？', Gateway({'items': [item]}, verdict(('V1', True))))
    assert result['status'] == 'reviewed' and result['items'][0]['type'] == 'missing_precision'


def test_empty_or_semantically_rejected_candidates_are_honest_insufficiency_without_retries(page):
    source, _, _ = page
    empty = Gateway({'items': []})
    result = read_chart_page(source, 1, '查询图中的金额。', empty)
    assert result['status'] == 'insufficient_evidence' and not result['review']['valid'] and len(empty.calls) == 1
    rejected = Gateway({'items': [fact()]}, verdict(('V1', False)))
    result = read_chart_page(source, 1, '查询图中的金额。', rejected)
    assert result['status'] == 'insufficient_evidence' and result['review']['valid'] and not result['items']
    assert len(rejected.calls) == 2


def test_reader_protocol_correction_is_bounded_and_followed_by_separate_review(page):
    source, _, _ = page
    wrong = fact(); wrong['bbox'] = [0, 1, 1, 0]
    gateway = Gateway({'items': [wrong]}, {'items': [fact()]}, verdict(('V1', True)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'reviewed'
    assert [purpose for _, purpose in gateway.calls] == ['document', 'document', 'verify']
    correction = json.loads(gateway.calls[1][0][1]['content'][0]['text'])['protocol_correction']
    assert 'bbox' in correction['errors'][0]
    assert [row['status'] for row in result['audit']['attempts']] == ['protocol_failed', 'completed', 'completed']


@pytest.mark.parametrize('code', ['schema_failed', 'content_json_failed', 'output_truncated'])
def test_completed_response_protocol_error_can_be_corrected_once(page, code):
    source, _, _ = page
    gateway = Gateway(ModelError('不应保存此原始文字 SECRET', code), {'items': [fact()]}, verdict(('V1', True)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'reviewed' and len(gateway.calls) == 3
    assert 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('bad', [None, {}, {'items': []}, {'items': [{'id': 'V1', 'supported': 1}]},
    {'items': [{'id': 'V2', 'supported': True}]}, {'items': [{'id': [], 'supported': True}]},
    {'items': [{'id': 'V1', 'supported': 'true'}]},
    {'items': [{'id': 'V1', 'supported': True}, {'id': 'V1', 'supported': True}]}])
def test_verdict_requires_exact_unique_boolean_ids_and_never_publishes_unreviewed_reading(page, bad):
    source, _, _ = page
    gateway = Gateway({'items': [fact()]}, bad, bad)
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'protocol_failed' and result['items'] == [] and not result['review']['valid']
    assert [purpose for _, purpose in gateway.calls] == ['document', 'verify', 'verify']


def test_verifier_protocol_correction_keeps_the_same_candidate_and_pixels(page):
    source, _, _ = page
    gateway = Gateway({'items': [fact()]}, {'items': []}, verdict(('V1', True)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'reviewed'
    first = json.loads(gateway.calls[1][0][1]['content'][0]['text'])
    second = json.loads(gateway.calls[2][0][1]['content'][0]['text'])
    assert first['candidates'] == second['candidates'] and 'protocol_correction' in second
    assert gateway.calls[1][0][1]['content'][-1] == gateway.calls[2][0][1]['content'][-1]


@pytest.mark.parametrize('where', ['read', 'verify'])
@pytest.mark.parametrize('failure', [ModelError('timeout', 'timeout'), ModelError('transport', 'transport_failed'),
                                    ModelError('invalid response', 'response_json_failed'), TimeoutError('unknown POST'),
                                    InterruptedError('cancelled')])
def test_cancel_or_uncertain_transport_is_propagated_without_resending(page, where, failure):
    source, _, _ = page
    gateway = Gateway(*([{'items': [fact()]}, failure] if where == 'verify' else [failure]))
    with pytest.raises(type(failure)):
        read_chart_page(source, 1, '核对图。', gateway)
    assert len(gateway.calls) == (2 if where == 'verify' else 1)


def test_cancellation_before_pixels_and_after_read_prevents_any_further_action(page):
    source, _, renders = page
    gateway = Gateway()
    with pytest.raises(InterruptedError):
        read_chart_page(source, 1, '核对图。', gateway, cancelled=lambda: True)
    assert not renders and not gateway.calls
    state = {'cancelled': False}
    def response(*args):
        state['cancelled'] = True
        return {'items': [fact()]}
    gateway = Gateway(response)
    with pytest.raises(InterruptedError):
        read_chart_page(source, 1, '核对图。', gateway, cancelled=lambda: state['cancelled'])
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('change', ['sign', 'ratio', 'extra_number', 'bad_bbox', 'nan_bbox', 'estimated',
                                    'missing_value', 'extra_fact', 'duplicate_ids', 'too_many'])
def test_invalid_precise_claims_must_be_fixed_before_review(page, change):
    source, _, _ = page; item = fact(); rows = [item]
    if change == 'sign': item['text'] = item['text'].replace('73.3', '-73.3')
    elif change == 'ratio': item['text'] = item['text'].replace('73.3', '73.3%')
    elif change == 'extra_number': item['text'] += '另一项为88.3亿元。'
    elif change == 'bad_bbox': item['bbox'] = [0, False, 1, 1]
    elif change == 'nan_bbox': item['bbox'] = [0, 0, float('nan'), 1]
    elif change == 'estimated': item['estimated'] = True
    elif change == 'missing_value': item.pop('value')
    elif change == 'extra_fact': item['text'] = '金额未知。'
    elif change == 'duplicate_ids': rows = [item, copy.deepcopy(item)]
    else: rows = [fact('V' + str(index)) for index in range(13)]
    gateway = Gateway({'items': rows}, {'items': rows})
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'protocol_failed' and not result['items']
    assert [purpose for _, purpose in gateway.calls] == ['document', 'document']


def test_standard_reference_values_can_support_comparison_but_not_inferred_bar_values(page):
    source, _, _ = page
    item = {'id': 'V1', 'type': 'visual_relation', 'text': '各柱低于0.20 mg/L参考线。', 'bbox': [.1, .1, .9, .9],
            'reference_labels': [{'text': '0.20 mg/L参考线', 'value': '0.20', 'unit': 'mg/L', 'bbox': [.1, .3, .9, .4]}]}
    gateway = Gateway({'items': [item]}, verdict(('V1', True)))
    result = read_chart_page(source, 1, '是否高于参考线？', gateway)
    assert result['status'] == 'reviewed' and result['items'][0]['reference_labels'][0]['value'] == '0.20'
    assert 'value' not in result['items'][0]
    item['text'] += '最高柱为0.17 mg/L。'
    gateway = Gateway({'items': [item]}, {'items': [item]})
    assert read_chart_page(source, 1, '核对图。', gateway)['status'] == 'protocol_failed'


def test_explicit_standard_line_decimals_keep_trailing_precision(page):
    source, _, _ = page
    item = {'id': 'V1', 'type': 'printed_value', 'text': '参考线为0.10 mg/L。', 'value': '0.10',
            'unit': 'mg/L', 'category': '参考线', 'bbox': [.1, .2, .9, .3]}
    result = read_chart_page(source, 1, '参考线是多少？', Gateway({'items': [item]}, verdict(('V1', True))))
    assert result['items'][0]['value'] == '0.10'


def test_audit_is_bounded_whitelisted_and_redacted_before_storage(page):
    source, _, _ = page
    rows = [{'id': f'V{index}', 'type': 'missing_precision',
             'text': '不能精确读取。 ' + '描述' * 150 + ' /Users/alice/private.png Authorization: Bearer PRIVATE_TOKEN',
             'bbox': [.1, .2, .9, .8], 'api_key': 'RAW_FIELD_SECRET', 'image': 'data:image/png;base64,PRIVATE'}
            for index in range(12)]
    gateway = Gateway({'items': rows}, verdict(*[(row['id'], False) for row in rows]))
    result = read_chart_page(source, 1, '能否精确读取？', gateway)
    audit = json.dumps(result['audit'], ensure_ascii=False)
    assert len(audit) <= chart_reading.MAX_AUDIT_CHARACTERS
    assert 'PRIVATE_TOKEN' not in audit and '/Users/' not in audit and 'RAW_FIELD_SECRET' not in audit
    assert 'data:image' not in audit and not result['items']
    assert result['audit']['truncated']


def test_bounded_context_failure_occurs_before_post(page, monkeypatch):
    source, _, _ = page
    monkeypatch.setattr(chart_reading, 'MAX_CONTEXT_CHARACTERS', 100)
    gateway = Gateway()
    with pytest.raises(ValueError, match='预算'):
        read_chart_page(source, 1, '核对图。', gateway)
    assert not gateway.calls


def test_invalid_or_missing_original_pixels_never_use_model(page, monkeypatch):
    source, _, _ = page
    monkeypatch.setattr(chart_reading, 'page_content', lambda *args: [{'type': 'text', 'text': '没有图'}])
    gateway = Gateway()
    with pytest.raises(ValueError, match='原图'):
        read_chart_page(source, 1, '核对图。', gateway)
    assert not gateway.calls


def test_calendar_range_authorizes_both_year_labels_without_removing_real_minus_signs(page):
    source, _, _ = page
    item = {'id': 'V5', 'type': 'visual_relation',
            'text': '2022年一般公共预算收入柱形高于2021年柱形，2022年收入未下降。',
            'bbox': [.47, .28, .615, .432], 'category': '一般公共预算收入', 'period': '2021-2022年'}
    result = read_chart_page(source, 1, '2022年收入是否下降？',
                             Gateway({'items': [item]}, verdict(('V5', True))))
    assert result['status'] == 'reviewed'
    assert chart_reading._tokens('2021-2022年') == {'2021', '2022'}
    assert chart_reading._tokens('2021至2022年') == {'2021', '2022'}
    assert chart_reading._tokens('-2022万元') == {'-2022'}
    assert chart_reading._tokens('2021 -2022万元') == {'2021', '-2022'}
    assert chart_reading._tokens('−0.20 mg/L') == {'-0.20'}


def test_explicit_figure_number_and_reference_axis_unit_remain_subject_to_image_review(page):
    source, _, _ = page
    item = {'id': 'V1', 'type': 'visual_relation',
            'text': '图2-8上半幅TP图中，长虹桥2024年TP浓度柱顶低于国家III类标准红线。',
            'bbox': [.1, .2, .9, .6], 'category': '长虹桥', 'series': 'TP浓度', 'period': '2024年',
            'reference_labels': [{'text': '国家III类标准 0.20', 'value': '0.20', 'unit': 'mg/L', 'bbox': [.1, .2, .9, .3]}]}
    gateway = Gateway({'items': [item]}, verdict(('V1', True)))
    result = read_chart_page(source, 1, '是否高于参考线？', gateway)
    assert result['status'] == 'reviewed'
    assert result['items'][0]['context_annotations'][0]['text'] == '图2-8'
    assert 'context_annotations' in json.loads(gateway.calls[1][0][1]['content'][0]['text'])['candidates'][0]
    # Even a structurally valid figure/unit association is not published if the
    # independent original-page reviewer cannot confirm that association.
    result = read_chart_page(source, 1, '是否高于参考线？', Gateway({'items': [item]}, verdict(('V1', False))))
    assert not result['items']


@pytest.mark.parametrize('suffix', ['小时', '万元', 'mg/L', '个百分点'])
def test_figure_prefix_cannot_disguise_an_unbound_measurement(page, suffix):
    source, _, _ = page
    item = {'id': 'V1', 'type': 'visual_relation', 'text': '图2' + suffix + '内达到最高。', 'bbox': [.1, .1, .9, .9]}
    gateway = Gateway({'items': [item]}, {'items': [item]})
    assert read_chart_page(source, 1, '核对图。', gateway)['status'] == 'protocol_failed'
    assert all(purpose == 'document' for _, purpose in gateway.calls)


def test_last_read_correction_salvages_only_valid_unique_items_then_reviews_every_one(page):
    source, _, _ = page
    valid = [fact('V1'), fact('V2', '80.2')]
    bad = {'id': 'V3', 'type': 'visual_relation', 'text': '柱值准确为999。', 'bbox': [.1, .1, .9, .9]}
    gateway = Gateway({'items': [*valid, bad]}, {'items': [*valid, bad]}, verdict(('V1', True), ('V2', False)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'reviewed' and [item['id'] for item in result['items']] == ['V1']
    assert result['reading']['partial'] and result['audit']['reading_partial']
    assert result['reading']['accepted_ids'] == ['V1', 'V2']
    assert result['reading']['rejected_items'][0]['id'] == 'V3'
    assert result['audit']['attempts'][1]['status'] == 'partial'
    payload = json.loads(gateway.calls[-1][0][1]['content'][0]['text'])
    assert [item['id'] for item in payload['candidates']] == ['V1', 'V2']
    assert result['review']['rejected_ids'] == ['V2']


def test_duplicate_ids_never_salvage_one_ambiguous_copy_even_if_other_fields_are_bad(page):
    source, _, _ = page
    bad = fact(); bad['type'] = 'invalid'
    rows = [fact(), bad, fact('V2')]
    gateway = Gateway({'items': rows}, {'items': rows})
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'protocol_failed' and result['items'] == [] and len(gateway.calls) == 2


def test_partial_reading_cannot_salvage_an_oversized_valid_subset(page, monkeypatch):
    source, _, _ = page
    monkeypatch.setattr(chart_reading, 'MAX_READING_CHARACTERS', 100)
    gateway = Gateway({'items': [fact()]}, {'items': [fact()]})
    assert read_chart_page(source, 1, '核对图。', gateway)['status'] == 'protocol_failed'
    assert len(gateway.calls) == 2


def test_last_protocol_exception_uses_only_explicit_retained_candidates_not_stale_raw(page):
    source, _, _ = page
    bad = fact('V2'); bad['bbox'] = [0, 1, 1, 0]
    gateway = Gateway({'items': [fact(), bad]}, ModelError('bad JSON', 'content_json_failed'), verdict(('V1', True)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['status'] == 'reviewed' and [r['id'] for r in result['items']] == ['V1'] and len(gateway.calls) == 3
    assert result['items'][0]['read_attempt'] == 1
    assert result['audit']['attempts'][1]['candidates'] == []
    assert result['audit']['attempts'][1]['status'] == 'partial'


@pytest.mark.parametrize('rejected', [None, 'V4'])
def test_read_correction_preserves_valid_values_when_new_titles_fail_but_relations_are_fixed(page, rejected):
    source, _, _ = page
    # Reduced from the two archived live-detail-final dual_axis_v1 responses:
    # first values pass while relations omit their periods; correction fixes
    # relations but adds unbound title years to the four numeric statements.
    values = []
    for identifier, year, value, unit, category in (
            ('V1', '2021', '73.3', '亿元', '一般公共预算收入'),
            ('V2', '2022', '73.8', '亿元', '一般公共预算收入'),
            ('V3', '2021', '11.6', '%', '增速'), ('V4', '2022', '0.6', '%', '增速')):
        values.append({'id': identifier, 'type': 'printed_value', 'text': f'{year}年{category}为{value}{unit}',
                       'bbox': [.2, .42, .64, .48], 'value': value, 'unit': unit,
                       'category': category, 'period': year + '年'})
    relations = [{'id': 'V5', 'type': 'visual_relation', 'text': '2022年收入柱高于2021年收入柱。',
                  'bbox': [.3, .22, .67, .48], 'category': '收入'},
                 {'id': 'V6', 'type': 'visual_relation', 'text': '2022年增速点低于2021年增速点。',
                  'bbox': [.3, .22, .83, .48], 'category': '增速'}]
    titled = [{**v, 'text': '图2《2020-2024年一般公共预算收入情况》中，' + v['text'],
               'bbox': [.2, .2, .83, .48]} for v in values]
    fixed = [{**r, 'period': '2021-2022年'} for r in relations]
    gateway = Gateway({'items': values + relations}, {'items': titled + fixed},
                      verdict(*[(f'V{i}', f'V{i}' != rejected) for i in range(1, 7)]))
    result = read_chart_page(source, 1, '列出两年的收入、增速，并比较变化。', gateway)
    reviewed = json.loads(gateway.calls[2][0][1]['content'][0]['text'])['candidates']
    assert [r['id'] for r in reviewed] == [f'V{i}' for i in range(1, 7)]
    assert [r['read_attempt'] for r in reviewed] == [1, 1, 1, 1, 2, 2]
    assert [r['text'] for r in reviewed[:4]] == [v['text'] for v in values]
    assert all('2020' not in r['text'] for r in reviewed)
    assert len(result['items']) == (5 if rejected else 6) and len(gateway.calls) == 3
    assert result['reading']['partial'] is True
    assert result['reading']['candidate_sources'] == {f'V{i}': 1 if i <= 4 else 2 for i in range(1, 7)}
    assert result['review']['rejected_ids'] == ([rejected] if rejected else [])


def test_valid_same_id_replacement_records_both_unreviewed_candidates_and_real_attempts(page):
    source, _, _ = page
    invalid = {'id': 'V2', 'type': 'bad', 'text': '无效候选', 'bbox': [.1, .1, .8, .8]}
    replacement = fact(value='80.2'); replacement['bbox'] = [.3, .2, .8, .5]
    gateway = Gateway({'items': [fact(), invalid]}, {'items': [replacement]}, verdict(('V1', False)))
    result = read_chart_page(source, 1, '核对收入。', gateway)
    attempt = result['audit']['attempts'][1]
    record = attempt['replacements'][0]
    assert record['id'] == 'V1' and (record['old_attempt'], record['new_attempt']) == (1, 2)
    assert record['old']['value'] == '73.3' and record['new']['value'] == '80.2'
    assert record['old']['bbox_verified'] is record['new']['bbox_verified'] is False
    assert record['old_sha256'] != record['new_sha256']
    reviewed = json.loads(gateway.calls[2][0][1]['content'][0]['text'])['candidates']
    assert reviewed[0]['read_attempt'] == 2 and reviewed[0]['value'] == '80.2'
    assert result['items'] == [] and result['review']['rejected_ids'] == ['V1']


def test_ambiguous_duplicate_batch_never_enters_accumulated_candidates(page):
    source, _, _ = page
    gateway = Gateway({'items': [fact(), fact(value='80.2'), fact('V2')]},
                      {'items': [fact('V3')]}, verdict(('V3', True)))
    result = read_chart_page(source, 1, '核对收入。', gateway)
    assert [r['id'] for r in result['items']] == ['V3']
    assert result['audit']['attempts'][0]['candidate_batch_eligible'] is False
    assert result['reading']['candidate_sources'] == {'V3': 2}


@pytest.mark.parametrize('bound', ['items', 'characters'])
def test_merged_valid_readings_cannot_exceed_page_budget_or_silently_drop_old_values(page, monkeypatch, bound):
    source, _, _ = page
    invalid = {'id': 'BAD', 'type': 'bad'}
    if bound == 'items':
        first = [fact(f'V{i}') for i in range(1, 7)]
        second = [fact(f'V{i}') for i in range(7, 14)]
    else:
        first, second = [fact()], [fact('V2')]
        single = chart_reading._size([{**fact(), 'read_attempt': 1}])
        monkeypatch.setattr(chart_reading, 'MAX_READING_CHARACTERS', single + 30)
    gateway = Gateway({'items': [*first, invalid]}, {'items': second})
    result = read_chart_page(source, 1, '核对收入。', gateway)
    assert result['status'] == 'protocol_failed' and result['items'] == []
    assert len(gateway.calls) == 2 and all(p == 'document' for _, p in gateway.calls)
    assert '预算' in str(result['audit']['attempts'][-1]['errors'])


def test_adjacent_page_and_figure_labels_keep_both_exact_source_identifiers():
    text = '第9页图5中，2024年居民人均消费支出里的居住占比为38.4%。'
    annotations = chart_reading._location_annotations(text)
    assert [(row['kind'], row['text']) for row in annotations] == [('page_label', '第9页'), ('figure_label', '图5')]
    assert all(text[row['start']:row['end']] == row['text'] for row in annotations)
    assert chart_reading._tokens(chart_reading._numeric_text(text, annotations)) == {'2024', '38.4%'}
    # A forged page that happens to equal a valid period must still be exposed
    # as a page annotation for the caller's source-location check.
    forged = chart_reading._location_annotations(text.replace('第9页', '第2024页'))
    assert forged[0]['kind'] == 'page_label' and forged[0]['text'] == '第2024页'


@pytest.mark.parametrize('text,expected', [
    ('第9页图5中，增幅为9%。', ['第9页', '图5']),
    ('第9页图像显示增幅为9%。', []),
    ('第9页图5%为增幅。', []),
    ('第9页图5小时内完成。', []),
])
def test_adjacent_location_rule_never_hides_business_numbers_or_accepts_arbitrary_tail(text, expected):
    annotations = chart_reading._location_annotations(text)
    assert [row['text'] for row in annotations] == expected
    numeric = chart_reading._numeric_text(text, annotations)
    if '9%' in text:
        assert '9%' in chart_reading._tokens(numeric)
    if not expected:
        assert numeric == text


@pytest.fixture
def high_resolution_page(tmp_path, monkeypatch):
    from PIL import ImageDraw
    from backend.visual_context import page_content as original_page_content
    path = tmp_path / 'chart.png'
    image = Image.new('RGB', (3200, 2400), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((960, 800, 1152, 1600), fill='blue')
    draw.line((320, 720, 2880, 720), fill='red', width=8)
    image.save(path)
    source = {'id': 'source-1', 'name': 'chart.png', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'parser_version': '2.3', 'format': 'png', 'original_path': str(path),
              'pages': [{'number': 1, 'text': '', 'blocks': []}]}
    renders = []
    def render(src, page_number, bbox=None):
        renders.append(copy.deepcopy(bbox))
        return original_page_content(src, page_number, bbox=bbox)
    monkeypatch.setattr(chart_reading, 'page_content', render)
    return source, renders


def threshold_relation(identifier='V2'):
    return {'id': identifier, 'type': 'visual_relation', 'text': '该柱低于0.20 mg/L参考线。',
            'bbox': [.30, .30, .36, .45],
            'reference_labels': [{'text': '0.20参考线', 'value': '0.20', 'unit': 'mg/L', 'bbox': [.10, .30, .90, .32]}]}


def test_one_new_detail_view_reviews_only_rejected_items_and_retains_both_verdicts(high_resolution_page):
    source, renders = high_resolution_page; before = copy.deepcopy(source)
    item = threshold_relation()
    gateway = Gateway({'items': [fact(), item]}, verdict(('V1', True), ('V2', False)), verdict(('V2', True)))
    result = read_chart_page(source, 1, '该柱是否低于参考线？', gateway)
    assert result['status'] == 'reviewed' and [row['id'] for row in result['items']] == ['V1', 'V2']
    detail = result['reading']['detail_review']
    assert detail['status'] == 'reviewed' and detail['initial_verdicts'] == [{'id': 'V2', 'supported': False}]
    assert detail['verdicts'] == [{'id': 'V2', 'supported': True}]
    assert detail['bbox'] == [.075, .275, .925, .475] and renders == [None, detail['bbox']]
    assert detail['pixel_density_ratio'] >= 1.15
    assert detail['detail_image_sha256'] != detail['original_page_image_sha256']
    assert result['review']['initial_verdicts'] == [{'id': 'V1', 'supported': True}, {'id': 'V2', 'supported': False}]
    assert result['review']['detail_recovered_ids'] == ['V2']
    assert result['items'][1]['review'] == {'supported': True, 'initial_supported': False, 'detail_supported': True}
    assert source == before and [purpose for _, purpose in gateway.calls] == ['document', 'verify', 'verify']
    content = gateway.calls[-1][0][1]['content']
    payload = json.loads(content[0]['text'])
    expected = {**item, 'bbox': [0, 0, 1, 1], 'localization_scope': 'original_page', 'read_attempt': 1,
                'reference_labels': [{**item['reference_labels'][0], 'bbox': [0, 0, 1, 1],
                                      'localization_scope': 'original_page'}]}
    assert payload['candidates'] == [expected] and payload['initial_verdicts'][0]['supported'] is False
    assert payload['detail_view']['target_localization_verified'] is False
    assert detail['target_localization_verified'] is False and detail['purpose'] == 'visual_review_crop'
    assert result['items'][1]['bbox'] == [0, 0, 1, 1]
    assert result['items'][1]['reference_labels'][0]['bbox'] == [0, 0, 1, 1]
    urls = [part['image_url']['url'] for part in content if part.get('type') == 'image_url']
    assert len(urls) == 2
    assert urls[0] == gateway.calls[0][0][1]['content'][-1]['image_url']['url']
    assert hashlib.sha256(base64.b64decode(urls[1].split(',', 1)[1])).hexdigest() == detail['detail_image_sha256']
    assert 'base64' not in json.dumps(result)


@pytest.mark.parametrize('last', [verdict(('V2', False)), {'items': []}, verdict(('V1', True)),
                                 ModelError('bad JSON', 'content_json_failed')])
def test_detail_denial_or_malformed_response_retains_original_rejection_without_another_call(high_resolution_page, last):
    source, _ = high_resolution_page
    gateway = Gateway({'items': [threshold_relation()]}, verdict(('V2', False)), last)
    result = read_chart_page(source, 1, '核对参考线。', gateway)
    assert result['status'] == 'insufficient_evidence' and not result['items']
    assert result['review']['rejected_ids'] == ['V2'] and len(gateway.calls) == 3
    assert result['reading']['detail_review']['initial_verdicts'][0]['supported'] is False


def test_detail_does_not_retry_same_pixels_or_a_nearly_whole_page(page):
    source, _, _ = page
    gateway = Gateway({'items': [threshold_relation()]}, verdict(('V2', False)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['reading']['detail_review']['reason'] == 'no_new_higher_density_pixels'
    assert len(gateway.calls) == 2
    item = threshold_relation(); item['bbox'] = [0, 0, 1, 1]
    gateway = Gateway({'items': [item]}, verdict(('V2', False)))
    result = read_chart_page(source, 1, '核对图。', gateway)
    assert result['reading']['detail_review']['reason'] == 'no_sufficiently_smaller_region'
    assert len(gateway.calls) == 2


@pytest.mark.parametrize('failure', [ModelError('timeout', 'timeout'), ModelError('transport', 'transport_failed'),
                                    InterruptedError('cancelled')])
def test_detail_transport_or_cancel_propagates_without_replaying_unknown_post(high_resolution_page, failure):
    source, _ = high_resolution_page
    gateway = Gateway({'items': [threshold_relation()]}, verdict(('V2', False)), failure)
    with pytest.raises(type(failure)):
        read_chart_page(source, 1, '核对参考线。', gateway)
    assert len(gateway.calls) == 3


def test_cancel_while_detail_pixels_are_prepared_never_makes_detail_post(page, monkeypatch):
    source, pixels, _ = page
    def render(src, number, bbox=None):
        if bbox is not None:
            raise InterruptedError('cancelled during crop')
        return [pixels]
    monkeypatch.setattr(chart_reading, 'page_content', render)
    gateway = Gateway({'items': [threshold_relation()]}, verdict(('V2', False)))
    with pytest.raises(InterruptedError):
        read_chart_page(source, 1, '核对参考线。', gateway)
    assert len(gateway.calls) == 2


def test_protocol_invalid_initial_review_never_triggers_detail(high_resolution_page):
    source, renders = high_resolution_page
    gateway = Gateway({'items': [threshold_relation()]}, {'items': []}, {'items': []})
    result = read_chart_page(source, 1, '核对参考线。', gateway)
    assert result['status'] == 'protocol_failed' and renders == [None]
    assert all(row['phase'] != 'detail_verify' for row in result['audit']['attempts'])


def test_figure_label_before_quoted_source_title_is_not_a_business_value(page):
    source, _, _ = page
    item = {'id': 'V1', 'type': 'printed_value',
            'text': '图5“2024年居民人均消费支出构成”中，居住占比为38.4%。',
            'bbox': [.35, .56, .78, .83], 'value': '38.4%', 'category': '居住', 'period': '2024年'}
    result = read_chart_page(source, 1, '居住占比是多少？', Gateway({'items': [item]}, verdict(('V1', True))))
    assert result['status'] == 'reviewed'
    assert result['items'][0]['context_annotations'][0]['text'] == '图5'
    assert '38.4%' in chart_reading._tokens(chart_reading._numeric_text(item['text'], result['items'][0]['context_annotations']))


def test_quoted_location_punctuation_never_hides_a_direct_business_unit():
    assert chart_reading._location_annotations('“图5”中显示趋势。')[0]['text'] == '图5'
    assert chart_reading._location_annotations('图5亿元的规模。') == []
    assert chart_reading._tokens(chart_reading._numeric_text('图5亿元的规模。', [])) == {'5'}


@pytest.mark.parametrize('predicate', ['未标注精确数值', '未提供精确数值', '没有给出精确数值',
                                      '未为长虹桥断面的总磷柱印刷精确数值'])
def test_figure_number_before_explicit_negative_printing_predicate_is_only_a_location(predicate):
    text = '图2-8' + predicate + '，不能支持三位小数。'
    annotations = chart_reading._location_annotations(text)
    assert [row['text'] for row in annotations] == ['图2-8']
    assert chart_reading._tokens(chart_reading._numeric_text(text, annotations)) == set()


@pytest.mark.parametrize('text', ['图2亿元未提供资金。', '图2小时未给出结果。', '图2%未标注。',
                                 '图2-8未为该柱印刷0.123 mg/L精确数值。'])
def test_negative_figure_predicate_does_not_exempt_business_values(text):
    annotations = chart_reading._location_annotations(text)
    if '0.123' in text:
        assert chart_reading._tokens(chart_reading._numeric_text(text, annotations)) == {'0.123'}
    else:
        assert annotations == []
        assert chart_reading._tokens(text) == ({'2%'} if '%' in text else {'2'})


@pytest.mark.parametrize('supported', [True, False])
def test_model_region_suggestion_never_becomes_a_reviewed_target_location(page, supported):
    source, _, _ = page
    item = threshold_relation()
    # Syntactically valid suggestions may point to an unrelated part of a page.
    item['bbox'] = [.65, .65, .85, .80]
    item['reference_labels'][0]['bbox'] = [.65, .61, .85, .63]
    before = copy.deepcopy(item)
    gateway = Gateway({'items': [item]}, verdict(('V2', supported)))
    result = read_chart_page(source, 1, '该柱是否低于参考线？', gateway)
    reviewed = json.loads(gateway.calls[1][0][1]['content'][0]['text'])['candidates'][0]
    assert reviewed['text'] == item['text']
    for positioned in (reviewed, reviewed['reference_labels'][0]):
        assert positioned['bbox'] == [0, 0, 1, 1]
        assert positioned['localization_scope'] == 'original_page'
    raw = result['audit']['attempts'][0]['candidates'][0]
    assert raw['bbox'] == item['bbox'] and raw['bbox_verified'] is False
    assert raw['localization_scope'] == 'unverified_crop_hint'
    assert raw['reference_labels'][0]['bbox'] == item['reference_labels'][0]['bbox']
    assert raw['reference_labels'][0]['bbox_verified'] is False
    assert result['audit']['attempts'][0]['candidates_are_evidence'] is False
    assert item == before and len(gateway.calls) == 2
    if supported:
        for positioned in (result['items'][0], result['items'][0]['reference_labels'][0]):
            assert positioned['bbox'] == [0, 0, 1, 1]
            assert positioned['localization_scope'] == 'original_page'
        assert result['review']['supported_ids'] == ['V2']
    else:
        assert not result['items'] and result['review']['rejected_ids'] == ['V2']


def test_unverified_crop_hint_still_selects_real_pixels_but_cannot_override_fact_rejection(high_resolution_page):
    source, renders = high_resolution_page
    item = threshold_relation()
    # This hint misses both the synthetic bar and its reference line. It may
    # select pixels for review, but neither the hint nor the crop proves truth.
    item['bbox'] = [.65, .65, .85, .80]
    item['reference_labels'][0]['bbox'] = [.65, .61, .85, .63]
    gateway = Gateway({'items': [item]}, verdict(('V2', False)), verdict(('V2', False)))
    result = read_chart_page(source, 1, '核对该柱和参考线。', gateway)
    detail = result['reading']['detail_review']
    assert renders == [None, [.625, .585, .875, .825]]
    assert detail['bbox'] == renders[-1] and detail['pixel_density_ratio'] >= 1.15
    assert detail['detail_image_sha256'] != detail['original_page_image_sha256']
    assert detail['target_localization_verified'] is False
    payload = json.loads(gateway.calls[-1][0][1]['content'][0]['text'])
    assert payload['detail_view']['bbox'] == renders[-1]
    for positioned in (payload['candidates'][0], payload['candidates'][0]['reference_labels'][0]):
        assert positioned['bbox'] == [0, 0, 1, 1]
        assert positioned['localization_scope'] == 'original_page'
    assert len([p for p in gateway.calls[-1][0][1]['content'] if p.get('type') == 'image_url']) == 2
    assert detail['initial_verdicts'] == detail['verdicts'] == [{'id': 'V2', 'supported': False}]
    assert result['status'] == 'insufficient_evidence' and result['items'] == []
    assert result['review']['initial_verdicts'] == [{'id': 'V2', 'supported': False}]
    assert result['review']['detail_recovered_ids'] == [] and len(gateway.calls) == 3
