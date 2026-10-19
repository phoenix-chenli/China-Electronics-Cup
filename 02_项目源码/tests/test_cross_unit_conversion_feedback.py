"""Synthetic source and scripted gateway, not a real-model performance claim."""
import copy,json,sqlite3
import pytest
from backend.formula_engine import calculate_formula,FormulaError
from backend.cross_source_engine import answer_cross_source
from backend.calculation_protocol import execution_receipt
from test_cross_source_engine import sources,Responses,actions,sql,search,completed,approved


def unit_source(sources):
    with sqlite3.connect(sources[0]['data_path']) as c:c.execute("UPDATE orders SET amount='56000' WHERE id=1")
    sources[1]['pages'][0]['text']='数据库amount是登记金额，单位为分；100分=1元。原单位合计按已登记数值求和。'


def convert(expression='a'):
    return {'id':'S3','kind':'calculate','expression':expression,
        'bindings':{'a':{'evidence_id':'E1','row':0,'column':'amount','unit':'分','unit_evidence_id':'E2'}},
        'output_unit':'元','rule_evidence_ids':['E2'],'depends_on':['S1','S2']}


def test_exact_conversion_is_visible_next_round_and_reused_sql_has_specific_feedback(sources):
    unit_source(sources);before=copy.deepcopy(sources)
    g=Responses(actions(sql(),search()),actions(convert()),actions(sql('S4')),
                completed('登记金额换算为560.00元。'),approved())
    status,r=answer_cross_source(sources,'将登记金额从原始单位换算为元。',[],g)
    assert status=='completed' and r['calculations'][0]['display_value']=='560.00'
    c=g.calls[2]['payload']['calculations'][0]
    assert c['id']=='K1' and c['expression']=='a'
    assert c['unit_handling']['parameter_base_values'][0]['base_value']=='560.00'
    assert c['unit_handling']['pure_unit_conversion'] and 'review_warning' not in c['unit_handling']
    assert g.calls[2]['payload']['mechanical_execution_state']['executed_calculation_ids']==['K1']
    feedback=g.calls[3]['payload']['protocol_feedback'][-1]
    assert feedback['available_calculation_ids']==['K1'] and feedback['result_reexecuted'] is False
    assert feedback['available_evidence_ids']==['E1'] and '非空' in feedback['message']
    audit=next(a['record'] for a in r['diagnostics']['audit_history'] if a['kind']=='calculation_mechanically_executed')
    assert audit['publication_status']=='pending_semantic_review' and not audit['is_evidence']
    assert audit['execution']['value']['calculation']['display_value']=='560.00'
    assert sources==before


def test_double_conversion_is_flagged_for_review_and_never_auto_approved(sources):
    unit_source(sources)
    rejected=approved(supported=False,claims=[{'id':'C1','supported':False}])
    g=Responses(actions(sql(),search()),actions(convert('a/100')),
        completed('登记金额换算为5.60元。'),rejected,completed('登记金额换算为5.60元。'),rejected)
    status,r=answer_cross_source(sources,'将登记金额从原始单位换算为元。',[],g)
    assert status!='completed' and r['claims']==[] and r['calculations']==[]
    c=g.calls[2]['payload']['calculations'][0]
    assert c['display_value']=='5.60' and '重复乘除' in c['unit_handling']['review_warning']
    review=g.calls[3]['payload']['calculations'][0]
    assert review['unit_handling']==c['unit_handling']
    a=next(a['record'] for a in r['diagnostics']['audit_history'] if a['kind']=='calculation_mechanically_executed')
    assert a['execution']['value']['calculation']['expression']=='a/100'
    assert a['execution']['value']['calculation']['display_value']=='5.60'
    assert a['semantic_status']=='not_reviewed' and a['diagnostic_only']


def test_unit_source_proof_remains_required_and_is_not_inferred_from_rule_reference():
    e={'E1':{'kind':'table','original_columns':['amount'],'query_result':{'columns':['amount'],'rows':[[56000]]}},
       'E2':{'kind':'text','quote':'登记金额原单位为分。100分=1元。'}}
    b={'a':{'evidence_id':'E1','row':0,'column':'amount','unit':'分'}}
    with pytest.raises(FormulaError,match='单位'):calculate_formula('a',b,e,'元',rule_evidence_ids=['E2'])
    b['a']['unit_evidence_id']='E2'
    assert calculate_formula('a',b,e,'元',rule_evidence_ids=['E2'])['display_value']=='560.00'
    b['a']['unit_evidence_id']='E1'
    with pytest.raises(FormulaError,match='单位依据'):calculate_formula('a',b,e,'元',rule_evidence_ids=['E2'])


def test_local_context_limit_reports_safe_sizes_without_sending_model_request(sources,monkeypatch):
    import backend.cross_source_engine as engine
    monkeypatch.setattr(engine,'MAX_CONTEXT',200)
    g=Responses();status,r=answer_cross_source(sources,'查看登记金额。',[],g)
    assert status=='insufficient_evidence' and not g.calls
    error=next(e for e in r['diagnostics']['tool_errors'] if e['kind']=='local_context_limit')
    assert error['serialized_characters']>error['limit_characters']==200
    assert error['request_sent'] is False and error['component_characters']['sources']>0
    assert 'source' not in error and 'payload' not in error and 'prompt' not in error
    assert '不是资料不存在答案' in error['message']


def test_mechanical_receipt_is_bounded_redacted_and_detached():
    action=convert();calc={'id':'K1','expression':'a','value':'560','display_value':'560.00','unit':'元',
        'parameters':[{'binding':{'quote':'api_key="fixture-secret" /Users/example/private.txt'},'user_input':{'source_text':'x'*10000}}]}
    original=copy.deepcopy(calc);record=execution_receipt(action,calc);raw=json.dumps(record,ensure_ascii=False)
    assert 'fixture-secret' not in raw and '/Users/example' not in raw and len(raw)<4800
    assert record['execution']['truncated'] and record['execution']['redacted']
    assert calc==original
