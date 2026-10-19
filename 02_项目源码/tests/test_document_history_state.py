import copy
import json

import pytest

from backend import document_user_context as user
from backend import document_query_references as refs
from backend import document_engine as engine

SCOPE=[dict(id='d',sha256='a'*64,parser_version='p',revision='r',semantic_revision='s')]
ASSUME='假设月末实盘改为7件。'
REVOKE='撤销刚才的7件假设，回到原始记录。'
ASK='能否确认V2.2采用同一规则？'
SUMMARY='请整理最终结论，并说明刚才的假设是否仍生效。'
RULE='最低库存为8件，低于8件申请，等于8件不触发。'
ACTUAL='月末实盘11件，台账12件，差额1件未知。'
VERSION='本手册版本V2.1，仅适用于本试验站。'


def history():
    return [dict(answer_id='h'+str(i),original_question=q,status='completed',source_revisions=copy.deepcopy(SCOPE),
                 parent_answer_id='h'+str(i-1) if i>1 else None,conversation_id='h1',turn_index=i)
            for i,q in enumerate([ASSUME,REVOKE,ASK],1)]


def context(h=None,q=SUMMARY):
    return user.build_context(q,history() if h is None else h,source_revisions=SCOPE)


def test_historical_withdrawal_keeps_real_event_and_is_not_active():
    c=context();assert c['active']==[] and len(c['revoked'])==1
    event=c['revoked'][0]
    assert event['source_answer_id']=='h1' and event['revocation_source_answer_id']=='h2'
    assert event['revocation_source_text'][event['revocation_start']:event['revocation_end']]=='撤销刚才的7件假设'
    for text in ['此前假设已撤销。','该假设不再生效。','刚才的假设已经取消。']:
        masked,annotations=user.annotate(text,c)
        assert masked==text and len(annotations)==1
        a=annotations[0];assert a['kind']=='user_assumption_state' and a['revocation_source_answer_id']=='h2'
        assert a['source_text'][a['source_start']:a['source_end']]==a['quote']


@pytest.mark.parametrize('change',[
    {'status':'failed'}, {'kind':'structured_context'},
    {'source_revisions':[{**SCOPE[0],'sha256':'b'*64}]},
    {'source_revisions':[{**SCOPE[0],'parser_version':'other'}]},
    {'source_revisions':[{**SCOPE[0],'semantic_revision':'other'}]},
    {'parent_answer_id':'other_branch'}, {'conversation_id':'other'}, {'turn_index':9},
])
def test_bad_or_wrong_branch_ancestor_does_not_provide_withdrawal(change):
    h=history();h[1].update(change);c=context(h)
    assert c['revoked']==[]
    assert user.annotate('此前假设已撤销。',c)[1]==[]


def test_later_new_assumption_supersedes_prior_withdrawal():
    h=history();h[2]['original_question']='重新假设月末实盘改为9件。'
    c=context(h);assert c['revoked'][0]['superseded_by']==c['active'][0]['id'] and c['active'][0]['value']=='9'
    assert user.annotate('此前假设已撤销。',c)[1]==[]
    c=context(q='重新假设月末实盘改为9件。');assert c['revoked'][0]['superseded_by']==c['active'][0]['id'] and c['active'][0]['value']=='9'
    c=context(h,q='请整理最终结论。');assert c['active']==[] and c['revoked'][0]['superseded_by']
    assert user.annotate('此前假设已撤销。',c)[1]==[]


@pytest.mark.parametrize('text',[
    '不是此前假设已撤销。','不要声称此前假设已撤销。','“此前假设已撤销”。',
    '此前假设已撤销？','此前假设已撤销，但仍沿用该假设。',
    '另一个假设已撤销。',
])
def test_ambiguous_negated_quoted_or_reactivated_state_not_annotated(text):
    assert user.annotate(text,context())[1]==[]


def test_tampered_revocation_or_assumption_origin_is_not_a_state_source():
    for key,value in [('revocation_sha256','x'),('revocation_start',1),('source_sha256','x'),('quote','库存改为8件')]:
        c=context();c['revoked'][0][key]=value
        assert user.annotate('此前假设已撤销。',c)[1]==[]


def test_history_query_reference_has_ancestor_origin_and_no_numeric_whitelist():
    c=refs.build(SUMMARY,history(),source_revisions=SCOPE)
    assert c['references']==[] and c['history_references'][0]['source_answer_id']=='h3'
    text='无法确认V2.2采用同一规则；实际金额为2.2万元。'
    masked,ann=refs.annotate(text,c)
    assert len(ann)==1 and ann[0]['source_answer_id']=='h3'
    assert ann[0]['source_text']==ASK and '实际金额为2.2万元' in masked
    assert refs.build('换个话题讨论天气。',history(),source_revisions=SCOPE) is None


@pytest.mark.parametrize('change',[
    {'status':'failed'}, {'parent_answer_id':'other_branch'}, {'conversation_id':'other'},
    {'source_revisions':[{**SCOPE[0],'revision':'other'}]},
])
def test_bad_query_ancestor_cannot_authorize_unknown_reference(change):
    h=history();h[2].update(change)
    c=refs.build(SUMMARY,h,source_revisions=SCOPE)
    assert c is None


def test_model_summary_and_assistant_answer_never_supply_user_history():
    h=history();h[0]['original_question']='解释规则。';h[0]['normalized_question']=ASSUME
    h[0]['answer']=ASSUME;h[1]['original_question']='继续。';h[1]['normalized_question']=REVOKE
    h[2]['original_question']='谢谢。';h[2]['answer']=ASK
    assert context(h)['revoked']==[]
    assert refs.build(SUMMARY,h,source_revisions=SCOPE) is None


def run(*,review=True):
    source=dict(id='d',name='manual.docx',sha256='a'*64,parser_version='p',status='ready',archived=False,warnings=[],
                metadata={'page_unit':'logical_section'},pages=[{'number':3,'text':RULE+'\n'+ACTUAL},{'number':5,'text':VERSION}])
    pairs=[('月末实盘11件，台账12件，差额1件未知。',ACTUAL),
           ('最低库存为8件，低于8件申请，等于8件不触发。',RULE),
           ('月末实盘11件不低于8件，因此当前不触发采购。',RULE+'\n'+ACTUAL),
           ('本手册版本V2.1，仅适用于本试验站。',VERSION),
           ('无法确认V2.2采用同一规则。',VERSION),
           ('此前假设已撤销，当前按月末实盘11件处理。',ACTUAL)]
    seen=[]
    def fake(messages,purpose):
        payload=json.loads(messages[-1]['content']);seen.append((purpose,payload))
        if purpose=='document':
            assert payload['user_context']['revoked'][0]['revocation_source_answer_id']=='h2'
            assert payload['user_context']['query_references']['history_references'][0]['source_answer_id']=='h3'
            claims=[]
            for text,quote in pairs:
                chunk=next(ch for ch in payload['chunks'] if quote in ch['text'])
                claims.append({'text':text,'citations':[{'chunk_id':chunk['chunk_id'],'quote':quote}]})
            return {'status':'completed','claims':claims}
        required=payload.get('user_context_claim_ids',[])
        return {'claims':[{'id':x['id'],'supported':True} for x in payload['claims']], 'answer_complete':True,
                'coverage':[{'question_id':q['id'],'status':'answered','claim_ids':[x['id'] for x in payload['claims']]} for q in payload['question_parts']],
                **({'user_context_relations':[{'id':cid,**{k:review for k in user._CHECKS}} for cid in required]} if review is not None else {})}
    result=engine.answer_document(source,SUMMARY,fake,lambda:False,lambda _:None,actual_user_question=SUMMARY,history=history(),source_revisions=SCOPE)
    return result,seen


def test_state_and_history_query_require_independent_review_with_source_facts():
    result,seen=run();assert result['status']=='completed' and len(result['claims'])==6
    assert seen[-1][1]['user_context_claim_ids']==['c5','c6']
    assert result['claims'][-1]['context_annotations'][0]['kind']=='user_assumption_state'
    for value in [False,None]:
        result,_=run(review=value)
        assert result['status']=='insufficient_evidence'
        assert all(not cl.get('context_annotations') for cl in result['claims'])
