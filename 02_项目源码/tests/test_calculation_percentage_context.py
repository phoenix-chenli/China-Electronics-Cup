"""Executed calculation rates bind individual percentage occurrences; zero paid APIs."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from backend import user_scenarios as us
from backend.calculation_percentage_context import annotate_calculation_percentages
from backend.cross_source_engine import _claims, CrossSourceError, answer_cross_source
from test_user_scenarios import binding

FIXTURE=Path(__file__).parent/'fixtures/percentage_formula_actual_a10.json'

def fixture():return json.loads(FIXTURE.read_text())
def check(data):return _claims({'claims':[data['claim']]},data['evidence'],data['calculations'],question=data['question'])
def annotate(data,ids=None):return annotate_calculation_percentages(data['claim']['text'],ids or data['claim']['calculation_ids'],data['calculations'])

def test_exact_actual_a10_unchanged_is_mechanically_supported_but_not_semantically_approved():
    d=fixture();before=copy.deepcopy(d);out=check(d)
    assert d==before and out[0]['text']==d['claim']['text']
    ann=[a for a in out[0]['context_annotations'] if a['kind']=='calculation_user_percentage']
    assert len(ann)==1 and ann[0]['start']==71 and ann[0]['end']==74
    a=ann[0];assert d['claim']['text'][a['start']:a['end']]=='10%'
    assert a['normalized_ratio']=='0.1' and a['calculation_ids']==['K3']
    assert a['source_quote']=='10%' and a['source_start']==21 and a['source_end']==24
    assert not a['is_document_evidence'] and a['semantic_status']=='pending_independent_scenario_and_claim_review'
    assert not us.review_passed({},us.required_calculations(out,d['calculations']))

@pytest.mark.parametrize('literal',['10.0%','10.00％','10 %'])
def test_decimal_equivalent_percentage_preserves_exact_occurrence(literal):
    d=fixture();d['claim']['text']=d['claim']['text'].replace('(1-10%)',f'(1-{literal})')
    out=check(d);assert out[0]['text']==d['claim']['text']
    a=next(a for a in out[0]['context_annotations'] if a['kind']=='calculation_user_percentage')
    assert a['label']==literal

@pytest.mark.parametrize('old,new',[
    ('(1-10%)','(1-20%)'),('(1-10%)','(1+10%)'),('(1-10%)','(1-10)'),
    ('(1-10%)','(1-10%)/米'),('(1-10%)','(1-10%)每米'),
    ('假设情景','不是假设情景'),('假设情景','实际登记结果'),
])
def test_wrong_rate_direction_naked_number_units_or_actuals_not_annotated(old,new):
    d=fixture();d['claim']['text']=d['claim']['text'].replace(old,new)
    assert annotate(d)[1]==[]
    with pytest.raises(CrossSourceError):check(d)

def test_unrelated_number_is_never_globally_allowed():
    d=fixture();d['claim']['text']+='手续费为10元。'
    with pytest.raises(CrossSourceError):check(d)

@pytest.mark.parametrize('change',[
    lambda k:k.update(id='fake'),lambda k:k.update(rule_origin='document'),
    lambda k:k.update(expression='a*(1+r)'),lambda k:k.update(expression='a*(1-r)+1'),
    lambda k:k['parameters'][1].update(value='20'),
    lambda k:k['parameters'][1].update(provenance='document'),
    lambda k:k['parameters'][1].update(user_input='bad'),
    lambda k:k['parameters'][1]['user_input'].update(source_sha256='fake'),
    lambda k:k['parameters'][1]['user_input'].update(source_answer_id='fake'),
    lambda k:k['parameters'][1]['user_input'].update(quote='20%'),
    lambda k:k['parameters'][1]['user_input'].update(start=0),
    lambda k:k['parameters'][1]['binding'].update(user_input_id='fake'),
    lambda k:k['unit_handling']['parameter_base_values'][1].update(base_value='0.2'),
])
def test_tampered_or_non_authorized_calculation_stays_unbound(change):
    d=fixture();change(d['calculations']['K3']);assert annotate(d)[1]==[]

def test_rehashed_but_unrequested_assumption_is_not_authorization():
    d=fixture();item=d['calculations']['K3']['parameters'][1]['user_input']
    item['source_text']=item['source_text'].replace('仅假设','不假设')
    item['source_sha256']=hashlib.sha256(item['source_text'].encode()).hexdigest()
    assert annotate(d)[1]==[]

def test_cited_dependency_is_eligible_but_uncited_fake_and_unrelated_k_are_not():
    d=fixture();assert annotate(d,['K5'])[1][0]['calculation_ids']==['K3']
    for ids in [['K1'],['K2'],['K-fake']]:assert annotate(d,ids)[1]==[]
    d['claim']['calculation_ids']=['K-fake']
    with pytest.raises(CrossSourceError):check(d)

@pytest.mark.parametrize('rejected_dimension',[None,*us.CHECKS])
def test_actual_engine_still_requires_all_four_independent_scenario_checks(rejected_dimension):
    source={'id':'s','name':'说明.txt','sha256':'fixture','parser_version':'test','tables':[],'warnings':[],
            'metadata':{},'pages':[{'number':1,'text':'实际登记金额10000分。人民币100分=1元。'}]}
    calls=[]
    def gateway(messages,purpose):
        p=json.loads(messages[-1]['content']);calls.append((purpose,p))
        if purpose=='verify':
            rows=[{'id':v,**dict.fromkeys(us.CHECKS,True)} for v in p['user_scenario_calculation_ids']]
            if rejected_dimension:rows[0][rejected_dimension]=False
            return {'supported':True,'answer_complete':True,'claims':[{'id':'C1','supported':True}],'conflict':False,
                'coverage':[{'question_id':v['id'],'status':'answered','claim_ids':['C1']} for v in p['question_parts']],
                'scenario_relations':rows}
        if not p.get('evidence'):
            return {'actions':[{'id':'S1','kind':'search','source_id':'s','query':'登记金额','depends_on':[]}]}
        if not p.get('calculations'):
            e=next(e for e in p['evidence'] if '10000分' in e['quote'])
            return {'actions':[{'id':'S2','kind':'calculate','expression':'a*(1-r)',
                'bindings':{'a':{'evidence_id':e['id'],'quote':'10000分','unit':'分'},'r':binding(p['user_assumptions'])},
                'output_unit':'元','operation':{'kind':'percentage_adjust','base':'a','rate':'r','direction':'decrease'},'depends_on':['S1']}]}
        return {'status':'completed','claims':[{'text':'该假设下按原金额乘以(1-10%)计算，金额为90.00元。','calculation_ids':['K1']}]}
    status,result=answer_cross_source([source],'假设登记金额减少10%，求金额。',[],gateway,actual_user_question='假设登记金额减少10%，求金额。')
    assert any(purpose=='verify' for purpose,_ in calls)
    if rejected_dimension:
        assert status!='completed' and not result.get('claims')
    else:
        assert status=='completed' and result['claims'][0]['text'].endswith('金额为90.00元。')
