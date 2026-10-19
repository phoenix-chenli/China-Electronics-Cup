"""Offline question-origin references do not authorize document facts."""
import copy
import json

import pytest

from backend import document_query_references as refs
from backend import document_engine as engine

QUESTION = '这份手册是哪一版本？能否确认V2.2也采用同一规则，或2027年4月2日已经生效？'
QUOTE = '本手册版本为V2.1，于2025年3月1日签发，只适用于青湾港试验站。'


@pytest.mark.parametrize('text', [
    '仅凭当前资料，无法确认V2.2也采用同一规则。',
    '不能声称V2.2版本也采用同一规则。',
    '尚不能确认用户所问的V2.2是否采用同一规则。',
    '当前已读片段未提供V2.2版本的规则，不能由此证明该版本不存在。',
    '无法确认2027年4月2日是否已生效。',
])
def test_exact_question_target_is_local_uncertainty_only(text):
    context = refs.build(QUESTION)
    masked, annotations = refs.annotate(text, context)
    assert len(annotations) == 1
    a = annotations[0]
    assert text[a['start']:a['end']] == a['quote']
    assert QUESTION[a['source_start']:a['source_end']] == a['quote']
    assert a['semantic_review_required'] and not a['is_document_evidence']
    assert not a['is_hypothetical'] and len(masked) == len(text)


@pytest.mark.parametrize('text', [
    'V2.2采用同一规则。', 'V2.2存在。', 'V2.2不存在。', 'V2.2已生效。',
    '2027年4月2日已经生效。', '文档未提及V2.2。', '资料未提供V2.2。',
    '整个原文没有V2.2。', '并非无法确认V2.2的规则。',
    '不能说无法确认V2.2。', '不要声称无法确认V2.2。',
    '无法确认V2.20的规则。', '无法确认V2.2.1的规则。',
    '无法确认V3.2的规则。', '无法确认2027年4月3日是否生效。',
    '无法确认V2.2，事实上已经生效。', '无法确认V2.2，但是该版本已实施。',
    '原文说无法确认V2.2。', '他说“无法确认V2.2”。',
])
def test_unsupported_fact_global_absence_or_changed_target_is_not_annotated(text):
    assert refs.annotate(text, refs.build(QUESTION)) == (text, [])


def test_no_global_numeric_whitelist_and_each_occurrence_requires_uncertainty():
    text = '无法确认V2.2的规则；V2.2已经生效，金额为2.2万元。'
    masked, annotations = refs.annotate(text, refs.build(QUESTION))
    assert len(annotations) == 1
    assert 'V2.2已经生效，金额为2.2万元' in masked


def test_actual_words_only_and_tampering_is_rejected():
    assert refs.build(None) is None
    assert refs.annotate('无法确认V2.2的规则。', refs.build('请查资料。'))[1] == []
    for key, value in [('source_sha256', 'x'), ('source_text', '能否确认V2.3？')]:
        ctx = refs.build(QUESTION); ctx[key] = value
        assert refs.annotate('无法确认V2.2的规则。', ctx)[1] == []
    for key, value in [('quote', 'V2.3'), ('start', 0), ('kind', 'date')]:
        ctx = refs.build(QUESTION); ctx['references'][0][key] = value
        assert refs.annotate('无法确认V2.2的规则。', ctx)[1] == []


def source():
    return dict(id='doc', name='manual.docx', sha256='a'*64, parser_version='p1',
                status='ready', archived=False, warnings=[], metadata={'page_unit':'logical_section'},
                pages=[{'number':5, 'text':QUOTE}])


def run(text, *, raw=QUESTION, relation=True, quote=QUOTE):
    seen=[]
    def fake(messages, purpose):
        payload=json.loads(messages[-1]['content']);seen.append((purpose,payload))
        if purpose=='document':
            chunk=payload['chunks'][0]
            return {'status':'completed','claims':[{'text':text,'citations':[{'chunk_id':chunk['chunk_id'],'quote':quote}]}]}
        return {'claims':[{'id':c['id'],'supported':True} for c in payload['claims']],
                'answer_complete':True,
                'coverage':[{'question_id':p['id'],'status':'answered','claim_ids':[c['id'] for c in payload['claims']]} for p in payload['question_parts']],
                **({'user_context_relations':[{'id':cid, 'intent_supported':True,'scope_supported':relation,'rule_supported':True,'status_supported':True} for cid in payload.get('user_context_claim_ids',[])]} if relation is not None else {})}
    result=engine.answer_document(source(),QUESTION,fake,lambda:False,lambda _:None,
                                  actual_user_question=raw,source_revisions=[{'id':'doc','sha256':'a'*64,'parser_version':'p1'}])
    return result,seen


def test_safe_limitation_reaches_review_but_is_not_auto_supported():
    text='当前已知手册版本为V2.1。仅凭当前资料，无法确认V2.2是否采用同一规则；未确认不等于证明该版本不存在或规则不同。'
    result,seen=run(text)
    assert result['status']=='completed'
    assert result['claims'][0]['context_annotations'][0]['kind']=='user_query_reference'
    assert result['evidence'][0]['quote']==QUOTE and 'V2.2' not in QUOTE
    assert seen[-1][1]['user_context_claim_ids']==['c1']
    for relation in [False,None]:
        rejected,_=run(text,relation=relation)
        assert rejected['status']=='insufficient_evidence' and rejected['claims']==[]


@pytest.mark.parametrize('text', [
    'V2.2采用同一规则。', 'V2.2不存在。', 'V2.2已生效。',
    '无法确认V2.2的规则，实际金额为2.2万元。',
    '仅凭当前资料，不能声称V2.2也采用同一规则，因为资料记载V2.1，未提及V2.2。',
])
def test_full_gate_keeps_affirmative_extra_value_and_global_absence_rejected(text):
    result,_=run(text)
    assert result['status']=='insufficient_evidence' and result['claims']==[]


def test_normalization_and_invalid_citation_cannot_authorize_reference():
    text='无法确认V2.2是否采用同一规则。'
    assert run(text,raw='请查资料。')[0]['claims']==[]
    assert run(text,raw=None)[0]['claims']==[]
    assert run(text,quote='本手册为V2.2。')[0]['claims']==[]
