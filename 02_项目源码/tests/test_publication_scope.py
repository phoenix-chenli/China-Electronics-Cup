"""Explicit document-scope assertions; no remote model and no changed source facts."""
import copy
import json
from pathlib import Path

import pytest

from backend.publication_guard import publication_violation
from backend.cross_source_engine import CrossSourceError, _check_claim_scope


DRAFT = {'id': 'draft', 'name': '甲方案征求意见稿.docx'}
OTHER = {'id': 'other', 'name': '计量更正通知.pdf'}
SOURCES = {'draft': DRAFT, 'other': OTHER}
EVIDENCE = [
    {'id': 'E1', 'source_id': 'draft', 'quote': '本文件为征求意见稿，尚不能确认后续状态。'},
    {'id': 'E2', 'source_id': 'other', 'quote': '计量更正通知\n本通知签发于2025年7月3日。'},
]


@pytest.mark.parametrize('text', [
    '所给资料均为征求意见稿及起草说明性质，不能确认后续正式施行状态。',
    '所有文件都是草案。',
    '全部材料属于草稿。',
    '本次所给文档是起草说明。',
    '所提供资料标注为征求意见稿。',
])
def test_mixed_or_unknown_collection_is_not_all_drafts(text):
    error = publication_violation(text, EVIDENCE, SOURCES)
    assert error and '范围未证实' in error
    assert '未知性质不等于正式文件' in error


def test_selected_uncited_source_is_not_removed_from_universal_scope():
    assert publication_violation('所给资料均为草案。', EVIDENCE[:1], SOURCES)
    assert publication_violation('所引资料均为草案。', EVIDENCE[:1], SOURCES) is None
    assert publication_violation('本条引用的资料均为草案。', EVIDENCE[:1], SOURCES) is None
    assert publication_violation('所引资料均为草案。', EVIDENCE, SOURCES)


@pytest.mark.parametrize('text', [
    '甲方案草案和乙方案草案已列出。这些资料都是草案，不能证明方案已正式施行。',
    '甲方案草案和乙方案草案已列出。上述两份草案不能证明方案已正式施行。',
    '甲方案草案和乙方案草案已列出。上述资料均为草案。',
])
def test_deictic_subset_is_not_expanded_to_all_selected_sources(text):
    sources = {**SOURCES, 'draft2': {'name': '乙方案草案.pdf'},
               'other': {'name': '通知.pdf', 'metadata': {'publication_status': 'formal'}}}
    evidence = [EVIDENCE[0], {'source_id': 'draft2', 'quote': '本文件为草案。'}]
    assert publication_violation(text, evidence, sources) is None
    assert publication_violation('所给资料均为草案。', evidence, sources)


@pytest.mark.parametrize('text', [
    '这些征求意见资料不能证明方案已正式施行。',
    '所引这两份草案不能证明方案已正式施行。',
    '所给资料中的征求意见稿不能证明方案已正式施行。',
    '不能把所给资料都视为草案。',
    '并非所有资料都是草案。',
    '不能确认所给资料均为草案。',
    '有人说全部是草案，但不成立。',
    '有人说所有资料都是草案，但不成立。',
    '有人说“所有资料都是草案”，但这一说法不成立。',
])
def test_local_subset_denial_and_attributed_claim_are_not_universal_assertions(text):
    assert publication_violation(text, EVIDENCE, SOURCES) is None


@pytest.mark.parametrize('source,quote', [
    ({'name': '另一草案.pdf'}, '后续安排尚未明确。'),
    ({'name': 'material.txt', 'metadata': {'publication_status': 'draft'}}, '后续安排尚未明确。'),
    ({'name': 'material.txt'}, '本文件为征求意见稿，后续安排尚未明确。'),
    ({'name': 'material.txt'}, '本文件为草案，另引用正式通知作为背景。'),
    ({'name': 'material.txt'}, '甲项目方案（征求意见稿）\n后续安排尚未明确。'),
    ({'name': 'material.txt'}, '起草说明'),
])
def test_all_sources_with_positive_document_nature_support_can_pass(source, quote):
    sources = {'draft': DRAFT, 'other': source}
    evidence = [EVIDENCE[0], {'source_id': 'other', 'quote': quote}]
    assert publication_violation('所给资料均为草案或起草说明。', evidence, sources) is None


@pytest.mark.parametrize('source,quote', [
    ({'name': 'material.txt'}, '本文引用他人的草案。'),
    ({'name': 'material.txt'}, '此正式通知附件为意见稿。'),
    ({'name': 'material.txt'}, '本文件附件为征求意见稿。'),
    ({'name': 'material.txt'}, '附件：甲方案征求意见稿'),
    ({'name': 'material.txt'}, '本通知附件\n甲方案（征求意见稿）'),
    ({'name': 'material.txt'}, '附件\n本文件为征求意见稿。'),
    ({'name': 'material.txt'}, '引用甲方案征求意见稿'),
    ({'name': 'material.txt'}, '甲方案是草案'),
    ({'name': 'material.txt'}, '该通知没有说明文件性质。'),
    ({'name': '甲方案征求意见稿.pdf', 'metadata': {'publication_status': 'formal'}}, '征求意见稿'),
])
def test_other_document_mentions_and_unknown_are_not_whole_source_draft_support(source, quote):
    sources = {'draft': DRAFT, 'other': source}
    evidence = [EVIDENCE[0], {'source_id': 'other', 'quote': quote}]
    assert publication_violation('所给资料均为草案。', evidence, sources)


def test_metadata_does_not_automatically_become_a_formal_state_proof():
    sources = {'draft': DRAFT, 'other': {'name': 'material.txt'}}
    assert publication_violation('方案已经生效。', EVIDENCE, sources)


def test_unknown_single_source_cannot_claim_draft_without_any_positive_marker():
    assert publication_violation('所给文件是草案。', [{'source_id': 'u', 'quote': '记录内容。'}], {'u': {'name': '记录.txt'}})


def test_guard_keeps_inputs_immutable():
    sources, evidence = copy.deepcopy(SOURCES), copy.deepcopy(EVIDENCE)
    before = copy.deepcopy((sources, evidence))
    publication_violation('所给资料均为草案。', evidence, sources)
    assert (sources, evidence) == before


def test_later_affirmation_is_not_excused_by_an_earlier_denial_or_report():
    assert publication_violation('不能把所给资料都视为草案。所给资料均为草案。', EVIDENCE, SOURCES)
    assert publication_violation('有人说全部是草案，但不成立。所给资料均为草案。', EVIDENCE, SOURCES)


def test_r6_original_published_candidate_is_rejected_without_rewriting_it():
    data = json.loads((Path(__file__).parent / 'fixtures/r6-cross024-terminal.json').read_text())
    assert data['run_sha256'] == '8fac4ee29016e434146bb1d075720bb8c1899628b77c07c2d5c87bcdb345bb39'
    claim = data['claims'][0]
    evidence = {item['id']: item for item in data['evidence']}
    sources = {item['id']: item for item in data['sources']}
    originals = copy.deepcopy((claim, evidence, sources))
    with pytest.raises(CrossSourceError, match='范围未证实'):
        _check_claim_scope(claim['text'], claim['evidence_ids'], evidence, sources, False)
    assert (claim, evidence, sources) == originals
    # A source-scoped limit is a different candidate, not a silent edit of R6.
    scoped = '这些征求意见资料不能证明绿色改造方案已正式施行。'
    _check_claim_scope(scoped, claim['evidence_ids'], evidence, sources, False)
