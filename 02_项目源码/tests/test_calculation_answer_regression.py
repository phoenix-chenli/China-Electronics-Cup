"""Actual calculator and publication paths; scripted models are never live scores."""
import copy
import json
from decimal import Decimal

import pytest

from backend import user_scenarios
from backend.answer_contract import public_record
from backend.cross_source_engine import answer_cross_source
from backend.document_engine import answer_document
from backend.formula_engine import FormulaError, calculate_formula
from backend.reasoning_tools import ReasoningLimits


RULE = '金额原始单位为分。100分=1元。'


def source(text=RULE):
    return dict(id='rule', name='口径说明.txt', sha256='a'*64, parser_version='test',
                metadata={}, tables=[], warnings=[], pages=[{'number':1,'text':text}])


def binding(question, index=0):
    item = user_scenarios.build_context(question)['inputs'][index]
    return {key:item['id'] if key == 'user_input_id' else item[key]
            for key in ('user_input_id','quote','start','end','unit')}


@pytest.mark.parametrize('question,expected', [
    ('1000分是多少钱', '10.00'), ('0分是多少钱', '0.00'),
    ('-125分是多少元', '-1.25'), ('2,500分是多少元', '25.00'),
    ('1e3分是多少元', '10.00'), ('10元等于多少分', '1000.00'),
])
def test_literal_user_quantity_has_provenance_and_actual_decimal_result(question, expected):
    target = '分' if question.startswith('10元') else '元'
    result = calculate_formula('a', {'a':binding(question)}, {'E1':{'kind':'text','quote':RULE}},
        target, rule_evidence_ids=['E1'], user_formula=question)
    assert result['display_value'] == expected
    assert result['is_hypothetical'] is False
    parameter = result['parameters'][0]
    assert parameter['provenance'] == 'user_question' and not parameter['is_document_evidence']
    assert parameter['user_input']['source_text'] == question
    assert result['rule_evidence_ids'] == ['E1']
    assert result['unit_handling']['pure_unit_conversion'] is True


@pytest.mark.parametrize('field,value', [('quote','2000分'), ('unit','万元'), ('start',1), ('end',4), ('value',1000)])
def test_literal_quantity_cannot_be_replaced_by_model(field, value):
    question='1000分是多少钱'; item=binding(question); item[field]=value
    with pytest.raises(FormulaError):
        calculate_formula('a', {'a':item}, {'E1':{'kind':'text','quote':RULE}},
            '元', rule_evidence_ids=['E1'], user_formula=question)


@pytest.mark.parametrize('question,expression,expected', [
    ('1000÷100是多少', 'a/b', '10.00'),
    ('12+8是多少', 'a+b', '20.00'),
    ('(12+8)/4是多少', '(a+b)/c', '5.00'),
])
def test_explicit_arithmetic_is_exactly_authorized_by_original_operands(question, expression, expected):
    values=user_scenarios.build_context(question)['inputs']
    bindings={chr(97+i):binding(question,i) for i in range(len(values))}
    result=calculate_formula(expression,bindings,{},'1',user_formula=question)
    assert result['display_value']==expected and result['rule_origin']=='user_question'
    with pytest.raises(FormulaError):
        calculate_formula(expression+'+1',bindings,{},'1',user_formula=question)


@pytest.mark.parametrize('question,expression,unit,expected', [
    ('120元+80元一共多少钱', 'a+b', '元', '200.00'),
    ('1000分+500分等于多少元', 'a+b', '元', '15.00'),
    ('1000分/100是多少钱', 'a/b', '元', '0.10'),
    ('2米+30厘米是多少米', 'a+b', 'm', '2.30'),
])
def test_explicit_quantity_arithmetic_preserves_user_operators_and_real_units(question,expression,unit,expected):
    inputs=user_scenarios.build_context(question)['inputs']
    bindings={chr(97+i):binding(question,i) for i in range(len(inputs))}
    calculation=calculate_formula(expression,bindings,{},unit,user_formula=question)
    assert calculation['display_value']==expected
    assert calculation['rule_origin']=='user_question'
    assert calculation['is_hypothetical'] is False
    # The same input numbers do not authorize an unrelated operator.
    with pytest.raises(FormulaError):
        calculate_formula(expression.replace('+','*') if '+' in expression else 'a*b',bindings,{},unit,user_formula=question)


def test_pure_arithmetic_zero_denominator_is_not_a_numeric_answer():
    question='1000÷0是多少'
    with pytest.raises(FormulaError):
        calculate_formula('a/b',{'a':binding(question),'b':binding(question,1)}, {}, '1',user_formula=question)


def test_pure_arithmetic_result_is_composed_with_its_real_calculation_basis():
    from backend.answer_composer import compose_answer
    question='12+8是多少'
    calculation=calculate_formula('a+b',{'a':binding(question),'b':binding(question,1)}, {}, '1',user_formula=question)
    calculation['id']='K1'
    result={'text':'计算结果为20.00。','claims':[{'id':'C1','text':'计算结果为20.00。','evidence_ids':[],'calculation_ids':['K1']}],
            'evidence':[],'calculations':[calculation],'verification':{'status':'passed'}}
    composed=compose_answer(result,'completed',question,'document')
    assert composed['composition_audit']['status']=='ready'
    assert composed['composition_audit']['checks']['fact_citations_adjacent'] is True
    assert [b['kind'] for b in composed['display_blocks']]==['fact','calculation']
    assert composed['display_blocks'][1]['calculation']['unit_handling']==calculation['unit_handling']


class ConversionGateway:
    """Read rule -> execute calculator -> publish separate rule and result -> review."""
    def __init__(self, question, expected='10.00', *, omit_result=False):
        self.question=question; self.expected=expected; self.omit_result=omit_result
        self.calls=[]; self.plans=0

    def __call__(self,messages,purpose):
        content=messages[-1]['content']
        payload=json.loads(content[0]['text'] if isinstance(content,list) else content)
        self.calls.append((purpose,payload))
        if purpose=='verify':
            claims=payload['candidate_claims']
            return {'supported':True,'answer_complete':True,'conflict':False,
                'claims':[{'id':c['id'],'supported':True} for c in claims],
                'coverage':[{'question_id':p['id'],'status':'answered','claim_ids':[c['id'] for c in claims], 'evidence_ids':['E1']}
                            for p in payload['question_parts']],
                'scenario_relations':[{'id':kid,**{key:True for key in user_scenarios.CHECKS}}
                                      for kid in payload.get('user_scenario_calculation_ids',[])]}
        self.plans+=1
        if self.plans==1:
            return {'actions':[{'id':'read','kind':'search','source_id':'rule','query':'金额单位和分元换算依据','depends_on':[]}]}
        if self.plans==2 and not self.omit_result:
            return {'actions':[{'id':'convert','kind':'calculate','expression':'a','bindings':{'a':binding(self.question)},
                'output_unit':'元','precision':2,'rule_evidence_ids':['E1'],'depends_on':['read']}]}
        claims=[{'text':RULE,'evidence_ids':['E1']}]
        if not self.omit_result:
            claims.append({'text':f'换算结果为{self.expected}元。','calculation_ids':['K1']})
        return {'status':'completed','claims':claims}


def test_real_tool_path_gives_ten_yuan_with_executed_result_and_basis():
    question='1000分是多少钱'; model=ConversionGateway(question)
    status,result=answer_cross_source([source()],question,[],model)
    assert status=='completed' and result['verification']['status']=='passed'
    assert result['calculation_completeness']['status']=='passed'
    assert result['completeness']['complete'] is True
    assert RULE in result['text'] and '10.00元' in result['text']
    assert result['calculations'][0]['parameters'][0]['value']=='1000'
    assert result['calculations'][0]['value']==str(Decimal('10.00'))
    assert len(model.calls)==4


def test_overconfident_reviewer_cannot_publish_rule_only_as_complete():
    question='1000分是多少钱'; model=ConversionGateway(question,omit_result=True)
    status,result=answer_cross_source([source()],question,[],model,
        limits=ReasoningLimits(max_no_progress_rounds=2))
    assert status!='completed'
    assert result['verification']['status']=='limited'
    assert result['completeness']['complete'] is False
    assert result['calculation_completeness']['status']=='incomplete'


def test_quotation_engine_defence_rejects_rule_only_even_if_reviewer_says_answered():
    def model(messages,purpose):
        payload=json.loads(messages[-1]['content'])
        if purpose=='document':
            return {'status':'completed','claims':[{'text':RULE,
                'citations':[{'chunk_id':payload['chunks'][0]['chunk_id'],'quote':RULE}]}]}
        return {'claims':[{'id':c['id'],'supported':True} for c in payload['claims']], 'answer_complete':True,
            'coverage':[{'question_id':'Q1','status':'answered','claim_ids':[c['id'] for c in payload['claims']],
                         'evidence_ids':[e['id'] for e in payload['available_evidence']]}]}
    result=answer_document(source(),'1000分是多少钱',model,lambda:False,lambda stage:None)
    assert result['status']=='insufficient_evidence'
    assert result['verification']['status']=='limited'
    assert result['completeness']['complete'] is False


def test_reading_old_incomplete_record_projects_display_audit_without_rewriting_history():
    item={'question':'1000分是多少钱','mode':'document','status':'completed','sources':[],
          'result':{'text':RULE,'claims':[{'id':'c1','text':RULE,'evidence_ids':['e1']}],
                    'evidence':[{'id':'e1','quote':RULE}], 'verification':{'status':'passed'},
                    'completeness':{'status':'assessed','complete':True,'items':[{'id':'Q1','text':'1000分是多少钱',
                        'status':'answered','claim_ids':['c1'],'evidence_ids':['e1']}]}}}
    original=copy.deepcopy(item); displayed=public_record(item)
    assert displayed['result']['calculation_completeness']['status']=='incomplete'
    assert displayed['result']['calculation_completeness']['projection_only'] is True
    assert displayed['status']=='completed' and displayed['result']['text']==RULE
    assert displayed['result']['completeness']['complete'] is True
    assert item==original
