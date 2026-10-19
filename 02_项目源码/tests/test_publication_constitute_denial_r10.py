"""Explicit two-ended evidence denial; no endpoint/model request."""
import copy
import json
from pathlib import Path
import pytest
from backend.publication_guard import publication_violation

EVIDENCE = [{'id':'E1','source_id':'d','quote':'本文件为征求意见稿，方案拟实施。'}]
SOURCES = {'d':{'id':'d','name':'方案征求意见稿.docx'}}

@pytest.mark.parametrize('text', [
    '计量更正通知的签发不构成对征求意见稿已正式施行的证明。',
    '其他通知不构成方案已经生效的依据。',
    '验收记录不构成对项目已经建成的直接证据。',
    '签署意见不构成方案尚未生效的可靠证明。',
    '文件的存在不构成对方案正式发布的充分依据。',
    '这份记录不构成对甲项目验收完毕的有效证据。',
])
def test_both_ends_deny_evidence_without_asserting_current_state(text):
    assert publication_violation(text,EVIDENCE,SOURCES) is None

@pytest.mark.parametrize('text', [
    '签发构成对方案已正式施行的证明。',
    '签发不构成对方案已正式施行。',
    '方案已正式施行不构成签发证明。',
    '签发不是不构成对方案已正式施行的证明。',
    '签发并非不构成对方案已正式施行的证明。',
    '不能说签发不构成对方案已正式施行的证明。',
    '不能认为签发不构成对方案已正式施行的证明。',
    '没有不构成对方案已正式施行的证明。',
    '否认签发不构成对方案已正式施行的证明。',
    '签发不构成对并非方案已正式施行的证明。',
    '签发不构成对方案已生效且乙方案已建成的证明。',
    '签发不构成对方案已经生效的证明但方案已建成。',
    '签发不构成对甲方案已生效的证明，乙方案已建成。',
    '签发不构成对方案已经生效的证明且方案已建成。',
    '签发不构成对方案已经生效的证明不成立。',
    '签发不构成对方案已经生效的证明没有争议且方案已建成。',
    '签发“不构成”对方案已经生效的证明。',
    '“签发不构成对方案已经生效的证明”，所以方案已经生效。',
    '"签发不构成"对方案已经生效的证明。',
    '“签发不构成对方案已经生效的证明。',
    '签发不构成对方案“已经生效”的证明。',
    '签发不构成对方案然而已正式施行的证明。',
    '签发不构成对方案实际上已正式施行的证明。',
    '签发不构成对'+('很长的对象名称'*6)+'已正式施行的证明。',
])
def test_no_double_negation_contrast_quote_or_missing_end_escape(text):
    assert publication_violation(text,EVIDENCE,SOURCES)


def test_denial_in_source_never_proves_a_positive_current_state():
    evidence=[{'id':'E1','source_id':'d','quote':'签发不构成对方案已经生效的证明。'}]
    assert publication_violation('方案已经生效。',evidence,SOURCES)


def test_three_actual_r9_candidates_pass_only_local_guard_without_rewriting():
    f=json.loads((Path(__file__).parent/'fixtures/cross024-r9-denial-candidates.json').read_text())
    before=copy.deepcopy(f);sources={s['id']:s for s in f['sources']}
    assert f['run_sha256']=='ef68385935837ff417bd1a0b16826a8338a47f4546c4fa7342808216b2682bc0'
    assert len(f['candidates'])==3
    for row in f['candidates']:
        c=row['candidate'];evidence=[e for e in f['evidence'] if e['id'] in c['evidence_ids']]
        assert publication_violation(c['text'],evidence,sources) is None
    assert f==before
