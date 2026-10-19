"""Real API dispatch with fake downstream engines; no model accuracy claim."""
import uuid
import pytest
from backend.answer_service import _document_needs_tools
from test_answers_api import app_env, project, upload, submit, wait_terminal


@pytest.mark.parametrize('question', ['请按原页公式和已给定参数计算R，结果用kW表示。', '第1页图1两年收入相差多少？', 'Compute the rate from the printed parameters.'])
def test_explicit_native_calculation_uses_tool_planner(app_env, monkeypatch, question):
    from backend import cross_source_engine, document_engine
    client, gateway, _ = app_env
    pid = project(client)
    detail = upload(client, pid, 'formula.txt', b'R = (a + b) / c; a = 12 kWh; b = 8 kWh; c = 4 h.')
    sid = detail['source']['id']; calls = []
    def cross(sources, received, history, *args, retrieval_factory=None, audit=None, actual_user_question=None):
        calls.append((sources[0]['id'], received)); assert callable(retrieval_factory) and callable(audit)
        return 'insufficient_evidence', {'text': 'Explicit fake route only', 'claims': [], 'evidence': [], 'verification': {'status': 'limited'}}
    monkeypatch.setattr(cross_source_engine, 'answer_cross_source', cross)
    monkeypatch.setattr(document_engine, 'answer_document', lambda *a, **k: pytest.fail('Quotation engine cannot compute'))
    result = wait_terminal(client, pid, submit(client, pid, {'question': question, 'source_id': sid, 'mode': 'document', 'request_id': str(uuid.uuid4())})['id'])
    assert result['status'] == 'insufficient_evidence'
    assert calls == [(sid, question)]
    assert result['result']['claims'] == []


@pytest.mark.parametrize('metadata', [{'formula_structures': [{'verified': False, 'computation_status': 'unavailable'}]}, {'ocr_performed': True}])
def test_candidate_structure_only_enables_tools_not_publication(metadata):
    assert _document_needs_tools({'metadata': metadata}, '给出R的结果。')


@pytest.mark.parametrize('question', ['2024年同比增长率是多少？', '不要计算，只引用原文金额。', '该公司有哪些业务？'])
def test_plain_quotation_does_not_require_tools(question):
    assert not _document_needs_tools({'metadata': {}}, question)
