"""Saved real R7 failure plus labelled synthetic polarity boundaries; zero API."""
import copy
import json
from pathlib import Path

import pytest

from backend.publication_guard import publication_violation

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/r7-cross024-publication-candidate.json').read_text())
SOURCES = {s['id']: s for s in FIXTURE['sources']}
EVIDENCE = FIXTURE['evidence']
DRAFT = [{'id': 'E1', 'source_id': 'd', 'quote': '本文件为征求意见稿，尚不能据此认定方案已经正式施行。'}]
DRAFT_SOURCES = {'d': {'id': 'd', 'name': '方案征求意见稿.docx'}}


def test_r7_received_candidate_passes_only_publication_guard_without_rewriting():
    before = copy.deepcopy(FIXTURE)
    assert FIXTURE['run_sha256'] == '4041f596a45e81358832bb91679e4bb9de104056f1d10f52307d95fa42af537f'
    assert publication_violation(FIXTURE['candidate']['text'], EVIDENCE, SOURCES) is None
    assert FIXTURE == before


@pytest.mark.parametrize('text', [
    '其签发不能作为该征求意见稿已转为正式施行文件的证据。',
    '该通知不能作为方案已经正式施行文件的充分证据。',
    '该通知不可作为方案已经正式施行文件的可靠依据。',
    '该通知不能证明该征求意见稿已转为正式施行文件。',
    '该通知无法证明方案已生效。',
    '尚不能据此认定方案已经正式施行。',
    '该通知不能证明“方案已正式施行”。',
    '该通知不能作为“方案已正式施行文件”的证据。',
])
def test_synthetic_local_denials_do_not_assert_the_embedded_state(text):
    assert publication_violation(text, DRAFT, DRAFT_SOURCES) is None


@pytest.mark.parametrize('text', [
    '其签发可以作为该征求意见稿已转为正式施行文件的证据。',
    '该通知证明该征求意见稿已转为正式施行文件。',
    '方案已正式施行。',
    '方案尚未正式施行。',
    '其签发不能作为参考而方案已正式施行。',
])
def test_synthetic_positive_or_actual_negative_state_still_needs_source_support(text):
    assert publication_violation(text, DRAFT, DRAFT_SOURCES)


@pytest.mark.parametrize('text', [
    '其签发不是不能作为该征求意见稿已转为正式施行文件的证据。',
    '其签发并非不能作为方案已正式施行文件的证据。',
    '不能说该通知不能作为方案已正式施行文件的证据。',
    '该通知不是不能证明方案已正式施行。',
    '该通知并非不能证明方案已正式施行。',
    '不能说该通知不能证明方案已正式施行。',
    '不是说该通知不能证明方案已正式施行。',
    '该通知不能不证明方案已正式施行。',
    '该通知无法不证明方案已正式施行。',
])
def test_synthetic_double_negation_is_not_a_denial_exemption(text):
    assert publication_violation(text, DRAFT, DRAFT_SOURCES)


@pytest.mark.parametrize('text', [
    '不能证明甲方案已施行，但乙方案已施行。',
    '不能证明甲方案已施行但乙方案已施行。',
    '不能证明甲方案已施行，事实上乙方案已施行。',
    '该通知不能作为甲方案已施行文件的证据且乙方案已施行。',
    '该通知不能作为甲方案已施行文件的证据但是乙方案已施行。',
    '有人说“不能证明”而方案已正式施行。',
    '有人说“不能证明方案已施行”但方案已正式施行。',
    '“不能证明”只是引述而方案已正式施行。',
    '"不能证明"只是引述而方案已正式施行。',
    "'不能证明'只是引述而方案已正式施行。",
    '该通知不能证明“甲方案已施行”但乙方案已施行。',
])
def test_synthetic_later_assertions_and_quoted_denials_do_not_escape(text):
    assert publication_violation(text, DRAFT, DRAFT_SOURCES)


def test_negative_quote_does_not_become_positive_formal_status_proof():
    sources = {**DRAFT_SOURCES, 'n': {'id': 'n', 'name': '计量正式通知.pdf', 'metadata': {'publication_status': 'formal'}}}
    evidence = [*DRAFT, {'id': 'E2', 'source_id': 'n', 'quote': '该通知不能作为方案已正式施行文件的证据。'}]
    before = copy.deepcopy((sources, evidence))
    assert publication_violation('方案已正式施行。', evidence, sources)
    assert (sources, evidence) == before


def test_actual_same_subject_formal_quote_remains_usable():
    sources = {**DRAFT_SOURCES, 'n': {'id': 'n', 'name': '施行通知.pdf', 'metadata': {'publication_status': 'formal'}}}
    evidence = [*DRAFT, {'id': 'E2', 'source_id': 'n', 'quote': '方案已正式施行。'}]
    assert publication_violation('方案已正式施行。', evidence, sources) is None


def test_mixed_draft_and_unknown_sources_still_cannot_all_be_called_drafts():
    assert publication_violation('所给资料均为草案，不能证明方案已正式施行。', EVIDENCE, SOURCES)


def test_unknown_notice_does_not_automatically_support_formal_status():
    assert publication_violation('方案已正式施行。', EVIDENCE, SOURCES)
