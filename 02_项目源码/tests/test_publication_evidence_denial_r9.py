"""Evidence-denial scope, plus exact saved R8 false rejection.

Passing this local guard never supplies a semantic review verdict.
"""
import copy
import json
from pathlib import Path
import pytest

from backend.publication_guard import publication_violation

SOURCES = {'draft': {'id': 'draft', 'name': '改造方案征求意见稿.pdf'}}
EVIDENCE = [{'id': 'E1', 'source_id': 'draft', 'quote': '改造方案征求意见稿\n本稿公开征求意见。'}]


@pytest.mark.parametrize('text', [
    '两份资料之间不存在可证明方案正式施行的关联依据。',
    '所查范围没有能够确认方案已正式施行的直接证据。',
    '所引资料不存在任何足以认定方案已经生效的可靠依据。',
    '没有证明方案尚未生效的证据。',
    '该通知与方案的正式施行无关联证据。',
    '更正记录与方案的正式发布无直接关联依据。',
    '不存在可证明方案已施行的依据；也不能证明它尚未施行。',
])
def test_bounded_denial_does_not_assert_embedded_state(text):
    assert publication_violation(text, EVIDENCE, SOURCES) is None


@pytest.mark.parametrize('text', [
    '方案已正式施行。',
    '方案尚未正式施行。',
    '并非不存在可证明方案已正式施行的依据。',
    '不是没有能够证明方案已经生效的证据。',
    '不存在不可证明方案已施行的依据。',
    '不存在不能证明方案已施行的依据。',
    '不存在可证明方案正式施行的关联依据，但方案实际上已正式施行。',
    '不存在可证明甲方案正式施行的依据，所以乙方案已经生效。',
    '有人说“没有可证明甲方案的证据”而乙方案已正式施行。',
    '与方案的已正式施行无关联证据。',
    '与方案的尚未正式施行无关联证据。',
    '并非与方案的正式施行无关联证据。',
    '该通知与方案的正式施行无关联证据，但目前已经生效。',
    '与方案的正式施行有直接关联证据。',
    '没有可证明方案正式施行的依据足以证明它已经生效。',
    '没有能够确认方案已正式施行的证据这一说法不成立。',
    '不存在可证明方案正式施行的依据并非事实。',
    '所给资料均为草案，因此方案尚未施行。',
])
def test_denial_cannot_authorize_affirmation_or_scope_leak(text):
    assert publication_violation(text, EVIDENCE, SOURCES)


def test_exact_r8_received_candidates_only_pass_local_guard():
    fixture = json.loads((Path(__file__).parent/'fixtures/cross024-r8-received.json').read_text())
    before = copy.deepcopy(fixture)
    evidence = {e['id']: e for e in fixture['evidence']}
    sources = {s['id']: s for s in fixture['sources']}
    for attempt in fixture['candidates']:
        for claim in attempt['claims']:
            assert publication_violation(claim['text'], [evidence[k] for k in claim['evidence_ids']], sources) is None
    assert fixture == before


def test_non_draft_actual_state_proof_still_required_and_accepted():
    source = {**SOURCES, 'formal': {'id': 'formal', 'name': '正式发布记录.txt'}}
    evidence = EVIDENCE + [{'id': 'E2', 'source_id': 'formal', 'quote': '乙方案已正式施行。'}]
    assert publication_violation('甲方案已正式施行。', evidence, source)
    assert publication_violation('乙方案已正式施行。', evidence, source) is None
