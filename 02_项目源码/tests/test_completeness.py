"""Coverage is an explicit, source-bound model judgment, never inferred truth."""
from backend import completeness as c
import pytest


def test_explicit_question_parts_keep_bracketed_conditions():
    parts = c.question_parts('请核对（范围：甲；乙）的预算？乙的工期是多少？')
    assert len(parts) == 2
    assert '甲；乙' in parts[0]['text']


@pytest.mark.parametrize('question,fragments', [
    ('仅按2024年已核定单价计算甲站费用并说明参数来源及单位。',
     ['仅按2024年已核定单价计算甲站费用', '并说明参数来源及单位。']),
    ('请给出甲断面三位小数实测浓度，并说明乙断面能否精确读取。',
     ['请给出甲断面三位小数实测浓度，', '并说明乙断面能否精确读取。']),
    ('列出实测值，同时请核对单位，并且说明适用条件。',
     ['列出实测值，', '同时请核对单位，', '并且说明适用条件。']),
    ('请给出结果，说明原公式来源。不要假设缺少的单价。',
     ['请给出结果，', '说明原公式来源。', '不要假设缺少的单价。']),
])
def test_conjoined_requests_have_separate_coverage_obligations(question, fragments):
    parts = c.question_parts(question)
    assert [p['text'] for p in parts] == fragments
    assert [p['id'] for p in parts] == [f'Q{i+1}' for i in range(len(fragments))]
    # The inventory keeps the exact wording, including qualifiers and negation.
    assert ''.join(p['text'] for p in parts) == question


@pytest.mark.parametrize('question', [
    '请比较Silt、Sand和Gravel的参数与适用条件。',
    '请按1,200.50元计算总额。',
    '请核对“计算并说明单位”这一原文标题。',
    '请解释"计算并说明单位"这一原文标题。',
    '请计算（表内要求：先计算，再说明单位）对应的费用。',
    '请列出名称含“说明”或“并说明”的记录。',
])
def test_request_splitting_does_not_change_entities_quotes_or_number_literals(question):
    assert c.question_parts(question) == [{'id': 'Q1', 'text': question}]


def test_one_sentence_missing_second_request_cannot_be_marked_complete():
    parts = c.question_parts('计算甲站费用并说明参数来源。')
    incomplete = c.assess(parts, [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']}], ['C1'], ['E1'])
    assert incomplete['status'] == 'invalid' and incomplete['complete'] is False
    missing = c.assess(parts, [
        {'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['C1']},
        {'question_id': 'Q2', 'status': 'missing_evidence', 'claim_ids': []}], ['C1'], ['E1'])
    assert c.repair_action(missing) == 'retrieve'
    assert c.focused_query('计算甲站费用并说明参数来源。', missing, 1000).endswith('重点补查：并说明参数来源。')


@pytest.mark.parametrize('raw', [[], [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': ['invented']}],
    [{'question_id': 'Q1', 'status': 'evidence_available', 'evidence_ids': ['invented']}],
    [{'question_id': 'Q1', 'status': 'answered', 'claim_ids': []}]])
def test_coverage_cannot_bind_missing_or_unsupported_facts(raw):
    coverage = c.assess(c.question_parts('预算多少？'), raw, ['C1'], ['E1'])
    assert coverage['status'] == 'invalid' and coverage['complete'] is False
    assert c.repair_action(coverage) is None


def test_missing_coverage_is_unknown_and_complete_is_not_inferred():
    coverage = c.assess(c.question_parts('预算多少？'), None, ['C1'], ['E1'])
    assert coverage['complete'] is None and coverage['status'] == 'unknown'
    assert c.permits_completion(coverage) is False


@pytest.mark.parametrize('coverage', [None, {}, {'status':'unknown','complete':True},
    {'status':'invalid','complete':True}, {'status':'conflict','complete':True},
    {'status':'assessed','complete':None}, {'status':'assessed','complete':1}])
def test_only_explicit_assessed_true_can_publish_complete(coverage):
    assert c.permits_completion(coverage) is False


def test_valid_complete_coverage_can_publish():
    coverage=c.assess(c.question_parts('预算多少？'),[{'question_id':'Q1','status':'answered','claim_ids':['C1']}],['C1'],[])
    assert c.permits_completion(coverage) is True


def test_full_question_is_not_truncated_to_force_retrieval():
    coverage = c.assess(c.question_parts('甲项目预算多少？'), [{'question_id': 'Q1', 'status': 'missing_evidence'}], [], [])
    assert c.focused_query('甲项目预算多少？', coverage, 5) is None


@pytest.mark.parametrize('raw', [[], 'malformed', [None],
    [{'question_id': 'forged', 'status': 'answered', 'claim_ids': ['C1']}]])
def test_conflict_signal_wins_over_missing_or_malformed_coverage(raw):
    value = c.assess(c.question_parts('预算多少？'), raw, ['C1'], ['E1'], conflict=True)
    assert value['status'] == 'conflict' and value['complete'] is False
    assert c.repair_action(value) is None
