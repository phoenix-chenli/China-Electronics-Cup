"""History is not the whole document: offline reproduction of the early-stop bug."""
import copy
import json
import uuid

import pytest

from backend.document_engine import answer_document
from backend.model_gateway import ModelError
from backend.query_planner import resolve_followup
from test_answers_api import app_env, project, upload, submit, wait_terminal


RULE = '密封圈最低库存为8件；低于8件时提出采购申请，等于8件无需触发采购。'
ACTUAL = '月末实盘密封圈为11件，电子台账为12件；差额1件尚未查明，不得直接认定遗失。'
QUESTION = '那月末的实际盘点和电子台账分别是多少？按刚才的规则是否需要申请采购，二者差额应当怎样表述？'
NORMALIZED = '根据 harbor.docx，密封圈在月末的实际盘点和电子台账分别是多少？按运行手册规则（低于8件时提出采购申请，等于8件无需触发采购）是否需要申请采购？二者差额应当怎样表述？'
ABSENCE = '当前资料 harbor.docx 中未包含密封圈月末实际盘点数量和电子台账数量的具体数据，无法计算差额或判断是否需要申请采购。'
# Reconstructed from the persisted failure fields, not an invented raw response.
REJECTED = dict(status='insufficient_evidence', normalized_question=NORMALIZED,
                intent={'metric':'月末实际盘点数量、电子台账数量、二者差额', 'time_range':'月末'}, message=ABSENCE)
HISTORY = [{'answer_id':'ancestor', 'original_question':'密封圈的采购规则是什么？',
            'status':'completed', 'text':RULE}]
SOURCE = dict(id='source', name='harbor.docx', tables=[], sha256='a'*64,
              parser_version='fixture', warnings=[], metadata={'page_unit':'logical_section'},
              pages=[{'number':3, 'text':RULE+'\n'+ACTUAL}])


class Responses:
    def __init__(self, *values): self.values=list(values); self.calls=[]
    def __call__(self, messages, role):
        self.calls.append((role, messages[0]['content'], json.loads(messages[-1]['content'])))
        return copy.deepcopy(self.values.pop(0))


def resolve(gateway, mode='document', **kwargs):
    return resolve_followup({'question':QUESTION, 'mode':mode, 'table_name':None},
                            HISTORY, SOURCE, gateway, **kwargs)


@pytest.mark.parametrize('mode', ['document','multi_source'])
def test_actual_failure_after_history_recovery_is_reviewed_then_retrieved(mode):
    recovered={'records':HISTORY, 'scope_valid':True}
    gateway=Responses({'status':'history_required', 'history_requests':[{'answer_id':'ancestor'}]},
                      REJECTED, {'supported':True})
    response=resolve(gateway, mode, history_loader=lambda request:recovered)
    assert response['status']=='ready' and response['normalized_question']==NORMALIZED
    assert response['history_recovery']==recovered
    assert response['followup_diagnostics']['audit_only'] is True
    assert 'message' not in response and ABSENCE not in json.dumps(response,ensure_ascii=False)
    assert [r for r,_,_ in gateway.calls]==['document','document','verify']
    assert gateway.calls[-1][2]['recovered_history']==recovered
    assert gateway.calls[-1][2]['scope']['mode']==mode
    assert '尚未检索的内容不能视为不存在' in gateway.calls[0][1]
    assert '历史回答不是资料全文' in gateway.calls[-1][1]


def test_deferred_source_sufficiency_cannot_disable_independent_review():
    gateway=Responses(REJECTED, {'supported':False, 'failure_kind':'invalid_plan',
                                'reason':'改写遗漏用户条件'})
    result=resolve(gateway, review=False)
    assert result['status']=='failed' and 'clarification' not in result
    assert [r for r,_,_ in gateway.calls]==['document','verify']
    assert ABSENCE not in result['message']


@pytest.mark.parametrize('from_reviewer', [False, True])
def test_actual_missing_user_condition_remains_clarification(from_reviewer):
    detail={'question':'这次要比较哪两个月的库存？', 'field':'comparison_months', 'options':[]}
    values=[REJECTED, {'supported':False, 'failure_kind':'missing_condition', 'clarification':detail}] if from_reviewer else [
        {'status':'needs_clarification','clarification':detail},
        {'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True}]
    result=resolve(Responses(*values))
    assert result['status']=='needs_clarification' and result['clarification']==detail


@pytest.mark.parametrize('normalized', [None, '', ' '*4, 'x'*8001])
def test_deferred_absence_never_bypasses_invalid_rewrite(normalized):
    with pytest.raises(ModelError, match='完整问题'):
        resolve(Responses({**REJECTED,'normalized_question':normalized}))


@pytest.mark.parametrize('mode', ['table','database'])
def test_schema_modes_keep_existing_insufficient_data_behavior(mode):
    gateway=Responses(REJECTED)
    response=resolve(gateway, mode)
    assert response==REJECTED and len(gateway.calls)==1


def test_original_text_is_retrieved_after_resolution_and_still_needs_evidence_review():
    response=resolve(Responses(REJECTED, {'supported':True}))
    seen=[]
    def model(messages, purpose):
        payload=json.loads(messages[-1]['content']);seen.append((purpose,payload))
        if purpose=='document':
            chunk=next(c for c in payload['chunks'] if ACTUAL in c['text'])
            return {'status':'completed','claims':[{'text':ACTUAL,
                'citations':[{'chunk_id':chunk['chunk_id'],'quote':ACTUAL}]},
                {'text':'原始实盘为11件，未低于最低库存8件，不触发采购。',
                 'citations':[{'chunk_id':chunk['chunk_id'],'quote':RULE+'\n'+ACTUAL}]}]}
        return {'claims':[{'id':c['id'],'supported':True} for c in payload['claims']],
                'answer_complete':True,'coverage':[{'question_id':p['id'],'status':'answered',
                 'claim_ids':[c['id'] for c in payload['claims']]} for p in payload['question_parts']]}
    result=answer_document(SOURCE,response['normalized_question'],model,lambda:False,lambda _:None)
    assert result['status']=='completed' and len(result['claims'])==2
    assert any(e['quote']==ACTUAL and e['page']==3 for e in result['evidence'])
    assert 'chunks' in seen[0][1] and seen[-1][0]=='verify'
    # A clear question is not a guarantee that the actual source can answer it.
    missing=answer_document(SOURCE,response['normalized_question'],
        lambda *_:{'status':'insufficient_evidence','claims':[], 'message':'已检索资料，未形成可核验结论。'},
        lambda:False,lambda _:None)
    assert missing['status']=='insufficient_evidence' and not missing['claims']


@pytest.mark.parametrize('final_status', ['completed','insufficient_evidence'])
def test_service_reaches_document_engine_and_preserves_its_real_decision(app_env,monkeypatch,final_status):
    from backend import document_engine
    client,_,_=app_env;pid=project(client)
    sid=upload(client,pid,'harbor.txt',(RULE+'\n'+ACTUAL).encode())['source']['id']
    calls=[]
    def downstream(source,question,*args,actual_user_question=None,**kwargs):
        calls.append((source['id'],question,actual_user_question))
        return {'status':final_status,'text':'离线下游状态测试，不宣称模型成绩。',
                'claims':[],'evidence':[],'verification':{'status':'limited'}}
    monkeypatch.setattr(document_engine,'answer_document',downstream)
    # Establish a real saved parent before replaying the failed interpreter shape.
    first=wait_terminal(client,pid,submit(client,pid,dict(source_id=sid,mode='document',
            question='密封圈的采购规则是什么？',request_id=str(uuid.uuid4())))['id'])
    class Gateway:
        models={};usage=[]
        def __call__(self,messages,purpose):
            payload=json.loads(messages[-1]['content'])
            if 'allowed_fields' in payload:return {'status':'unavailable'}
            if 'interpretation' in payload:return {'supported':True}
            assert purpose=='document' and 'scope' in payload
            return copy.deepcopy(REJECTED)
    client.app.state.answers.gateway_factory=lambda *_:Gateway()
    second=wait_terminal(client,pid,submit(client,pid,dict(source_id=sid,mode='document',
        question=QUESTION,parent_answer_id=first['id'],request_id=str(uuid.uuid4())))['id'])
    assert len(calls)==2 and calls[-1]==(sid,NORMALIZED,QUESTION)
    assert second['status']==final_status
    assert second['result']['followup_diagnostics']['audit_only'] is True
    assert ABSENCE not in second['result']['text']
