import copy
import json
import pytest
from backend.cross_source_engine import _claims, CrossSourceError, answer_cross_source, _query_review_passed
from backend.query_result_context import build_query_result_context
from backend.reasoning_tools import ReasoningLimits
from test_cross_business_repair import source, sql

def binding(record, text):
    context=record['query_result_context'];row=context['rows'][0];cells=row['cells'];cell=cells[2]
    return {'evidence_id':record['id'],'context_id':context['context_id'],'cell_id':cell['cell_id'],
        'row_index':0,'column_index':2,'column_label':cell['column_label'],'value':cell['value'],
        'entity_cells':[cells[0]['cell_id'],cells[1]['cell_id']],'statement':text}

def evidence():
    record={'id':'E1','kind':'table','source_id':'s','sha256':'h','parser_version':'p',
        'query':{'sql':'SELECT period,object,quantity FROM data','parameters':[]},
        'query_result':{'columns':['period','object','quantity'],'rows':[['2025-07-01','全站','2610']],'truncated':False},
        'quote':'2025-07-01 全站 2610'}
    record['query_result_context']=build_query_result_context(record)
    return record

def test_ordinary_query_wrong_native_cell_has_actionable_feedback():
    record=evidence();text='全站用电量2610。'
    with pytest.raises(CrossSourceError,match='查询输出当成原表格') as error:
        _claims({'claims':[{'text':text,'evidence_ids':['E1'],'table_bindings':[{'evidence_id':'E1','cell_id':'R1C3'}]}]}, {'E1':record},{})
    assert error.value.details['query_evidence_ids']==['E1']
    assert 'query_bindings' in error.value.details['repair_guidance']

def test_true_binding_cannot_hide_unbound_reuse_of_true_number():
    record=evidence();text='全站2025-07-01用电量为2610。'
    candidate={'text':text+'另有2610元。','evidence_ids':['E1'],'query_bindings':[binding(record,text)]}
    with pytest.raises(CrossSourceError,match='数值'):
        _claims({'claims':[candidate]},{'E1':record},{})

def test_unknown_original_structure_cannot_use_query_binding_escape():
    record=evidence();text='全站2025-07-01用电量为2610。'
    record['table_context']={'has_table':True,'unresolved_structure':True,'tables':[]}
    with pytest.raises(CrossSourceError):
        _claims({'claims':[{'text':text,'evidence_ids':['E1'],'query_bindings':[binding(record,text)]}]},{'E1':record},{})

@pytest.mark.parametrize('ok',[True,False])
def test_executed_result_requires_new_relation_review_when_typed(source,ok):
    class Gateway:
        def __init__(self):self.n=0;self.payloads=[]
        def __call__(self,messages,role):
            self.n+=1;p=json.loads(messages[-1]['content']);self.payloads.append(p)
            if self.n==1:return sql()
            if self.n==2:
                record=p['evidence'][0];text='全站2025-07-01用电量为2610。'
                return {'status':'completed','claims':[{'text':text,'evidence_ids':['E1'],'query_bindings':[binding(record,text)]}]}
            assert self.n==3 and role=='verify'
            assert p['query_relation_claim_ids']==['C1']
            return {'supported':True,'answer_complete':True,'conflict':False,'claims':[{'id':'C1','supported':True}],
                'query_relations':[{'id':'C1','supported':ok}],
                'coverage':[{'question_id':'Q1','status':'answered','claim_ids':['C1']}]}
    g=Gateway();status,result=answer_cross_source([source],'查全站用电量',[],g,limits=ReasoningLimits(max_no_progress_rounds=1))
    assert (status=='completed') is ok
    if ok:
        q=result['claims'][0]['query_bindings'][0]
        assert q['localization_scope']=='query_output_not_original_cell'
        assert result['verification']['query_relations'][0]['supported'] is True
    else:assert result['claims']==[]

def test_negative_query_binding_review_has_explicit_system_contract(source):
    class Gateway:
        def __init__(self):self.n=0
        def __call__(self,messages,role):
            self.n+=1;p=json.loads(messages[-1]['content'])
            if self.n==1:return sql()
            if self.n==2:
                record=p['evidence'][0];text='全站2025-07-01用电量为2610；该记录不能说明其他时期。'
                return {'status':'insufficient_evidence','reason':'missing_evidence','claims':[{'text':text,'evidence_ids':['E1'],'query_bindings':[binding(record,text)]}]}
            assert role=='verify' and 'query_relation_claim_ids' in messages[0]['content'] and 'query_relations' in messages[0]['content']
            assert p['query_relation_claim_ids']==['C1']
            return {'negative_supported':True,'conflict':False,'claims':[{'id':'C1','supported':True}],
                    'query_relations':[{'id':'C1','supported':True}]}
    g=Gateway();status,result=answer_cross_source([source],'查其他时期用电量',[],g)
    assert status=='insufficient_evidence' and result['verification']['negative_review'] is True

def test_malformed_query_relation_id_never_raises_or_passes():
    assert not _query_review_passed({'query_relations':[{'id':{},'supported':True}]},['C1'])
    assert not _query_review_passed({'query_relations':[{'id':'C1','supported':1}]},['C1'])
