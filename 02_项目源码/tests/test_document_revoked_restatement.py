"""Verbatim old inputs plus proven withdrawal, not an allowance for their value."""
import copy

import pytest

from backend import document_user_context as user
from test_document_user_context import ASSUME,REVOKE,ACTUAL,SCOPE,turn,fake_model,run

SUMMARY='请整理最终结论，并说明此前假设是否仍生效。'
TEXT='此前“月末实盘改为7件”的情景假设已撤销、不再生效，当前以手册原始月末记录为准。'


def history():
    return [turn(),dict(answer_id='p2',original_question=REVOKE,status='completed',source_revisions=SCOPE)]


def ctx():
    return user.build_context(SUMMARY,history(),source_revisions=SCOPE)


@pytest.mark.parametrize('text',[
    TEXT,TEXT.replace('，当前','；当前'),TEXT.replace('此前','之前用户'),
    TEXT.replace('“','').replace('”',''),TEXT.replace('“','「').replace('”','」'),
])
def test_exact_old_input_followed_by_proven_revocation_has_local_origin(text):
    masked,ann=user.annotate(text,ctx())
    assert len(ann)==1 and len(masked)==len(text)
    a=ann[0];assert text[a['start']:a['end']]=='7' and masked[a['start']:a['end']]==' '
    assert a['source_answer_id']=='p1' and a['revocation_source_answer_id']=='p2'
    assert a['source_text'][a['source_start']:a['source_end']]==a['quote']=='月末实盘改为7件'
    assert a['revocation_source_text'][a['revocation_start']:a['revocation_end']]==a['revocation_quote']
    assert a['semantic_review_required'] and not a['is_document_evidence']


@pytest.mark.parametrize('text',[
    TEXT.replace('7件','9件'),TEXT.replace('7件','7吨'),TEXT.replace('月末实盘','电子台账'),
    TEXT.replace('已撤销、不再生效','尚未撤销、仍然生效'),
    TEXT.replace('此前','手册记载此前'),TEXT.replace('此前','不要声称此前'),
    TEXT.replace('当前以手册原始月末记录为准','但仍沿用该假设'),
    TEXT.replace('当前以手册原始月末记录为准','恢复该假设'),
    TEXT.replace('当前以手册原始月末记录为准','假设重新生效'),
    '“'+TEXT+'”',TEXT.replace('”','」'),
    '此前“月末实盘改为7件”的情景假设已撤销？',
])
def test_wrong_value_unit_object_document_fact_or_reactivation_not_annotated(text):
    assert user.annotate(text,ctx())==(text,[])


def test_same_value_as_current_quantity_is_not_whitelisted():
    text=TEXT+'当前库存为7件。'
    masked,ann=user.annotate(text,ctx())
    assert len(ann)==1 and masked.endswith('当前库存为7件。')
    result=run(fake_model(text,quote=ACTUAL),raw=SUMMARY,question=SUMMARY,history=history())
    assert result['status']=='insufficient_evidence' and not result['claims']


@pytest.mark.parametrize('key,value',[
    ('quote','月末实盘改为9件'),('value','9'),('unit','吨'),('source_sha256','fake'),
    ('revocation_sha256','fake'),('revocation_start',1),('superseded_by','new-setting'),
])
def test_forged_or_superseded_old_input_is_not_annotation_source(key,value):
    c=ctx();c['revoked'][0][key]=value
    assert user.annotate(TEXT,c)==(TEXT,[])


def test_no_real_withdrawal_or_later_new_hypothesis_cannot_use_old_value():
    for h in [[turn()],history()+[dict(answer_id='p3',original_question='重新假设月末实盘改为9件。',status='completed',source_revisions=SCOPE)]]:
        c=user.build_context(SUMMARY,h,source_revisions=SCOPE)
        assert user.annotate(TEXT,c)==(TEXT,[])


@pytest.mark.parametrize('review',[True,False,None])
def test_verbatim_restatement_still_requires_independent_user_rule_review(review):
    model=fake_model(TEXT,review=review,quote=ACTUAL)
    result=run(model,raw=SUMMARY,question=SUMMARY,history=history())
    if review is True:
        assert result['status']=='completed' and len(result['claims'])==1
        assert result['claims'][0]['context_annotations'][0]['revocation_source_answer_id']=='p2'
        assert model.seen[-1][1]['user_context_claim_ids']==['c1']
    else:
        assert result['status']=='insufficient_evidence' and not result['claims']
