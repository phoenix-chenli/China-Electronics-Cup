"""Frozen cross-plan parent reuse preserves original evidence without paid replay."""
import copy
import json

import pytest

from evaluation.acceptance import parent_plan_verified, verified_parent_attempt
from evaluation.core import digest


def fixture(tmp_path):
    entry = {'plan_sha256': 'original', 'case_id': 'chain-t2',
             'record': {'id': 'real-answer'},
             'payload': {'question': '选择西店。', 'mode': 'database'},
             'source_sha256': {'db': 'source-hash'}}
    path = tmp_path / 'attempt.json'; path.write_text(json.dumps(entry))
    review = tmp_path / 'review.json'; review.write_text('{"passed":true}')
    plan = {'plan_sha256': 'extension',
            'sources': [{'id': 'db', 'sha256': 'source-hash'}],
            'cases': [{'id': 'chain-t2', 'question': '选择西店。',
                       'mode': 'database', 'source_ids': ['db']}],
            'baseline_artifacts': [{'kind': 'verified_parent_answer', 'case_id': 'chain-t2',
                'path': str(path), 'sha256': digest(path), 'review_path': str(review),
                'review_sha256': digest(review), 'independently_verified': True,
                'original_plan_sha256': 'original', 'answer_id': 'real-answer'}]}
    return plan, entry, path, review


def test_attested_parent_keeps_original_plan_and_payload(tmp_path):
    plan, entry, path, _ = fixture(tmp_path); before = copy.deepcopy(entry)
    assert parent_plan_verified(plan, entry, path, 'chain-t2')
    assert entry == before and entry['plan_sha256'] == 'original'


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'review', 'record', 'question',
                                   'source', 'mode', 'answer_id', 'attestation'])
def test_changed_or_unreviewed_parent_is_rejected(tmp_path, change):
    plan, entry, path, review = fixture(tmp_path)
    if change == 'missing': plan['baseline_artifacts'] = []
    elif change == 'duplicate': plan['baseline_artifacts'] *= 2
    elif change == 'review': review.write_text('{"passed":false}')
    elif change == 'record': path.write_text('{}')
    elif change == 'question': plan['cases'][0]['question'] = '选择东店。'
    elif change == 'source': plan['sources'][0]['sha256'] = 'changed'
    elif change == 'mode': plan['cases'][0]['mode'] = 'document'
    elif change == 'answer_id': entry['record']['id'] = 'unrelated'
    elif change == 'attestation': plan['baseline_artifacts'][0]['independently_verified'] = False
    assert not parent_plan_verified(plan, entry, path, 'chain-t2')


def test_same_plan_still_requires_actual_parent_identity(tmp_path):
    plan, entry, path, _ = fixture(tmp_path)
    entry['plan_sha256'] = plan['plan_sha256']
    assert parent_plan_verified(plan, entry, path, 'chain-t2')
    entry['source_sha256']['db'] = 'wrong-source'
    assert not parent_plan_verified(plan, entry, path, 'chain-t2')


def test_parent_selection_requires_review_and_selects_attested_attempt():
    attempts = [{'attempt_id': 'reviewed'}, {'attempt_id': 'later-unreviewed'}]
    result = {'attempts': attempts, 'assessment': {'passed': True}, 'verified_attempt_id': 'reviewed'}
    assert verified_parent_attempt(result) is attempts[0]
    assert verified_parent_attempt({**result, 'assessment': None}) is None
    assert verified_parent_attempt({**result, 'verified_attempt_id': None}) is None
    assert verified_parent_attempt({**result, 'verified_attempt_id': 'absent'}) is None
