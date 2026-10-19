"""Punctuation is not a subquestion; short real subquestions still need evidence."""
import json
from pathlib import Path

import pytest

from backend import completeness as c


@pytest.mark.parametrize('separators', ['？；', '?;', '？；\n', '；；', '?？；;\n'])
def test_adjacent_separators_do_not_make_empty_question(separators):
    assert c.question_parts('甲' + separators + '乙？') == [
        {'id': 'Q1', 'text': '甲' + separators[0]}, {'id': 'Q2', 'text': '乙？'}]


@pytest.mark.parametrize('text', ['', '   ', '？；', '\n\t；？！…', '“”；【】？', '\u200b；'])
def test_no_content_cannot_complete_by_empty_list(text):
    parts = c.question_parts(text)
    assert parts == []
    coverage = c.assess(parts, [], [], [])
    assert coverage['status'] == 'invalid' and coverage['complete'] is False
    assert coverage['validation_errors'][0]['code'] == 'empty_question_parts'
    assert not c.permits_completion(coverage)


def test_empty_parts_preserve_positive_conflict():
    coverage = c.assess([], [], [], [], conflict=True)
    assert coverage['status'] == 'conflict' and coverage['complete'] is False


def test_short_chinese_numeric_and_mathematical_items_are_not_filtered():
    parts = c.question_parts('甲？；0？；-2？；2024？；β？；√？')
    assert [p['text'] for p in parts] == ['甲？', '0？', '-2？', '2024？', 'β？', '√？']
    raw = [{'question_id': p['id'], 'status': 'answered', 'claim_ids': ['C1']} for p in parts]
    assert c.permits_completion(c.assess(parts, raw, ['C1'], []))
    raw[-1]['claim_ids'] = []
    assert not c.permits_completion(c.assess(parts, raw, ['C1'], []))


def test_nested_quoted_conditions_keep_original_characters_and_boundaries():
    text = '核对“甲？；乙”的范围（附表【0；-2】）；何时？；'
    assert c.question_parts(text) == [
        {'id': 'Q1', 'text': '核对“甲？；乙”的范围（附表【0；-2】）；'},
        {'id': 'Q2', 'text': '何时？'},
    ]


def test_real_frozen_recovery_questions_keep_all_content_without_punctuation_parts():
    suite = Path(__file__).resolve().parent / 'fixtures/recovery-question-punctuation.json'
    data = json.loads(suite.read_text())
    counts = {'recovery_harbor': 3, 'recovery_river': 3, 'recovery_equipment': 4, 'recovery_policy': 5}
    for case in data['cases']:
        parts = c.question_parts(case['question'])
        assert len(parts) == counts[case['id']]
        assert all(p['text'] != '；' for p in parts)
        assert [p['id'] for p in parts] == [f'Q{i+1}' for i in range(len(parts))]
        raw = [{'question_id': p['id'], 'status': 'answered', 'claim_ids': ['C1']} for p in parts]
        assert c.permits_completion(c.assess(parts, raw, ['C1'], []))
        # No model answer is inferred: one unresolved actual clause blocks completion.
        raw[-1] = {'question_id': parts[-1]['id'], 'status': 'missing_evidence'}
        assert not c.permits_completion(c.assess(parts, raw, ['C1'], []))
