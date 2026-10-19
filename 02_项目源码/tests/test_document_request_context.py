"""Original request survives retrieval rewrites and all existing repair stages."""
import copy
import json

import pytest

from backend import document_engine as engine,completeness
from test_document_user_context import document,SCOPE,RULE,ACTUAL

RAW='请把我们最终确认的库存和申请规则整理为说明，并保留适用范围。'
NORMALIZED='查询库存。'


def history():
    return [dict(answer_id='h'+str(i),original_question=q,status='completed',source_revisions=copy.deepcopy(SCOPE),
                 parent_answer_id='h'+str(i-1) if i>1 else None,conversation_id='h1',turn_index=i,
                 answer='旧回答捏造99件。',normalized_question='重写捏造98件。',intent={'metric':'伪造'})
            for i,q in enumerate(['当前是否触发采购？','能否声称其他站点或V2.2采用同一规则？'],1)]


def test_original_raw_and_same_scope_user_requests_only():
    c=engine._request_context(NORMALIZED,RAW,history(),SCOPE)
    assert c['actual_user_question']==RAW and c['normalized_question']==NORMALIZED
    assert c['question_parts']==completeness.question_parts(RAW)
    assert c['question_parts']!=completeness.question_parts(NORMALIZED)
    assert [x['answer_id'] for x in c['historical_user_requests']]==['h1','h2']
    serialized=json.dumps(c,ensure_ascii=False)
    assert all(value not in serialized for value in ['捏造','伪造','normalized_question":"重写'])
    assert c['history_purpose']=='intent_context_only_not_evidence_or_numeric_authorization'


@pytest.mark.parametrize('change',[
    {'status':'failed'},{'kind':'structured_context'},
    {'parent_answer_id':'wrongbranch'},{'conversation_id':'wrong'},
    {'source_revisions':[{**SCOPE[0],'sha256':'b'*64}]},
])
def test_bad_tail_ancestor_cannot_supply_summary_context(change):
    h=history();h[-1].update(change)
    assert engine._request_context(NORMALIZED,RAW,h,SCOPE)['historical_user_requests']==[]


@pytest.mark.parametrize('raw',['当前库存是多少？','请整理这份文档的内容。',None])
def test_ordinary_request_does_not_inherit_all_historical_questions(raw):
    c=engine._request_context(NORMALIZED,raw,history(),SCOPE)
    assert not c['historical_user_requests']
    if raw is None:
        assert c['actual_user_question'] is None and c['question_parts_origin']=='legacy_question'
        assert c['question_parts']==completeness.question_parts(NORMALIZED)


def test_summary_history_is_bounded_without_truncating_or_inventing_quotes():
    h=history();h[0]['original_question']='原始问题'+('甲'*9000);h[1]['original_question']='第二原始问题'+('乙'*9000)
    c=engine._request_context(NORMALIZED,RAW,h,SCOPE)
    assert c['historical_user_requests_limited'] is True
    assert len(c['historical_user_requests'])==1
    assert c['historical_user_requests'][0]['original_question']==h[1]['original_question']


def test_all_existing_generation_review_coverage_and_supplement_stages_use_raw_parts():
    seen=[];doc_count=0;verify_count=0
    def gateway(messages,purpose):
        nonlocal doc_count,verify_count
        p=json.loads(messages[-1]['content']);seen.append((purpose,p))
        assert p['actual_user_question']==RAW and p['normalized_question']==NORMALIZED
        assert p['question_parts']==completeness.question_parts(RAW)
        assert [x['original_question'] for x in p['historical_user_requests']]==[x['original_question'] for x in history()]
        if purpose=='document':
            doc_count+=1;ch=next(x for x in p['chunks'] if ACTUAL in x['text'])
            claims=[{'text':'月末实盘为11件。','citations':[{'chunk_id':ch['chunk_id'],'quote':ACTUAL}]}]
            if doc_count>1:claims.append({'text':'密封圈最低库存为8件。','citations':[{'chunk_id':ch['chunk_id'],'quote':RULE}]})
            return {'status':'completed','claims':claims}
        verify_count+=1
        if 'protocol_feedback' in p:
            return {'answer_complete':False,'coverage':[{'question_id':q['id'],'status':'evidence_available','claim_ids':[],
                    'evidence_ids':[p['available_evidence'][0]['id']]} for q in p['question_parts']]}
        result={'claims':[{'id':c['id'],'supported':True} for c in p['claims']],'answer_complete':doc_count>1}
        result['coverage']=[] if doc_count==1 else [{'question_id':q['id'],'status':'answered',
             'claim_ids':[c['id'] for c in p['claims']]} for q in p['question_parts']]
        return result
    result=engine.answer_document(document(),NORMALIZED,gateway,lambda:False,lambda _:None,
                                  actual_user_question=RAW,history=history(),source_revisions=SCOPE)
    assert result['status']=='completed'
    assert [purpose for purpose,_ in seen]==['document','verify','verify','document','verify']
    assert result['completion_repair']['attempted'] is True and result['completion_repair']['resolved'] is True
    assert result['completeness']['items'][0]['text']==completeness.question_parts(RAW)[0]['text']


def test_historical_user_request_value_is_not_source_numeric_authorization():
    h=history();h[0]['original_question']='当前库存是不是7件？'
    seen=[]
    def gateway(messages,purpose):
        p=json.loads(messages[-1]['content']);seen.append(p)
        assert p['historical_user_requests'][0]['original_question']=='当前库存是不是7件？'
        if purpose=='document':
            ch=next(x for x in p['chunks'] if ACTUAL in x['text'])
            return {'status':'completed','claims':[{'text':'当前库存为7件。','citations':[{'chunk_id':ch['chunk_id'],'quote':ACTUAL}]}]}
        raise AssertionError('unsupported historical query number must not reach semantic approval')
    result=engine.answer_document(document(),NORMALIZED,gateway,lambda:False,lambda _:None,
                                  actual_user_question=RAW,history=h,source_revisions=SCOPE)
    assert result['status']=='insufficient_evidence' and result['claims']==[]
