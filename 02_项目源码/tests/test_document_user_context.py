"""Offline user-origin scenarios; fake models never authorize unsupported values."""
import copy
import hashlib
import json

import pytest

from backend import document_engine as engine
from backend import document_user_context as user

SCOPE = [dict(id='doc', sha256='a'*64, parser_version='parser1', revision=1, semantic_revision=0)]
ASSUME = '先作一个假设：月末实盘改为7件，采购规则保持不变。此时是否应提出申请？请明确这只是情景假设。'
REVOKE = '撤销刚才的7件假设，回到手册原始月末记录。现在是否触发采购，库存差额能否直接认定为遗失？'
RULE = '密封圈最低库存为8件；低于8件时提出采购申请，等于8件无需触发采购。'
ACTUAL = '月末实盘密封圈为11件，电子台账为12件；差额1件尚未查明，不得直接认定遗失。'
STATEMENT = '在用户假设月末实盘改为7件的情景下，依据低于8件时申请的规则，应提出采购申请。'


def ctx(q=ASSUME, history=(), scope=SCOPE):
    return user.build_context(q, history, source_revisions=scope)


def turn(q=ASSUME, **changes):
    return dict(answer_id='p1', original_question=q, status='completed', source_revisions=SCOPE, **changes)


def document():
    return dict(id='doc', name='测试手册.docx', sha256='a'*64, parser_version='parser1',
                status='ready', archived=False, warnings=[], metadata={'page_unit':'logical_section'},
                pages=[{'number':3,'text':RULE+'\n'+ACTUAL}])


def test_current_input_has_verbatim_provenance_not_document_evidence():
    context=ctx();item=context['active'][0]
    assert item['subject']=='月末实盘' and item['value']=='7' and item['unit']=='件'
    assert item['source_text'][item['start']:item['end']]==item['quote']=='月末实盘改为7件'
    assert item['source_sha256']==hashlib.sha256(ASSUME.encode()).hexdigest()
    assert item['is_hypothetical'] and not item['is_document_evidence']
    masked, annotations=user.annotate(STATEMENT,context)
    assert '7' not in masked and '8' in masked and len(masked)==len(STATEMENT)
    assert len(annotations)==1
    a=annotations[0]
    assert STATEMENT[a['start']:a['end']]=='7' and a['semantic_review_required']


@pytest.mark.parametrize('question',[
    '月末实盘为7件。', '不要假设月末实盘为7件。', '并非假设月末实盘为7件。',
    '原文说假设月末实盘为7件。', '手册要求假设月末实盘为7件。',
    '“假设月末实盘为7件”，请解释这句话。', '他说“假设月末实盘为7件”。',
    '假设月末实盘为7件/天。', '假设月末实盘为7件 每天。',
    '假设月末实盘为7kg/m2。', '假设月末实盘为7e4件。',
    '假设月末实盘为1000000000000000001件。',
])
def test_reported_negative_quoted_and_unsupported_units_are_not_authorization(question):
    assert ctx(question)['active']==[]


def test_only_actual_words_authorize_not_model_normalization():
    assert user.build_context(None) is None
    h=turn(q='请解释采购规则。');h['normalized_question']=ASSUME
    assert ctx('继续刚才的假设。',[h])['active']==[]


@pytest.mark.parametrize('text',[
    '假设月末实盘改为9件，应提出申请。', '假设月末实盘改为7吨，应提出申请。',
    '假设电子台账改为7件，应提出申请。', '实际库存月末实盘改为7件。',
    '假设月末实盘改为7件，但实际库存为7件。', '不要假设月末实盘改为7件。',
])
def test_changed_subject_value_unit_or_actual_claim_not_annotated(text):
    masked,annotations=user.annotate(text,ctx())
    assert masked==text and annotations==[]


def test_same_number_elsewhere_not_whitelisted():
    text=STATEMENT+'实际采购了7件。'
    masked,annotations=user.annotate(text,ctx())
    assert len(annotations)==1 and masked.endswith('实际采购了7件。')


@pytest.mark.parametrize('key,value',[('value','9'),('unit','吨'),('subject','别的对象'),('source_sha256','b'*64),('start',0)])
def test_tampered_provenance_not_accepted(key,value):
    context=ctx();context['active'][0][key]=value
    assert user.annotate(STATEMENT,context)==(STATEMENT,[])


def test_revoke_exact_previous_input_and_never_reactivate_old_value():
    c=ctx(REVOKE,[turn()]);assert not c['active'] and len(c['revoked'])==1
    text='撤销刚才的7件假设，回到手册原始月末记录。原始实盘为11件，不触发采购。'
    masked,annotations=user.annotate(text,c)
    assert '7' not in masked and '11' in masked
    assert annotations[0]['kind']=='user_assumption_revocation'
    assert user.annotate(STATEMENT,c)==(STATEMENT,[])
    reused=text+'继续假设月末实盘改为7件。'
    assert '7' in user.annotate(reused,c)[0]
    later=ctx('沿用刚才假设回答。',[turn(),dict(answer_id='p2',original_question=REVOKE,status='completed',source_revisions=SCOPE)])
    assert later['active']==[]


@pytest.mark.parametrize('revoke',['不要撤销刚才的7件假设。','原文说撤销刚才的7件假设。','“撤销刚才的7件假设”。','撤销电子台账7件假设。'])
def test_invalid_revocation_does_not_claim_previous_input_was_withdrawn(revoke):
    assert ctx(revoke,[turn()])['revoked']==[]


def test_history_requires_explicit_current_carry_and_same_completed_source_version():
    assert ctx('现在如何申请？',[turn()])['active']==[]
    assert ctx('沿用刚才的假设继续。',[turn()])['active'][0]['source_answer_id']=='p1'
    for key,value in [('status','failed'),('source_revisions',[{**SCOPE[0],'sha256':'b'*64}]),('source_revisions',[]),('kind','structured_context')]:
        t=turn();t[key]=value
        assert ctx(REVOKE,[t])['revoked']==[]
        assert ctx('沿用刚才的假设继续。',[turn(),t])['active']==[]


@pytest.mark.parametrize('change',[None,False,'true'])
def test_dedicated_review_is_required_for_each_boolean(change):
    required={'c1'}
    assert user.reviewed_ids({'claims':[{'id':'c1','supported':True}]},required)==set()
    checks={k:True for k in user._CHECKS};checks['scope_supported']=change
    assert user.reviewed_ids({'user_context_relations':[{'id':'c1',**checks}]},required)==set()
    assert user.reviewed_ids({'user_context_relations':[{'id':'c1',**{k:True for k in user._CHECKS}}]},required)==required


def fake_model(text=STATEMENT, review=True, quote=RULE):
    seen=[]
    def model(messages,purpose):
        payload=json.loads(messages[-1]['content']);seen.append((purpose,payload))
        if purpose=='document':
            chunk=next(c for c in payload['chunks'] if quote in c['text'])
            return {'status':'completed','claims':[{'text':text,'citations':[{'chunk_id':chunk['chunk_id'],'quote':quote}]}]}
        required=payload.get('user_context_claim_ids',[])
        return {'claims':[{'id':c['id'],'supported':True} for c in payload['claims']], 'answer_complete':True,
                'coverage':[{'question_id':p['id'],'status':'answered','claim_ids':[c['id'] for c in payload['claims']]} for p in payload['question_parts']],
                **({'user_context_relations':[{'id':cid,**{k:review for k in user._CHECKS}} for cid in required]} if review is not None else {})}
    model.seen=seen
    return model


def run(model, raw=ASSUME, question=ASSUME, history=()):
    return engine.answer_document(document(),question,model,lambda:False,lambda _:None,
                                  actual_user_question=raw,history=history,source_revisions=SCOPE)


def test_full_document_rule_and_user_input_have_separate_provenance_and_review():
    model=fake_model('  '+STATEMENT+'  ');result=run(model)
    assert result['status']=='completed' and result['verification']['status']=='passed'
    claim=result['claims'][0];a=claim['context_annotations'][0]
    assert claim['text'][a['start']:a['end']]=='7'
    assert result['evidence'][0]['quote']==RULE and '7' not in result['evidence'][0]['quote']
    assert result['evidence'][0]['page']==3
    assert model.seen[-1][1]['user_context_claim_ids']==['c1']
    assert result['user_context']['active'][0]['quote']=='月末实盘改为7件'


@pytest.mark.parametrize('review',[None,False])
def test_generic_supported_true_cannot_bypass_separate_user_rule_review(review):
    result=run(fake_model(review=review))
    assert result['status']=='insufficient_evidence' and result['claims']==[]


@pytest.mark.parametrize('raw,text',[(None,STATEMENT),('请解释原始记录。',STATEMENT),(ASSUME,STATEMENT+'已经采购7件。')])
def test_normalized_hypothesis_or_extra_business_value_cannot_pass(raw,text):
    result=run(fake_model(text),raw=raw)
    assert result['status']=='insufficient_evidence' and result['claims']==[]


def test_revoke_answer_can_explain_original_facts_but_not_use_old_scenario():
    text='撤销刚才的7件假设，回到手册原始月末记录。原始实盘为11件，电子台账为12件，差额1件尚未查明。'
    result=run(fake_model(text,quote=ACTUAL),raw=REVOKE,question=REVOKE,history=[turn()])
    assert result['status']=='completed' and not result['user_context']['active']
    assert result['claims'][0]['context_annotations'][0]['kind']=='user_assumption_revocation'
    bad=run(fake_model(STATEMENT),raw=REVOKE,question=REVOKE,history=[turn()])
    assert bad['claims']==[]


def test_service_supplies_real_branch_and_raw_question_before_normalization(app_env, monkeypatch):
    import uuid
    from backend import document_engine, query_planner, answer_service
    from test_answers_api import project, upload, submit, wait_terminal
    client, gateway, _=app_env
    pid=project(client);detail=upload(client,pid,'rules.txt',(RULE+'\n'+ACTUAL).encode())
    sid=detail['source']['id'];received=[]
    def downstream(source, question, *args, actual_user_question=None, history=(), source_revisions=None, **kwargs):
        received.append(dict(question=question,actual=actual_user_question,history=history,scope=source_revisions))
        return {'status':'completed','text':'离线测试结论','claims':[],'evidence':[], 'verification':{'status':'passed'}}
    monkeypatch.setattr(document_engine,'answer_document',downstream)
    monkeypatch.setattr(answer_service,'effective_question',lambda q,patch:q+' 控制器补充文字。')
    monkeypatch.setattr(query_planner,'resolve_followup',lambda *a,**k:{'status':'ready','normalized_question':'模型重写为9件的情景。','intent':{}})
    first=wait_terminal(client,pid,submit(client,pid,{'source_id':sid,'mode':'document','question':ASSUME,'request_id':str(uuid.uuid4())})['id'])
    assert first['status']=='completed'
    second=wait_terminal(client,pid,submit(client,pid,{'source_id':sid,'mode':'document','question':REVOKE,'parent_answer_id':first['id'],'request_id':str(uuid.uuid4())})['id'])
    assert second['status']=='completed'
    assert received[0]['actual']==ASSUME and received[0]['history']==[]
    assert received[1]['actual']==REVOKE and received[1]['question']=='模型重写为9件的情景。'
    h=received[1]['history'];assert len(h)==1
    assert h[0]['answer_id']==first['id'] and h[0]['original_question']==ASSUME
    assert h[0]['source_revisions']==received[1]['scope'] and h[0]['status']=='completed'
    context=user.build_context(received[1]['actual'],h,source_revisions=received[1]['scope'])
    assert context['active']==[] and context['revoked'][0]['value']=='7'


# Uses the existing isolated local test app and its fake gateway; no paid API.
from test_answers_api import app_env


@pytest.mark.parametrize('question',[
    '假设月末实盘改为7件。撤销刚才的7件假设，回到原始记录。',
    '假设月末实盘改为7件，不要沿用这个假设。',
    '假设月末实盘改为7件，不再沿用这个假设。',
])
def test_later_same_turn_revocation_wins_over_prior_assignment(question):
    context=ctx(question)
    assert context['active']==[] and len(context['revoked'])==1
    assert user.annotate(STATEMENT,context)==(STATEMENT,[])


def test_revoke_then_new_explicit_assignment_preserves_original_order():
    context=ctx('撤销刚才的7件假设；重新假设月末实盘改为6件。',[turn()])
    assert len(context['revoked'])==1 and context['revoked'][0]['value']=='7'
    assert len(context['active'])==1 and context['active'][0]['value']=='6'
    assert user.annotate('假设月末实盘改为6件，应按规则处理。',context)[1]


def test_same_turn_revocation_survives_real_history_replay():
    old=turn('假设月末实盘改为7件。撤销刚才的7件假设。')
    assert ctx('沿用刚才的假设继续。',[old])['active']==[]
