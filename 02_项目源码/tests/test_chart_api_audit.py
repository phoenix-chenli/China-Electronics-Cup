"""Actual service callback wiring; explicit fake pixels and model, no API calls."""
import json
import threading
import uuid

import pytest

from backend.model_gateway import ModelError
from test_answers_api import app_env, project, upload, submit
from test_late_terminal_audit import wait_worker


@pytest.mark.parametrize('outcome', ['later_service_error', 'cancelled_before_reply'])
def test_chart_received_response_survives_actual_service_terminal(app_env, monkeypatch, outcome):
    from backend import chart_answer_engine, chart_reading
    client, _, runtime = app_env
    pid = project(client)
    detail = upload(client, pid, 'chart-fixture.txt', '图1能源占比。'.encode())
    # Route through the real chart pipeline, explicitly without asserting pixel accuracy.
    monkeypatch.setattr(chart_answer_engine, 'can_answer', lambda *args: True)
    monkeypatch.setattr(chart_reading, '_image_parts', lambda *args:
        ([{'type':'image_url','image_url':{'url':'fixture'}}], 'synthetic-image-hash', (100,100)))
    calls, entered, release = [], threading.Event(), threading.Event()
    class Gateway:
        models = {}; usage = []; attempts = []
        def __call__(self, messages, purpose):
            calls.append(purpose)
            if len(calls) > 1:
                raise ModelError('已收到服务错误', code='service_unavailable')
            entered.set()
            assert release.wait(5)
            return {'items':[{'id':'V1','type':'printed_value','text':'能源占比40%。',
                              'value':'40%','unit':'%','category':'能源','bbox':[0,0,1,1]}],
                    'secret':'fixture-secret-value', 'endpoint':'https://fixture.invalid/private-api'}
    service = client.app.state.answers
    monkeypatch.setattr(service, 'gateway_factory', lambda *args: Gateway())
    record = submit(client, pid, {'source_id':detail['source']['id'], 'mode':'document',
        'question':'第1页图1能源占比是多少？', 'request_id':str(uuid.uuid4())})
    try:
        assert entered.wait(5)
        if outcome == 'cancelled_before_reply':
            assert client.post(f"/api/projects/{pid}/answers/{record['id']}/cancel").status_code == 200
            before = service.store.get(pid,record['id'])
    finally:
        release.set()
    wait_worker(service,record['id'])
    after = service.store.get(pid,record['id'])
    if outcome == 'cancelled_before_reply':
        assert after['status']=='cancelled' and after['result']==before['result']
        assert after['updated_at']==before['updated_at'] and len(calls)==1
        diagnostics=after['diagnostics']['late_received_audits'][0]['data']
    else:
        assert after['status']=='failed' and calls==['document','verify']
        assert after['result']['claims']==[] and after['result']['evidence']==[]
        diagnostics=after['result']['diagnostics']
    assert len(diagnostics['chart_responses'])==1
    receipt=diagnostics['chart_responses'][0]
    assert receipt['received'] and receipt['response']['value']['items'][0]['value']=='40%'
    assert receipt['publication_status']=='not_adopted'
    public=client.get(f"/api/projects/{pid}/answers/{record['id']}").json()
    assert 'fixture-secret-value' not in json.dumps(public)
    assert 'fixture.invalid' not in json.dumps(public)
