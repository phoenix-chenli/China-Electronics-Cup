"""Saved protocol receipts plus negative cases; no model or network calls."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from backend import chart_reading as reader, chart_answer_engine as engine
from test_chart_reading import page, Gateway, verdict

FIXTURE = Path(__file__).parent / 'fixtures/chart-locator-receipts.json'
RECEIPTS = json.loads(FIXTURE.read_text())['cases']


def test_original_chart004_both_receipts_pass_without_rewriting_any_original_field():
    for raw in RECEIPTS['chart-004']:
        before = copy.deepcopy(raw)
        items, errors = reader._reading(raw)
        assert not errors and len(items) == 2
        for original, item in zip(raw['items'], items):
            assert all(item[k] == v for k, v in original.items())
            assert [a['text'] for a in item['context_annotations']] == ['图2']
            assert reader._tokens(reader._numeric_text(item['text'], item['context_annotations'])) == {'2025', item['value']}
        assert raw == before


def test_original_chart005_valid_first_draft_preserved_but_missing_period_repair_still_rejected():
    items, errors = reader._reading(RECEIPTS['chart-005'][0])
    assert not errors and [i['id'] for i in items] == ['V1', 'V2', 'V3', 'V4']
    items, errors = reader._reading(RECEIPTS['chart-005'][1])
    assert [i['id'] for i in items] == ['V4']
    assert len(errors) == 3 and all('完整值、单位和语义标签' in e for e in errors)


def test_original_chart010_preserves_date_negative_temperature_and_clock_contract():
    items, errors = reader._reading(RECEIPTS['chart-010'][0])
    assert [i['id'] for i in items] == ['V2', 'V3', 'V4']
    assert [i['value'] for i in items] == ['-7', '-2', '5']
    assert [i['period'] for i in items] == ['8月1日', '8月2日', '8月3日']
    assert all(i['unit'] == '℃' for i in items)
    # The first V1 does not bind the printed clock in period: not repaired by a locator.
    assert len(errors) == 1 and errors[0].startswith('items[0]')
    corrected, errors = reader._reading(RECEIPTS['chart-010'][1])
    assert not errors and corrected[0]['period'] == '每日08:00'
    assert '08:00' in reader._numeric_text(corrected[0]['text'], corrected[0]['context_annotations'])


@pytest.mark.parametrize('label,kind', [('图2','figure_label'), ('表2','table_label'), ('第2页','page_label')])
def test_explicit_locators_only_mask_identifier_not_title_or_body_numbers(label, kind):
    suffix = '中，' if label.startswith('第') else '青湾港2025年运营支出构成中，'
    text = label + suffix + '能源占35%，另有2万元费用。'
    annotations = reader._location_annotations(text)
    assert annotations[0]['kind'] == kind
    remaining = reader._numeric_text(text, annotations)
    assert '35%' in remaining and '2万元' in remaining
    if not label.startswith('第'):
        assert '2025年' in remaining


@pytest.mark.parametrize('extra', ['另有2万元费用。', '其他占12%。', '精确浓度为0.123 mg/L。',
                                   '同比增幅为-2%。', '变化为3个百分点。', '另有2024年数据。'])
def test_locator_does_not_authorize_unbound_measurements(extra):
    row = copy.deepcopy(RECEIPTS['chart-004'][0]['items'][0]); row['text'] += extra
    items, errors = reader._reading({'items':[row]})
    assert not items and any('未逐项转录的精确数值' in e for e in errors)


@pytest.mark.parametrize('suffix', ['亿元','小时','mg/L','kWh','℃','千瓦时','毫克','公里','公斤','英镑','欧元','美元','港币','号机组','组设备'])
def test_business_unit_and_equipment_suffix_not_exempted_as_adjacent_title(suffix):
    text = '图2' + suffix + '统计中，能源占比为35%。'
    assert reader._location_annotations(text) == []
    row = {'id':'V1','type':'printed_value','text':text,'bbox':[0,0,1,1],'value':'35%','category':'能源'}
    assert not reader._reading({'items':[row]})[0]


def test_semantic_entity_series_labels_keep_their_digits():
    text = '图2港口运营情况中，2025年能源占比为35%。'
    labels = ['图2港口', '2025年']
    assert reader._location_annotations(text, labels) == []
    assert reader._tokens(reader._numeric_text(text, reader._location_annotations(text, labels))) == {'2','2025','35%'}
    row={'id':'V1','type':'printed_value','text':text,'bbox':[0,0,1,1], 'value':'35%', 'category':'图2港口','period':'2025年'}
    items, errors = reader._reading({'items':[row]})
    assert not errors and 'context_annotations' not in items[0]


def test_adjacent_original_page_and_title_have_separate_annotations():
    text = '第2页图2青湾港2025年运营支出构成中，能源占比为35%。'
    annotations = reader._location_annotations(text)
    assert [a['text'] for a in annotations] == ['第2页','图2']
    assert reader._tokens(reader._numeric_text(text, annotations)) == {'2025','35%'}


@pytest.mark.parametrize('supported', [True, False])
def test_real_saved_candidate_requires_separate_image_review(page, supported):
    source, _, _ = page
    raw = RECEIPTS['chart-004'][0]
    gateway = Gateway(raw, verdict(('V1',supported),('V2',supported)))
    result = reader.read_chart_page(source, 1, '能源和人工占比？', gateway)
    assert [purpose for _, purpose in gateway.calls] == ['document','verify']
    assert len(result['items']) == (2 if supported else 0)
    review_payload=json.loads(gateway.calls[1][0][1]['content'][0]['text'])
    assert all(c['context_annotations'][0]['text']=='图2' for c in review_payload['candidates'])
    assert '表号' in gateway.calls[1][0][0]['content']


@pytest.mark.parametrize('label', ['图2','表2'])
def test_final_claim_locator_must_be_anchored_to_bound_source_or_reviewed_item(label):
    row = copy.deepcopy(RECEIPTS['chart-004'][0]['items'][0]); row['text']=row['text'].replace('图2',label)
    items, errors=reader._reading({'items':[row]}); assert not errors
    item={**items[0],'page':2,'review':{'supported':True}}
    source={'id':'S1','pages':[{'number':2,'text':''}]}
    raw={'claims':[{'id':'C1','text':row['text'],'item_ids':['V1']}]}
    claims, errors=engine._claims(raw,{'V1':item},source)
    assert claims and not errors
    raw['claims'][0]['text']=row['text'].replace(label,label[0]+'2025')
    assert not engine._claims(raw,{'V1':item},source)[0]
    # Even an actual figure/table identifier cannot supply a business value.
    raw['claims'][0]['text']=row['text']+'另有2万元费用。'
    assert not engine._claims(raw,{'V1':item},source)[0]


def test_table_locator_requires_matching_kind_not_same_number_figure():
    items,_=reader._reading(RECEIPTS['chart-004'][0])
    item={**items[0],'page':2}
    source={'id':'S1','pages':[{'number':2,'text':'图2 青湾港运营支出'}]}
    raw={'claims':[{'id':'C1','text':item['text'].replace('图2','表2'),'item_ids':['V1']}]}
    assert not engine._claims(raw,{'V1':item},source)[0]
