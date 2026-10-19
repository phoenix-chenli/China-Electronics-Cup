"""Typed visual and cross-source contracts; gateways are explicit scripted fakes."""
import base64,copy,hashlib,io,json
import pytest
from PIL import Image
from backend import reasoning_tools as tools
from backend.cross_source_engine import answer_cross_source,_claims,CrossSourceError
from backend.formula_engine import calculate_formula,evaluate_arithmetic,FormulaError
from backend.model_gateway import ModelError
from backend.retrieval import RetrievalService
from backend.formula_structure import computation_candidate,latex
from test_cross_source_engine import Responses,approved,actions


def image_source(**updates):
    out={'id':'img','name':'受控图.png','format':'png','sha256':'a'*64,'parser_version':'test',
         'metadata':{'page_unit':'physical_page'},'pages':[{'number':1,'text':'图1 2024年甲收入'}],
         'tables':[],'warnings':[],'status':'ready','archived':False}
    out.update(updates);return out


@pytest.fixture
def pixels(monkeypatch):
    buffer=io.BytesIO();Image.new('RGB',(120,100),'white').save(buffer,format='JPEG');data=buffer.getvalue()
    parts=[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(data).decode()}}]
    monkeypatch.setattr('backend.visual_context.page_content',lambda *args,**kwargs:copy.deepcopy(parts))
    monkeypatch.setattr('backend.chart_reading._image_parts',lambda *args:(copy.deepcopy(parts),hashlib.sha256(data).hexdigest(),(120,100)))
    monkeypatch.setattr('backend.formula_vision.page_content',lambda *args,**kwargs:copy.deepcopy(parts))
    return parts


def printed(value='10',period='2024年',unit='万元'):
    return {'id':'V1','type':'printed_value','text':f'{period}甲收入为{value}{unit}。',
            'value':value,'unit':unit,'category':'甲','series':'收入','period':period,'bbox':[0,0,1,1]}


def chart_action(identifier='V',**updates):
    return {'id':identifier,'kind':'chart_read','source_id':'img','page':1,'query':'2024年甲收入是多少？','depends_on':[],**updates}


def fact(item=None,eid='E1'):
    item=item or printed()
    return {'text':item['text'],'evidence_ids':[eid],
            'visual_bindings':[{'evidence_id':eid,'item_id':item['id'],'statement':item['text']}]}


def visual_evidence(item=None):
    source=image_source();item=copy.deepcopy(item or printed())
    item.update(source_id='img',sha256=source['sha256'],parser_version='test',page=1,bbox=[0,0,1,1],
        localization_scope='original_page',provenance='separate_visual_read_and_review',review={'supported':True})
    return {'id':'E1','kind':'chart_observation','source_id':'img','source_name':source['name'],
            'sha256':source['sha256'],'parser_version':'test','page':1,'quote':item['text'],
            'visual_item':item,'provenance':item['provenance'],'bbox':[0,0,1,1]}


@pytest.mark.parametrize('history',[[],[{'question':'2023年甲收入？','status':'completed','result':{'text':'旧轮只作条件参考，不作本轮证据。'}}]])
def test_chart_read_and_followup_have_separate_read_review_and_final_review(pixels,history):
    source=image_source();before=copy.deepcopy(source);item=printed()
    gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        {'status':'completed','claims':[fact(item)]},approved(visual_supported=True,chart_relations=[{'id':'C1','supported':True}]))
    status,result=answer_cross_source([source],'2024年甲收入是多少？',history,gateway)
    assert status=='completed' and source==before
    assert [c['purpose'] for c in gateway.calls]==['table','document','verify','table','verify']
    record=result['evidence'][0]
    assert record['kind']=='chart_observation' and record['quote_origin']=='independently_reviewed_visual_transcription'
    assert record['bbox']==[0,0,1,1] and record['localization_scope']=='original_page'
    assert result['claims'][0]['visual_bindings'][0]['item_id']=='V1'
    assert result['visual_tool_audit'][0]['reading']['review']['supported_ids']==['V1']


def test_chart_and_document_chain_keeps_two_real_sources(pixels):
    rule={'id':'rule','name':'规则.txt','sha256':'b'*64,'parser_version':'test','pages':[{'number':1,'text':'甲属于试点组。'}],
          'metadata':{},'warnings':[],'tables':[]}
    item=printed();gateway=Responses(actions(chart_action(),{'id':'R','kind':'search','source_id':'rule','query':'甲属于哪组','depends_on':[]}),
        {'items':[item]},{'items':[{'id':'V1','supported':True}]},
        {'status':'completed','claims':[fact(item),{'text':'甲属于试点组。','evidence_ids':['E2']}]},
        {'supported':True,'answer_complete':True,'conflict':False,'visual_supported':True,
         'claims':[{'id':'C1','supported':True},{'id':'C2','supported':True}],
         'chart_relations':[{'id':'C1','supported':True}],
         'coverage':[{'question_id':'Q1','status':'answered','claim_ids':['C1','C2']}]})
    status,result=answer_cross_source([image_source(),rule],'说明甲收入及所属组。',[],gateway)
    assert status=='completed' and {r['source_id'] for r in result['evidence']}=={'img','rule'}
    assert len(result['source_groups'])==2


def test_multi_page_chart_cannot_guess_page_without_locating_search(pixels):
    source=image_source(pages=[{'number':1,'text':'概况'},{'number':2,'text':'图1甲收入'}]);plan=actions(chart_action(page=2))
    gateway=Responses(plan,plan,plan)
    status,result=answer_cross_source([source],'甲收入是多少？',[],gateway)
    assert status!='completed' and all(c['purpose']=='table' for c in gateway.calls) and not result['evidence']
    assert '先检索定位' in result['diagnostics']['tool_errors'][0]['message']


def test_cross_search_uses_persisted_scoped_retriever(tmp_path):
    source={'id':'d','project_id':'p','name':'材料.txt','sha256':'c'*64,'parser_version':'test','metadata':{},'warnings':[],
            'pages':[{'number':1,'text':'甲属于试点组。'}],'tables':[]}
    service=RetrievalService(tmp_path,None,default_mode='lexical');prepared=[]
    def factory(s):
        value=service.prepare(s,'p');prepared.append(value);return value
    gateway=Responses(actions({'id':'S','kind':'search','source_id':'d','query':'甲属于哪组','depends_on':[]}),
        {'status':'completed','claims':[{'text':'甲属于试点组。','evidence_ids':['E1']}]},approved())
    status,result=answer_cross_source([source],'甲属于哪组？',[],gateway,retrieval_factory=factory)
    assert status=='completed' and len(prepared)==1
    assert result['retrieval_audit']['d']['searches'][0]['scope']['source_ids']==['d']
    e=result['evidence'][0];assert source['pages'][0]['text'][e['start']:e['end']]==e['quote']


def test_same_visual_request_new_step_id_reuses_without_network(pixels):
    item=printed();gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        actions(chart_action(identifier='Again')),{'status':'completed','claims':[fact(item)]},
        approved(visual_supported=True,chart_relations=[{'id':'C1','supported':True}]))
    status,result=answer_cross_source([image_source()],'2024年甲收入是多少？',[],gateway)
    assert status=='completed' and len(result['evidence'])==1 and len(result['visual_tool_audit'])==1
    assert [s['evidence_ids'] for s in result['steps']]==[['E1'],['E1']]
    assert sum(c['purpose']=='document' for c in gateway.calls)==1


def test_unknown_visual_post_propagates_without_planner_retry(pixels):
    gateway=Responses(actions(chart_action()));calls=[]
    def call(messages,purpose):
        calls.append(purpose)
        if purpose=='document':raise ModelError('unknown post','transport_failed')
        return gateway(messages,purpose)
    with pytest.raises(ModelError):answer_cross_source([image_source()],'甲收入？',[],call)
    assert calls==['table','document']


@pytest.mark.parametrize('mutation',['wrong_item','missing_statement','new_number','unreviewed','wrong_version','no_binding'])
def test_visual_binding_rejects_wrong_or_unverified_source(mutation):
    record=visual_evidence();candidate=fact()
    if mutation=='wrong_item':candidate['visual_bindings'][0]['item_id']='V2'
    if mutation=='missing_statement':candidate['visual_bindings'][0]['statement']='不在正文的句子'
    if mutation=='new_number':
        candidate['text']=candidate['text'].replace('10','11');candidate['visual_bindings'][0]['statement']=candidate['text']
    if mutation=='unreviewed':record['visual_item']['review']['supported']=False
    if mutation=='wrong_version':record['parser_version']='changed'
    if mutation=='no_binding':candidate.pop('visual_bindings')
    with pytest.raises(CrossSourceError):_claims({'claims':[candidate]},{'E1':record},{},sources={'img':image_source()})


@pytest.mark.parametrize('kind',['visual_relation','missing_precision'])
def test_reference_numbers_never_become_exact_measurement_parameter(kind):
    item={'id':'V1','type':kind,'text':'甲位于参考线上方。','bbox':[0,0,1,1],
          'reference_labels':[{'value':'10','text':'10万元参考线','unit':'万元','bbox':[0,0,1,1]}]}
    with pytest.raises(FormulaError):calculate_formula('a',{'a':{'evidence_id':'E1','visual_item_id':'V1','unit':'万元'}},
        {'E1':visual_evidence(item)},'万元',user_formula='a')


@pytest.mark.parametrize('value,unit,output,expected',[('10','万元','元','100000'),('-7.2%','%','1','-0.072'),('5','kWh','kWh','5')])
def test_only_bound_printed_values_enter_decimal_calculator(value,unit,output,expected):
    result=calculate_formula('a',{'a':{'evidence_id':'E1','visual_item_id':'V1','unit':unit}},
        {'E1':visual_evidence(printed(value=value,unit=unit))},output,user_formula='a')
    assert result['value']==expected and result['parameters'][0]['provenance']=='separate_visual_read_and_review'


@pytest.mark.parametrize('bad',[{'visual_item_id':'wrong'},{'unit':'元'},{'value':'99'},{'row':0,'column':'value','visual_item_id':None}])
def test_visual_calculation_rejects_id_unit_or_injected_value(bad):
    with pytest.raises(FormulaError):calculate_formula('a',{'a':{'evidence_id':'E1','visual_item_id':'V1','unit':'万元',**bad}},
        {'E1':visual_evidence()},'万元',user_formula='a')


def formula_record():
    tree={'type':'row','children':[{'type':'text','text':'R='},{'type':'fraction','layout':'bar',
         'numerator':{'type':'text','text':'a+b'},'denominator':{'type':'text','text':'c'}}]}
    base={'source_id':'formula','sha256':'f'*64,'parser_version':'test','page':1}
    formula={'id':'F1','tree':tree,'latex':latex(tree),'computation_ast':computation_candidate(tree),
             'verified':True,'review':{'supported':True},'source_position':base,'execution_eligible':False}
    return {'id':'F','kind':'formula_structure',**base,'quote':formula['latex'],'formula':formula,
            'provenance':'separate_formula_visual_read_and_review'}


def parameter_record(identifier,symbol,value,unit):
    base={'source_id':'formula','sha256':'f'*64,'parser_version':'test','page':1}
    return {'id':identifier,'kind':'formula_parameter',**base,'formula_id':'F1','parameter_id':identifier,
        'parameter':{'id':identifier,'symbol':symbol,'value':value,'unit':unit,'verified':True,'source_position':copy.deepcopy(base)},
        'formula_verified':True,'provenance':'separate_formula_visual_read_and_review'}


@pytest.mark.parametrize('expression,expected',[('(a+b)/c','5'),('(a-b)/c',None),('a+b/c',None),('(a+b)*c',None)])
def test_visual_formula_must_match_actual_operator_tree(expression,expected):
    records={'F':formula_record(),'A':parameter_record('A','a','12','kWh'),'B':parameter_record('B','b','8','kWh'),
             'D':parameter_record('D','c','4','h')}
    bindings={name:{'evidence_id':eid,'formula_id':'F1','parameter_id':eid,'unit':unit} for name,eid,unit in [('a','A','kWh'),('b','B','kWh'),('c','D','h')]}
    if expected is None:
        with pytest.raises(FormulaError):calculate_formula(expression,bindings,records,'kW',rule_evidence_ids=['F'])
    else:
        result=calculate_formula(expression,bindings,records,'kW',rule_evidence_ids=['F'])
        assert result['value']==expected and result['visual_rule_structure_checked'] is True
        assert records['F']['formula']['execution_eligible'] is False


@pytest.mark.parametrize('mutation',['unverified','unknown_unit','wrong_parameter','conflict','stale_page'])
def test_formula_parameter_failures_are_not_silently_filled(mutation):
    record=parameter_record('A','a','12','kWh');binding={'evidence_id':'A','formula_id':'F1','parameter_id':'A','unit':'kWh'}
    if mutation=='unverified':record['parameter']['verified']=False
    if mutation=='unknown_unit':record['parameter']['unit']=None
    if mutation=='wrong_parameter':binding['parameter_id']='B'
    if mutation=='conflict':record['formula_parameter_conflicts']=['a']
    if mutation=='stale_page':record['parameter']['source_position']['page']=2
    with pytest.raises(FormulaError):calculate_formula('a',{'a':binding},{'A':record},'kWh',user_formula='a')


def test_arithmetic_only_is_immutable_and_never_claims_source_validation():
    parameters={'a':{'value':'12','unit':'kWh'},'b':{'value':'4','unit':'h'}};before=copy.deepcopy(parameters)
    result=evaluate_arithmetic('a/b',parameters,'kW')
    from decimal import Decimal
    assert Decimal(result.pop('value'))==Decimal('3')
    assert result=={'display_value':'3.00','unit':'kW','precision':2,'origin':'arithmetic_only'}
    assert parameters==before and 'evidence_ids' not in result and 'checks' not in result


@pytest.mark.parametrize('expression,parameters,unit',[('a/b',{'a':{'value':'1','unit':'元'},'b':{'value':'0','unit':'1'}},'元'),
    ('a+b',{'a':{'value':'1','unit':'元'},'b':{'value':'1','unit':'m'}},'元'),
    ('__import__("os").system("echo bad")',{'a':{'value':'1','unit':'元'}},'元'),
    ('a',{'a':{'value':'NaN','unit':'元'}},'元')])
def test_arithmetic_only_keeps_safety_and_unit_bounds(expression,parameters,unit):
    with pytest.raises(FormulaError):evaluate_arithmetic(expression,parameters,unit)


def test_arithmetic_cancel_stays_distinct():
    with pytest.raises(InterruptedError):evaluate_arithmetic('a',{'a':{'value':'1','unit':'元'}},'元',cancelled=lambda:True)


def test_precision_refusal_cannot_complete_explicit_measurement():
    item={'id':'V1','type':'missing_precision','text':'甲测点没有精确数值标签，无法提供三位小数实测值。','bbox':[0,0,1,1]}
    record=visual_evidence(item);claims=[{'id':'C1','text':item['text'],'evidence_ids':['E1']}]
    before={'complete':True,'items':[{'id':'Q1','text':'给出甲测点精确到三位小数的实测值。','status':'answered','claim_ids':['C1']}]}
    adjusted,changes=tools.precision_coverage(before,claims,{'E1':record})
    assert adjusted['complete'] is False and changes and before['complete'] is True


def test_chart_to_calculation_keeps_typed_parameter_and_final_visual_review(pixels):
    item=printed();gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        actions({'id':'K','kind':'calculate','expression':'a','bindings':{'a':{'evidence_id':'E1','visual_item_id':'V1','unit':'万元'}},
                 'output_unit':'元','depends_on':['V']}),
        {'status':'completed','claims':[{'text':'2024年甲收入折合100000元。','evidence_ids':['E1'],'calculation_ids':['K1']}]},
        approved(visual_supported=True,chart_relations=[{'id':'C1','supported':True}]))
    status,result=answer_cross_source([image_source()],'请按表达式a将2024年甲收入换算为元。',[],gateway)
    assert status=='completed' and result['calculations'][0]['value']=='100000'
    assert result['calculations'][0]['parameters'][0]['binding']['visual_item_id']=='V1'
    assert result['steps'][1]['depends_on']==['V']
    assert gateway.calls[-1]['payload']['chart_relation_claim_ids']==['C1']


def test_formula_read_to_bound_calculation_uses_structure_and_distinct_parameters(pixels):
    record=formula_record();formula={'id':'F1','label':'(1)','tree':record['formula']['tree'],'limitations':[],
        'parameters':[{'id':f'P{i}','symbol':symbol,'value':value,'unit':unit,'quote':f'{symbol}={value} {unit}','unit_evidence':''}
                      for i,(symbol,value,unit) in enumerate([('a','12','kWh'),('b','8','kWh'),('c','4','h')],1)]}
    gateway=Responses(actions({'id':'F','kind':'formula_read','source_id':'img','page':1,'query':'读取R公式和参数','depends_on':[]}),
        {'formulas':[formula],'limitations':[]},{'items':[{'id':i,'supported':True} for i in ['F1','F1.P1','F1.P2','F1.P3']]},
        actions({'id':'K','kind':'calculate','expression':'(a+b)/c','bindings':{
            name:{'evidence_id':f'E{i+1}','formula_id':'F1','parameter_id':f'P{i}','unit':unit}
            for i,(name,unit) in enumerate([('a','kWh'),('b','kWh'),('c','h')],1)},
            'output_unit':'kW','rule_evidence_ids':['E1'],'depends_on':['F']}),
        {'status':'completed','claims':[{'text':'按公式(1)，R复算为5.00 kW。','evidence_ids':['E1','E2','E3','E4'],'calculation_ids':['K1']}]},
        approved(visual_supported=True))
    status,result=answer_cross_source([image_source()],'按原页公式及参数复算R。',[],gateway)
    assert status=='completed',result
    assert [r['kind'] for r in result['evidence']]==['formula_structure','formula_parameter','formula_parameter','formula_parameter']
    assert result['calculations'][0]['visual_rule_structure_checked'] is True
    assert result['evidence'][0]['formula']['execution_eligible'] is False

    assert '(1)' in result['evidence'][0]['quote']
    assert result['evidence'][0]['quote_origin']=='independently_reviewed_formula_transcription'


@pytest.mark.parametrize('coverage',['absent','invalid'])
def test_visual_coverage_unknown_preserves_verified_partial_without_extra_call(pixels,coverage):
    item=printed();review=approved(visual_supported=True,chart_relations=[{'id':'C1','supported':True}])
    if coverage=='absent':review.pop('coverage')
    else:review['coverage']='bad'
    gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        {'status':'completed','claims':[fact(item)]},review)
    status,result=answer_cross_source([image_source()],'2024年甲收入是多少？',[],gateway)
    assert status=='insufficient_evidence' and result['claims'][0]['text']==item['text']
    assert len(gateway.calls)==5 and result['verification']['status']=='limited'


def test_missing_final_chart_relation_never_publishes_or_retains_claim(pixels):
    item=printed();final={'status':'completed','claims':[fact(item)]};review=approved(visual_supported=True)
    gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},final,review,final,review)
    status,result=answer_cross_source([image_source()],'2024年甲收入是多少？',[],gateway)
    assert status!='completed' and result['claims']==[]
    assert result['visual_tool_audit'][0]['reading']['review']['supported_ids']==['V1']


def test_scope_filtered_vector_search_never_returns_other_page(tmp_path):
    document={'id':'d','project_id':'p','name':'pages.txt','sha256':'a'*64,'parser_version':'test','metadata':{},
              'pages':[{'number':1,'text':'甲收入10元。'},{'number':2,'text':'乙收入20元。'}]}
    handle=RetrievalService(tmp_path,None,default_mode='lexical').prepare(document,'p')
    chunks,_=handle.select('甲乙收入',pages=[2],limit=4)
    assert [c.page for c in chunks]==[2] and handle.audit()['searches'][0]['selected_pages_filter']==[2]


def test_visual_result_parameterizes_later_sql(pixels,tmp_path):
    import sqlite3
    from backend.parsers import sqlite_tables
    path=tmp_path/'actual.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE groups(name TEXT, category TEXT)')
        db.execute('INSERT INTO groups VALUES (?,?)',('甲','试点组'))
    tables,warnings=sqlite_tables(path)
    database={'id':'db','name':'名单.sqlite','format':'sqlite','data_path':str(path),'tables':tables,'warnings':warnings,
              'sha256':'b'*64,'parser_version':'test','metadata':{},'pages':[]}
    item=printed()
    gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        actions({'id':'S','kind':'sql','source_id':'db','sql':'SELECT category FROM groups WHERE name=?',
                 'parameters':[{'from_step':'V','row':0,'column':'category'}],'depends_on':['V']}),
        {'status':'completed','claims':[{'text':'图中甲属于试点组。','evidence_ids':['E2']}]},approved(visual_supported=True,chart_relations=[{'id':'C1','supported':True}]))
    status,result=answer_cross_source([image_source(),database],'图中甲收入对应对象属于哪组？',[],gateway)
    assert status=='completed',result
    assert gateway.calls[-1]['payload']['chart_relation_claim_ids']==['C1']
    assert result['steps'][1]['query']['parameters']==['甲']
    assert result['steps'][1]['query']['parameter_bindings'][0]['resolved_value']=='甲'
    assert result['steps'][1]['depends_on']==['V']
    assert result['evidence'][1]['query_result']['rows']==[['试点组']]


def test_visual_result_real_object_can_form_later_search(pixels):
    source={'id':'rule','name':'说明.txt','sha256':'c'*64,'parser_version':'test','metadata':{},'tables':[],
            'pages':[{'number':1,'text':'甲使用实际统计口径。'}],'warnings':[]}
    item=printed();gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        actions({'id':'S','kind':'search','source_id':'rule','query':[{'from_step':'V','row':0,'column':'category'},'使用什么口径'],
                 'depends_on':['V']}),{'status':'completed','claims':[{'text':'甲使用实际统计口径。','evidence_ids':['E2']}]},
        approved(visual_supported=True,chart_relations=[{'id':'C1','supported':True}]))
    status,result=answer_cross_source([image_source(),source],'图中甲使用什么口径？',[],gateway)
    assert status=='completed' and result['steps'][1]['depends_on']==['V']


def test_visual_relation_outputs_do_not_expose_threshold_as_measurement():
    item={'id':'V1','type':'visual_relation','text':'甲在10万元参考线上方。','value':'10','category':'甲',
          'reference_labels':[{'value':'10','unit':'万元'}]}
    result=tools.visual_output('chart_read',[visual_evidence(item)],'reviewed')
    assert result['rows'][0][result['columns'].index('value')] is None


@pytest.mark.parametrize('key,value',[('source_id','another'),('sha256','another'),('parser_version','another'),('page',2)])
def test_visual_inner_identity_cannot_differ_from_registry(key,value):
    record=visual_evidence();record['visual_item'][key]=value
    with pytest.raises(CrossSourceError):
        _claims({'claims':[fact()]},{'E1':record},{},sources={'img':image_source()})


@pytest.mark.parametrize('supported',[True,False])
def test_negative_visual_statement_also_requires_chart_relation_review(pixels,supported):
    item={'id':'V1','type':'missing_precision','text':'甲没有精确值标签，不能提供三位小数实测值。','bbox':[0,0,1,1]}
    review={'negative_supported':True,'conflict':False,'visual_supported':True,'claims':[{'id':'C1','supported':True}]}
    if supported:review['chart_relations']=[{'id':'C1','supported':True}]
    gateway=Responses(actions(chart_action()),{'items':[item]},{'items':[{'id':'V1','supported':True}]},
        {'status':'insufficient_evidence','reason':'missing_evidence','claims':[fact(item)]},review)
    status,result=answer_cross_source([image_source()],'给出甲三位小数实测值。',[],gateway)
    assert gateway.calls[-1]['payload']['chart_relation_claim_ids']==['C1']
    assert bool(result['claims']) is supported
    assert status!='completed'


def parameters_and_rule():
    records={'F':formula_record(),'A':parameter_record('A','a','12','kWh'),'B':parameter_record('B','b','8','kWh'),
             'D':parameter_record('D','c','4','h')}
    bindings={name:{'evidence_id':eid,'formula_id':'F1','parameter_id':eid,'unit':unit} for name,eid,unit in [('a','A','kWh'),('b','B','kWh'),('c','D','h')]}
    return records,bindings


@pytest.mark.parametrize('output,expected',[('W','5000'),('kW','5'),('MW','0.005')])
def test_visual_formula_standard_conversion_keeps_source_expression(output,expected):
    from decimal import Decimal
    records,bindings=parameters_and_rule()
    result=calculate_formula('(a+b)/c',bindings,records,output,rule_evidence_ids=['F'])
    assert Decimal(result['value'])==Decimal(expected)
    assert result['visual_rule_structure_checked'] is True


def test_visual_formula_rejects_extra_conversion_multiplier():
    records,bindings=parameters_and_rule()
    with pytest.raises(FormulaError):
        calculate_formula('(a+b)/c*1000',bindings,records,'W',rule_evidence_ids=['F'])


def test_display_only_formula_is_controlled_error_not_attribute_crash():
    records,bindings=parameters_and_rule()
    tree={'type':'matrix','rows':[[{'type':'text','text':'a'},{'type':'text','text':'b'}]]}
    records['F']['formula'].update(tree=tree,computation_ast=None)
    with pytest.raises(FormulaError,match='不支持确定性复算|仅支持展示'):
        calculate_formula('(a+b)/c',bindings,records,'kW',rule_evidence_ids=['F'])


def test_bound_visual_period_is_not_rejected_by_second_numeric_gate():
    item=printed();item['text']='甲收入为10万元。'
    text='2024年甲收入为10万元。';candidate=fact(item);candidate['text']=text
    candidate['visual_bindings'][0]['statement']=text
    result=_claims({'claims':[candidate]},{'E1':visual_evidence(item)},{},sources={'img':image_source()})
    assert result[0]['text']==text


def test_visual_period_never_authorizes_unbound_business_number():
    item=printed();item['text']='甲收入为10万元。'
    statement='2024年甲收入为10万元。';candidate=fact(item);candidate['text']=statement+'另有2024万元。'
    candidate['visual_bindings'][0]['statement']=statement
    with pytest.raises(CrossSourceError):
        _claims({'claims':[candidate]},{'E1':visual_evidence(item)},{},sources={'img':image_source()})


def test_false_visual_year_still_rejected_by_specific_gate():
    item=printed();item['text']='甲收入为10万元。'
    text='2025年甲收入为10万元。';candidate=fact(item);candidate['text']=text
    candidate['visual_bindings'][0]['statement']=text
    with pytest.raises(CrossSourceError):
        _claims({'claims':[candidate]},{'E1':visual_evidence(item)},{},sources={'img':image_source()})
