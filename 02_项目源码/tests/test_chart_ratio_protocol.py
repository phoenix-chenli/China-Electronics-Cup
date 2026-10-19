"""Ratio notation is not unit conversion; failed proposals remain non-evidence."""
import copy
import hashlib
import json

import pytest

from backend import chart_reading as chart


def ratio(value='40%', unit='百分比', text='能源占比为40%。'):
    return {'id':'V1', 'type':'printed_value', 'text':text,
            'value':value, 'unit':unit, 'category':'能源', 'bbox':[0,0,1,1]}


@pytest.mark.parametrize('unit,value,text', [
    ('百分比','40%','能源占比为40%。'), ('百分率','40','能源占比为40%。'),
    ('千分率','3‰','能源比例为3‰。'), ('千分比','3','能源比例为3‰。'),
    ('％','40％','能源占比为40%。'), ('百分比','-1.5%','能源占比为-1.5%。'),
])
def test_explicit_matching_ratio_notation_keeps_original_fields(unit, value, text):
    original=ratio(value,unit,text)
    values,errors=chart._reading({'items':[original]})
    assert errors==[]
    assert values[0]['value']==value and values[0]['unit']==unit


@pytest.mark.parametrize('unit,value,text', [
    ('百分点','40%','能源占比为40%。'), ('百分比','40','能源值为40。'),
    ('百分比','40%','能源值为40‰。'), ('千分比','40%','能源值为40%（单位‰）。'),
    ('百分比','0.4','能源占比为40%。'), ('元','40','能源占比为40%。'),
    ('百分比','40%','能源占比40%，人工占比35%。'),
])
def test_ratio_equivalence_never_converts_or_admits_unbound_measurements(unit,value,text):
    assert chart._reading({'items':[ratio(value,unit,text)]})[1]


def test_failed_candidate_projection_is_redacted_and_bound_to_received_hash():
    raw=ratio(text='能源40%，另一个999。api_key=private-value /Users/private/chart.png')
    raw['reference_labels']=['invalid', {'text':'Bearer hidden-token','value':'999','secret':'never-copy'}]
    raw['api_key']='never-copy'
    rows=chart._rejected_items({'items':[raw]}, ['items[0]协议错误'])
    assert rows[0]['raw_sha256']==hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    assert rows[0]['candidate_is_evidence'] is False
    encoded=json.dumps(rows,ensure_ascii=False)
    assert '能源40%' in encoded
    assert all(token not in encoded for token in ['private-value','hidden-token','/Users/private','never-copy'])


def test_correction_receives_failed_proposal_and_still_requires_independent_review(monkeypatch):
    monkeypatch.setattr(chart,'_image_parts',lambda *args:([{'type':'image_url','image_url':{'url':'fixture'}}],'imagehash',(120,80)))
    bad=ratio(text='能源40%，人工35%。')
    good=ratio()
    responses=[{'items':[bad]}, {'items':[good]}, {'items':[{'id':'V1','supported':False}]}]
    calls=[]
    def gateway(messages,purpose):
        calls.append((copy.deepcopy(messages),purpose));return responses.pop(0)
    source={'id':'source','name':'合成图','sha256':'f'*64,'parser_version':'3.1','format':'pdf'}
    result=chart.read_chart_page(source,1,'能源占比是多少？',gateway)
    correction=json.loads(calls[1][0][1]['content'][0]['text'])['protocol_correction']
    assert correction['previous_rejected_candidates'][0]['candidate']['text']==bad['text']
    assert result['items']==[]
    assert result['audit']['attempts'][0]['rejected_items'][0]['candidate']['text']==bad['text']
    assert result['review']['rejected_ids']==['V1']
    assert [purpose for _,purpose in calls]==['document','document','verify']


def test_large_rejected_audit_retains_failure_identity_and_explicit_omissions():
    bad=ratio(text='长'*1200)
    attempts=[]
    for n in range(2):
        rows=[dict(bad,id=f'V{i}',category='类'*200,series='系'*200,period='期'*200) for i in range(12)]
        rejected=chart._rejected_items({'items':rows},[f'items[{i}]协议错误' for i in range(12)])
        attempts.append({'phase':'read','attempt':n+1,'status':'protocol_failed','errors':['格式无效'],
                         'request':{'purpose':'document'},'rejected_items':rejected})
    audit={'attempts':attempts,'truncated':False}
    chart._fit_audit(audit)
    assert chart._size(audit)<=chart.MAX_AUDIT_CHARACTERS and audit['truncated']
    assert len(audit['attempts'])==2
    for event in audit['attempts']:
        assert event['rejected_items'][0]['raw_sha256']
        assert any(row.get('candidate_details_omitted') for row in event['rejected_items'])


@pytest.mark.parametrize('unit,text', [('℃','当天温度-7°C。'), ('°C','当天温度-7℃。'),
                                       ('摄氏度','当天温度-7℃。'), ('℃','当天温度-7摄氏度。')])
def test_celsius_notation_does_not_change_printed_value(unit,text):
    row={'id':'T1','type':'printed_value','value':'-7','unit':unit,'text':text,'bbox':[0,0,1,1]}
    items,errors=chart._reading({'items':[row]})
    assert not errors and items[0]['value']=='-7' and items[0]['unit']==unit


@pytest.mark.parametrize('unit,text', [('℃','当天温度-7°F。'), ('摄氏度','当天温度-7。'), ('°F','当天温度-7℃。')])
def test_celsius_unit_neither_inferred_nor_converted(unit,text):
    row={'id':'T1','type':'printed_value','value':'-7','unit':unit,'text':text,'bbox':[0,0,1,1]}
    assert chart._reading({'items':[row]})[1]


def test_clock_is_a_source_period_label_and_temperature_unit_is_not_value():
    clock={'id':'T1','type':'visual_relation','text':'采样为每日08:00瞬时值。',
           'period':'每日08:00','bbox':[0,0,1,1]}
    assert chart._reading({'items':[clock]})[1]==[]
    invalid=dict(clock,type='printed_value',value='08:00')
    assert chart._reading({'items':[invalid]})[1]
    invalid={'id':'T2','type':'printed_value','text':'温度-7℃。','value':'-7℃','bbox':[0,0,1,1]}
    errors=chart._reading({'items':[invalid]})[1]
    assert errors and 'unit' in errors[0]


@pytest.mark.parametrize('secret_text', [
    'api_key="not-a-real-secret"', "password='private word tail'", '{"api_key":"private word tail"}',
    'secret="private \\"quoted\\" tail"', 'cookie="private word tail',
])
def test_quoted_credentials_are_redacted_in_chart_and_shared_receipt(secret_text):
    from backend.document_receipts import received_snapshot
    value='可公开的图表信息；'+secret_text+'；结束'
    chart_text=chart._clean(value,1000)
    receipt=received_snapshot({'text':value})
    assert all(token not in json.dumps([chart_text,receipt],ensure_ascii=False)
               for token in ('not-a-real-secret','private',' tail','quoted'))
    assert '可公开的图表信息' in chart_text and receipt['redacted']
