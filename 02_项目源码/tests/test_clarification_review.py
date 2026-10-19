"""Synthetic scripted responses, not reconstructed R6 candidates; zero network."""
import copy
import hashlib
import json
import sqlite3
import pytest
from backend.cross_source_engine import answer_cross_source
from backend.parsers import sqlite_tables
from backend.model_gateway import ModelError
from backend import clarification_review as cr

@pytest.fixture
def source(tmp_path):
    path=tmp_path/'sites.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE sites(id TEXT PRIMARY KEY,name TEXT,bank TEXT); INSERT INTO sites VALUES('D01','重名点','北岸'),('D02','重名点','南岸');")
    tables,warnings=sqlite_tables(path)
    return {'id':'db','name':'sites.sqlite','data_path':str(path),'tables':tables,'warnings':warnings,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'parser_version':'controlled-test','metadata':{}}

def plan():return {'actions':[{'id':'S1','kind':'sql','source_id':'db','sql':'SELECT id,name,bank FROM sites','parameters':[],'depends_on':[]}]}
def proposal():return {'status':'needs_clarification','clarification':{'question':'你指北岸D01还是南岸D02的重名点？','field':'site_id','options':['D01 北岸','D02 南岸']}}
WRONG_SHAPE={'supported':False,'answer_complete':False,'conflict':False,'claims':[], 'coverage':[{'question_id':'Q1','status':'missing_evidence','claim_ids':[],'evidence_ids':[]}],'table_relations':[],'query_relations':[]}

class Gateway:
    def __init__(self,*responses,on_call=None):self.responses=list(responses);self.calls=[];self.on_call=on_call
    def __call__(self,messages,purpose):
        raw=messages[-1]['content'];payload=json.loads(raw[0]['text'] if isinstance(raw,list) else raw)
        self.calls.append({'messages':messages,'payload':payload,'purpose':purpose})
        assert self.responses, 'unexpected model call'
        response=self.responses.pop(0)
        if self.on_call:self.on_call(len(self.calls))
        if isinstance(response,Exception):raise response
        return copy.deepcopy(response)

def run(source,gateway,**kw):return answer_cross_source([source],'重名点有多少巡检？',[],gateway,**kw)
CLARIFICATION_RECEIPT_KINDS = {'clarification_candidate_received', 'independent_review_received'}
def clarification_records(history):
    return [event for event in history
            if event.get('kind') in CLARIFICATION_RECEIPT_KINDS
            and isinstance(event.get('record'), dict)
            and event['record'].get('scope') == 'clarification_attempt']
def records(events):return clarification_records(events[-1]['audit_history']) if events else []


def test_missing_review_field_replans_with_actual_candidate_and_keeps_receipts(source):
    events=[];g=Gateway(plan(),proposal(),WRONG_SHAPE,proposal(),{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True,'reason':'真实结果有两个对象'})
    status,result=run(source,g,audit=events.append)
    assert status=='needs_clarification' and result['clarification']==proposal()['clarification']
    assert [c['purpose'] for c in g.calls]==['table','table','verify','table','verify']
    first=g.calls[2];assert first['messages'][0]['content']==cr.INSTRUCTIONS
    assert 'claims:[' not in first['messages'][0]['content']
    assert first['payload']['question']=='重名点有多少巡检？'
    assert first['payload']['evidence'][0]['query_result']['rows']==[['D01','重名点','北岸'],['D02','重名点','南岸']]
    assert g.calls[3]['payload']['last_clarification_review_repair']['previous_clarification']==proposal()['clarification']
    history=records(events)
    assert len(history)==4
    assert [h['kind'] for h in history]==['clarification_candidate_received','independent_review_received']*2
    # Sequence is global: dispatch receipts remain in the full audit trail.
    sequences=[h['sequence'] for h in history]
    assert sequences==sorted(set(sequences))
    full_history=events[-1]['audit_history']
    assert [h['sequence'] for h in full_history]==list(range(1,len(full_history)+1))
    assert len([h for h in full_history if h['kind']=='planner_gateway_dispatch'])==3
    assert history[0]['record']['clarification']['question']==proposal()['clarification']['question']
    assert history[1]['record']['review']['protocol_errors']==[{'field':field,'code':'missing_field'} for field in cr.REVIEW_FIELDS]
    assert history[-1]['record']['review']['clarification_supported'] is True
    assert result['diagnostics']['audit_history']==full_history
    assert clarification_records(result['diagnostics']['audit_history'])==history
    assert not result['claims'] and not result['calculations']

@pytest.mark.parametrize('value',[None,'true',1,0,[],{}])
def test_nonboolean_is_received_protocol_error_not_false_or_approval(source,value):
    g=Gateway(plan(),proposal(),{'clarification_supported':value},proposal(),{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True})
    status,result=run(source,g)
    assert status=='needs_clarification' and len(g.calls)==5
    assert result['diagnostics']['tool_errors'][0]['validation_errors'][0]['code']=='expected_boolean'


def test_explicit_false_remains_rejected_without_retry(source):
    events=[];g=Gateway(plan(),proposal(),{'clarification_supported':False,'reason':'候选区分条件没有依据'})
    status,result=run(source,g,audit=events.append)
    assert status=='insufficient_evidence' and len(g.calls)==3 and 'clarification' not in result
    assert result['diagnostics']['rejections'][-1]['stage']=='clarification_review'
    assert records(events)[-1]['record']['review']['reason']=='候选区分条件没有依据'
    assert records(events)[-1]['record']['review']['clarification_supported'] is False


def test_supported_true_keeps_normal_clarification_shape(source):
    g=Gateway(plan(),proposal(),{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True})
    status,result=run(source,g)
    assert status=='needs_clarification' and result['text']==proposal()['clarification']['question']
    assert result['clarification']['options']==['D01 北岸','D02 南岸']
    assert len(g.calls)==3 and result['claims']==[]


def test_repeated_missing_schema_stops_by_existing_no_progress(source):
    g=Gateway(plan(),*[v for _ in range(4) for v in [proposal(),WRONG_SHAPE]])
    status,result=run(source,g)
    assert status=='insufficient_evidence' and result['verification']['reason']=='no_progress'
    assert len(g.calls)==7 and len(result['steps'])==1
    assert len(clarification_records(result['diagnostics']['audit_history']))==6


@pytest.mark.parametrize('code',['timeout','transport_failed','response_json_failed','http_error'])
def test_unknown_or_service_error_is_not_replayed(source,code):
    events=[];g=Gateway(plan(),proposal(),ModelError('controlled unknown/service outcome',code))
    with pytest.raises(ModelError):run(source,g,audit=events.append)
    assert len(g.calls)==3
    assert [h['kind'] for h in records(events)]==['clarification_candidate_received']


def test_unknown_after_bad_review_preserves_first_receipt_and_stops(source):
    events=[];g=Gateway(plan(),proposal(),WRONG_SHAPE,ModelError('unknown new planner POST','timeout'))
    with pytest.raises(ModelError):run(source,g,audit=events.append)
    assert len(g.calls)==4 and len(records(events))==2
    assert records(events)[-1]['record']['review']['protocol_errors']


@pytest.mark.parametrize('cancel_at',[2,3])
def test_cancel_arriving_with_reply_saves_received_projection_no_next_call(source,cancel_at):
    cancelled=[False];events=[]
    def on_call(n):
        if n==cancel_at:cancelled[0]=True
    g=Gateway(plan(),proposal(),WRONG_SHAPE,on_call=on_call)
    with pytest.raises(InterruptedError):run(source,g,audit=events.append,cancelled=lambda:cancelled[0])
    assert len(g.calls)==cancel_at and len(records(events))==cancel_at-1
    assert records(events)[0]['record']['clarification']['options']==proposal()['clarification']['options']


def test_hostile_extra_fields_and_secrets_never_enter_persisted_projection(source):
    p=proposal();p['clarification'].update(question='请选 D01 或 D02；api_key="fake credential tail" /Users/test/private',secret={'claims':[999]})
    review={'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True,'reason':'password="fake hidden reason"','claims':[{'text':'INJECTED_ANSWER'}], 'authorization':'DO_NOT_SAVE'}
    events=[];g=Gateway(plan(),p,review)
    status,result=run(source,g,audit=events.append)
    assert status=='needs_clarification' and result['claims']==[]
    saved=json.dumps(records(events),ensure_ascii=False)
    assert all(s not in saved for s in ['fake credential tail','fake hidden reason','/Users/test','INJECTED_ANSWER','DO_NOT_SAVE'])
    assert records(events)[0]['record']['clarification']['omitted_extra_fields']==1
    assert records(events)[-1]['record']['review']['omitted_extra_fields']==2


def test_overlong_candidate_is_audited_bounded_then_rejected_without_verify(source):
    p=proposal();p['clarification']['question']='x'*100000
    events=[];g=Gateway(plan(),p,p,p)
    status,result=run(source,g,audit=events.append)
    assert status=='insufficient_evidence' and len(g.calls)==4
    assert all(c['purpose']=='table' for c in g.calls)
    assert result['diagnostics']['tool_errors'][0]['validation_errors'][0]['code']=='too_long'
    receipt=records(events)[0]['record'];assert receipt['truncated'] and len(json.dumps(receipt,ensure_ascii=False))<=cr.MAX_RECORD_CHARACTERS
    assert receipt['candidate_identity']['characters']>100000


def test_projection_escape_budget_raw_identity_and_no_mutation():
    candidate={'question':'\x00'*100000,'field':'x'*10000,'options':['\x01'*10000]*30,'extra':{'api_key':'NEVER_SAVED'}}
    review={'clarification_supported':'yes','reason':'\x02'*10000,'claims':[{'text':'NEVER_SAVED'}]}
    original=copy.deepcopy((candidate,review));r=cr.receipt(candidate,round_number=1,stage='test',review=review)
    assert (candidate,review)==original and r['truncated']
    assert len(json.dumps(r,ensure_ascii=False))<=cr.MAX_RECORD_CHARACTERS
    assert 'NEVER_SAVED' not in json.dumps(r)
    assert r['candidate_identity']['sha256']==hashlib.sha256(json.dumps(candidate,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
