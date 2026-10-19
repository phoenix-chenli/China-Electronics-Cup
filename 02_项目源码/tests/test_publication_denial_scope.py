import pytest

from backend.publication_guard import publication_violation


EVIDENCE = [{'id': 'E1', 'source_id': 'draft', 'quote': '征求意见稿：方案拟实施。'}]
SOURCES = {'draft': {'id': 'draft', 'name': '方案征求意见稿.docx'}}


@pytest.mark.parametrize('text', [
    '该通知的签发不能作为征求意见稿已正式施行的证据。',
    '这份文件不可作为方案已经生效的充分证据。',
    '原稿不足以作为项目已经建成的直接依据。',
    '该通知无法作为方案尚未生效的可靠证据。',
])
def test_local_denial_of_evidence_is_not_embedded_state_assertion(text):
    assert publication_violation(text, EVIDENCE, SOURCES) is None


@pytest.mark.parametrize('text', [
    '该通知可以作为方案已正式施行的证据。',
    '该通知不是不能作为方案已正式施行的证据。',
    '该通知并非不能作为方案已正式施行的证据。',
    '不能说该通知不能作为参考，方案已正式施行。',
    '该通知不能作为参考；方案已正式施行。',
    '该通知不能作为甲方案已生效的证据，乙方案已生效。',
    '该通知不能作为甲方案已生效的证据但是乙方案已生效。',
    '该通知不能作为甲方案已生效的证据且乙方案已生效。',
    '该通知不能作为参考而方案已经生效。',
])
def test_denial_does_not_authorize_positive_or_other_state_claim(text):
    assert publication_violation(text, EVIDENCE, SOURCES)


def test_denial_in_source_is_not_positive_state_evidence():
    quote = [{'id': 'E1', 'source_id': 'draft', 'quote': '征求意见稿。'},
             {'id': 'E2', 'source_id': 'notice', 'quote': '通知不能作为方案已生效的证据。'}]
    sources = {**SOURCES, 'notice': {'id': 'notice', 'name': '正式通知.docx'}}
    assert publication_violation('方案已生效。', quote, sources)
