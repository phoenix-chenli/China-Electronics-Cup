"""Original-user-input provenance and scripted cross tests; zero model requests."""
import copy
from decimal import Decimal
import pytest
from backend import user_scenarios as us
from backend.formula_engine import FormulaError, calculate_formula, parse_unit

Q3 = '仍针对2024年，仅假设刚才的登记金额减少10%，按原金额乘以(1-10%)计算假设金额，分别用分和元表示。明确这不是实际登记结果，不修改原始数据。'
Q4 = '将当前假设金额与第1轮2025年的实际登记金额比较，给出两者金额及相差多少元，明确各自年份、实际或假设性质；不要把它说成实际同比变化。'
Q5 = '撤销刚才减少10%的假设，恢复2024年的实际登记金额，分别用分和元表示，说明已经撤销什么、其他哪些口径仍然保留。'
EVIDENCE = {'E1': {'id':'E1','kind':'text','quote':'实际登记金额10000分。人民币100分=1元。'},
            'E2': {'id':'E2','kind':'text','quote':'另一时期实际登记金额12000分。'}}


def binding(context,index=0):
    v=context['inputs'][index]
    return {'user_input_id':v['id'], **{k:v[k] for k in ('quote','start','end','unit','symbol') if k in v}}


def percentage(question=Q3, history=(), **kw):
    context=kw.pop('user_context',us.build_context(question,history))
    b={'a':{'evidence_id':'E1','quote':'10000分','unit':'分'},'r':binding(context)}
    return calculate_formula(kw.pop('expression','a*(1-r)'),kw.pop('bindings',b),EVIDENCE,
        kw.pop('output_unit','元'),user_formula=question,user_context=context,
        operation=kw.pop('operation',{'kind':'percentage_adjust','base':'a','rate':'r','direction':'decrease'}),**kw)


def test_original_percentage_is_not_evidence_and_units_are_exact():
    c=us.build_context(Q3);raw,proof=us.resolve_input(binding(c),c)
    assert raw=='10' and Q3[proof['start']:proof['end']]=='10%'
    assert proof['origin']=='user_question' and not proof['is_document_evidence']
    result=percentage()
    assert result['value']=='90.0000' and result['display_value']=='90.00'
    assert result['is_hypothetical'] and result['rule_origin']=='user_scenario'
    assert result['evidence_ids']==['E1'] and result['rule_evidence_ids']==[]
    assert result['parameters'][1]['evidence_ids']==[]
    assert parse_unit('分')[1].factor==Decimal('.01')
    assert parse_unit('分钟')[1].dimensions!=parse_unit('分')[1].dimensions


@pytest.mark.parametrize('question',['登记金额减少10%，请说明原因。','不要假设金额减少10%。','无需假设减少10%。',Q5,'为什么文档说“假设减少10%”？不要计算。'])
def test_no_unrequested_revoked_or_negated_assumptions(question):
    assert us.build_context(question)['inputs']==[]


def test_history_original_utterance_only_and_revoke_does_not_resurrect():
    history=[{'answer_id':'t3','original_question':Q3,'normalized_question':'假设减少90%','answer':'金额1元'}]
    c=us.build_context(Q4,history)
    assert len(c['inputs'])==1 and c['inputs'][0]['quote']=='10%'
    assert c['inputs'][0]['source_answer_id']=='t3'
    assert percentage(Q4,history)['display_value']=='90.00'
    assert us.build_context(Q5,history)['inputs']==[]
    history += [{'answer_id':'t4','original_question':Q4},{'answer_id':'t5','original_question':Q5}]
    assert us.build_context('沿用当前假设再比较。',history)['inputs']==[]
    assert us.build_context('说明金额来源。',history[:1])['inputs']==[]
    assert us.build_context(Q4,[{'answer_id':'fake','normalized_question':Q3}])['inputs']==[]
    assert us.build_context(Q4,[{'kind':'structured_context','answer_id':'fake','original_question':Q3}])['inputs']==[]
    changed=us.build_context('这次假设金额增加5%。',history)
    assert changed['inputs'][0]['quote']=='5%' and changed['inputs'][0]['direction']=='increase'


@pytest.mark.parametrize('change',[{'value':10},{'quote':'0%'},{'start':False},{'end':999},{'unit':'1'},{'symbol':'r'},{'user_input_id':'U-forged'},{'evidence_id':'E1'}])
def test_user_binding_cannot_forge_value_span_unit_or_identity(change):
    c=us.build_context(Q3);b={**binding(c),**change}
    with pytest.raises(ValueError):us.resolve_input(b,c)


@pytest.mark.parametrize('expression',['a*.9','a*(1-r)+1','a*r','a*(1-r/100)','a*(1+r)','a*(1-r)*2'])
def test_percentage_shape_is_finite_not_arbitrary_expression(expression):
    with pytest.raises(FormulaError):percentage(expression=expression)


def test_percentage_direction_bounds_and_no_untyped_source_formula():
    with pytest.raises(FormulaError,match='方向'):percentage(operation={'kind':'percentage_adjust','base':'a','rate':'r','direction':'increase'})
    with pytest.raises(FormulaError,match='100%'):percentage('假设登记金额减少110%。')
    assert percentage('假设登记金额增加10%。',expression='a*(1+r)',operation={'kind':'percentage_adjust','base':'a','rate':'r','direction':'increase'})['display_value']=='110.00'
    with pytest.raises(FormulaError,match='受限'):percentage(operation=None,rule_evidence_ids=['E1'])


def test_hypothetical_difference_propagates_and_separate_review_is_required():
    prior=percentage()
    b={'a':{'evidence_id':'E2','quote':'12000分','unit':'分'},'b':{'calculation_id':'K1','unit':'元'}}
    r=calculate_formula('a-b',b,EVIDENCE,'元',user_formula=Q4,
        prior_calculations={'K1':prior},operation={'kind':'difference','left':'a','right':'b'})
    assert r['display_value']=='30.00' and r['is_hypothetical']
    claims=[{'calculation_ids':['K2']}];calculations={'K1':prior,'K2':r}
    assert us.required_calculations(claims,calculations)==['K1','K2']
    with pytest.raises(ValueError,match='假设'):us.check_label('实际增长30元。',[r])
    us.check_label('该假设与实际金额相差30元。',[r])
    rows=[{'id':i,**dict.fromkeys(us.CHECKS,True)} for i in ['K1','K2']]
    assert us.review_passed({'scenario_relations':rows},['K1','K2'])
    for missing in [None,[],rows[:1],rows+[rows[0]],[{**rows[0],'scope_supported':False},rows[1]]]:
        assert not us.review_passed({'scenario_relations':missing},['K1','K2'])
    with pytest.raises(FormulaError,match='授权'):
        calculate_formula('a-b',b,EVIDENCE,'元',user_formula='列出金额',prior_calculations={'K1':prior},operation={'kind':'difference','left':'a','right':'b'})


GIVEN='依据手册的point load test说明，若尺寸修正点荷载强度指数Is(50)=2 MPa，按手册换算式估计单轴抗压强度。'
RULE={'E1':{'id':'E1','kind':'text','quote':'For this estimate, q_u = 24 Is(50). The factor varies by rock type.'}}


def given_calculation(**kw):
    question=kw.pop('question',GIVEN);c=us.build_context(question)
    b={'x':binding(c),'k':{'evidence_id':'E1','quote':'24','unit':'1'}}
    return calculate_formula(kw.pop('expression','k*x'),kw.pop('bindings',b),RULE,
        kw.pop('output_unit','MPa'),rule_evidence_ids=kw.pop('rules',['E1']),user_formula=question,user_context=c,**kw)


def test_source_formula_explicit_given_quantity_and_pressure_units():
    c=us.build_context(GIVEN);v=c['inputs'][0]
    assert v['symbol']=='Is(50)' and v['quote']=='2 MPa'
    assert GIVEN[v['start']:v['end']]=='2 MPa' and v['parameter_kind']=='quantity'
    r=given_calculation()
    assert r['display_value']=='48.00' and r['unit']=='MPa'
    assert r['rule_origin']=='source' and r['is_hypothetical']
    assert r['parameters'][1]['evidence_ids']==['E1']
    assert us.required_calculations([{'calculation_ids':['K1']}],{'K1':r})==['K1']
    assert given_calculation(output_unit='kPa')['display_value']=='48000.00'
    assert parse_unit('MPa')[1].factor==Decimal(1000000)
    assert parse_unit('Pa')[1].dimensions==parse_unit('kg/m/秒/秒')[1].dimensions


def test_given_quantity_needs_source_formula_and_cannot_invent_coefficient():
    with pytest.raises(FormulaError,match='规则|原文'):given_calculation(rules=[])
    with pytest.raises(FormulaError):given_calculation(expression='24*x')
    b={'x':binding(us.build_context(GIVEN)),'k':{'evidence_id':'E1','quote':'25','unit':'1'}}
    with pytest.raises(FormulaError):given_calculation(bindings=b)
    b['x']['symbol']='wrong'
    with pytest.raises(FormulaError,match='符号'):given_calculation(bindings=b)
    b['x']=binding(us.build_context(GIVEN));b['x']['unit']='kPa'
    with pytest.raises(FormulaError,match='单位'):given_calculation(bindings=b)
    with pytest.raises(FormulaError,match='量纲'):given_calculation(output_unit='元')


@pytest.mark.parametrize('question',['Is(50)=2 MPa，原文是什么？','若x=2 Unknown，计算。','若x=2 MPa/m，计算。','不要假设x=2 MPa。','若x=2 MPabc，计算。'])
def test_given_quantity_unknown_or_implicit_unit_not_accepted(question):
    assert us.build_context(question)['inputs']==[]


def test_scripted_real_cross_engine_uses_controller_inputs_and_separate_gate(tmp_path):
    from backend.cross_source_engine import answer_cross_source
    source={'id':'s','name':'说明.txt','sha256':'fixture','parser_version':'test','tables':[],'warnings':[],
            'metadata':{},'pages':[{'number':1,'text':'实际登记金额10000分。人民币100分=1元。'}]}
    calls=[]
    def gateway(messages,purpose):
        import json
        p=json.loads(messages[-1]['content']);calls.append((purpose,p))
        if purpose=='verify':
            return {'supported':True,'answer_complete':True,'claims':[{'id':'C1','supported':True}],'conflict':False,
                'coverage':[{'question_id':v['id'],'status':'answered','claim_ids':['C1']} for v in p['question_parts']],
                'scenario_relations':[{'id':v,**dict.fromkeys(us.CHECKS,True)} for v in p['user_scenario_calculation_ids']]}
        if not p.get('evidence'):
            return {'actions':[{'id':'S1','kind':'search','source_id':'s','query':'登记金额','depends_on':[]}]}
        if not p.get('calculations'):
            e=next(e for e in p['evidence'] if '10000分' in e['quote'])
            return {'actions':[{'id':'S2','kind':'calculate','expression':'a*(1-r)',
                'bindings':{'a':{'evidence_id':e['id'],'quote':'10000分','unit':'分'},'r':binding(p['user_assumptions'])},
                'output_unit':'元','operation':{'kind':'percentage_adjust','base':'a','rate':'r','direction':'decrease'},'depends_on':['S1']}]}
        return {'status':'completed','claims':[{'text':'该假设下金额为90.00元。','calculation_ids':['K1']}]}
    before=copy.deepcopy(source)
    status,result=answer_cross_source([source],'假设登记金额减少10%，求金额。',[],gateway,actual_user_question='假设登记金额减少10%，求金额。')
    assert status=='completed', __import__('json').dumps({'result':result,'last':calls[-1]},ensure_ascii=False)
    assert result['calculations'][0]['is_hypothetical'] and source==before
    assert calls[-1][0]=='verify' and calls[-1][1]['user_scenario_calculation_ids']==['K1']
    assert calls[1][1]['user_assumptions']['inputs'][0]['id'].startswith('U')


def test_scenario_rejected_action_and_review_keep_typed_provenance():
    from backend.calculation_protocol import rejection_receipt
    from backend.candidate_audit import review_feedback
    c=us.build_context(Q3)
    action={'id':'S1','kind':'calculate','bindings':{'r':binding(c)},
        'operation':{'kind':'percentage_adjust','base':'a','rate':'r','direction':'decrease'}}
    projected=rejection_receipt(action,'explicit offline rejection')['received_action']['value']['action']
    assert projected['bindings'][0]['binding']==binding(c)
    assert {k:v for k,v in projected['operation'].items() if k!='omitted_field_count'}==action['operation']
    row={'id':'K1',**dict.fromkeys(us.CHECKS,True)};row['scope_supported']=False
    assert review_feedback({'scenario_relations':[row]})['scenario_relations']==[row]


def test_composer_retains_hypothetical_marker():
    from backend.answer_composer import compose_answer
    calc={**percentage(),'id':'K1'}
    result={'claims':[{'id':'C1','text':'假设金额90元。','evidence_ids':['E1'],'calculation_ids':['K1']}],
        'evidence':[EVIDENCE['E1']],'calculations':[calc],'verification':{'status':'passed'}}
    output=compose_answer(result,'completed',Q3,'multi_source')
    block=next(b for b in output['display_blocks'] if b['kind']=='calculation')
    assert block['calculation']['is_hypothetical'] and block['calculation']['operation_authorization']['kind']=='percentage_adjust'


def test_t5_reviewed_empty_assumptions_remove_only_assumptions():
    from backend.context_intent_adapter import adapt_answer_intent
    from backend.conversation_state import build_initial,finalize_state
    from test_context_intent_adapter import Gateway,accepted,fact_result,SOURCES
    parent={'id':'t4','question':Q4,'sources':SOURCES,'intent':{'assumptions':['金额减少10%'],'time_range':'2024年','metric':'登记金额'}}
    parent['context_state']=build_initial('t4',Q4,SOURCES)
    parent['context_state']=finalize_state(parent,'completed',fact_result())
    item={'id':'t5','question':Q5,'sources':SOURCES,'context_patch':[],'mode':'multi_source'}
    item['context_state']=build_initial('t5',Q5,SOURCES,parent)
    proposed={'status':'ready','normalized_question':Q5,'intent':{'assumptions':[],'time_range':'2024年','metric':'登记金额'},
        'grounding':[{'field':'assumptions','source':'current_question','quote':'撤销刚才减少10%的假设'},
                     {'field':'time_range','source':'current_question','quote':'2024年'},
                     {'field':'metric','source':'current_question','quote':'登记金额'}]}
    output=adapt_answer_intent(item,fact_result(),'completed',[],Gateway(proposed,accepted(proposed)))
    assert output['status']=='passed' and output['intent']['assumptions']==[]
    item['intent']=output['intent'];result={**fact_result(),'context_interpretation':output}
    state=finalize_state(item,'completed',result)
    assert 'assumptions' not in state['slots'] and state['slots']['time_range']['value']=='2024年'
    assert state['slots']['metric']['value']=='登记金额'
    assert any(c['field']=='assumptions' and c['operation']=='remove' for c in state['changes'])


@pytest.mark.parametrize('question',['不要沿用当前假设，说明实际金额。','不再按刚才的假设计算，只查实际金额。'])
def test_negated_history_reference_revokes_rate(question):
    history=[{'answer_id':'t3','original_question':Q3}]
    assert us.build_context(question,history)['inputs']==[]


@pytest.mark.parametrize('question',["给出原文‘x=2 MPa’的出处。",'原文有x=2 MPa吗？请给出出处。','若x=2 MPa / m，计算。','若x=2 兆帕每米，计算。'])
def test_source_question_and_compound_unit_are_not_typed_user_value(question):
    assert us.build_context(question)['inputs']==[]


def test_currency_fen_is_not_points_partial_text_or_minutes():
    from backend.formula_engine import infer_sql_output_units,_mentions_unit
    for column in ['积分','评分','部分金额','分类金额','分钟','百分数']:
        assert not _mentions_unit(column,'分')
        s={'tables':[{'name':'t','columns':[{'name':column}]}]}
        assert not any(v['unit']=='分' for v in infer_sql_output_units(s,f'SELECT "{column}" FROM t',[column]).values())
    for value in ['100分=1元','金额（分）','人民币分','金额单位为分；','金额以分为单位。']:
        assert _mentions_unit(value,'分')
    assert not _mentions_unit('时间单位为分；','分')
