"""Verified reference labels do not authorize matching business amounts."""
import copy
import pytest
from backend.claim_context_annotations import annotate_context
from backend.cross_source_engine import _claims, CrossSourceError
from test_user_scenarios import given_calculation, GIVEN, RULE


def fixture():
    source={'id':'s','name':'手册.pdf','format':'pdf','sha256':'abc','parser_version':'v1',
            'metadata':{'page_unit':'physical_page'},'pages':[{'number':340,'text':'FHWA NHI-06-088\n'+RULE['E1']['quote']}]}
    evidence={'E1':{**RULE['E1'],'id':'E1','source_id':'s','sha256':'abc','parser_version':'v1','page':340}}
    return source,evidence,given_calculation()


def test_verified_source_code_page_and_typed_given_quantity_are_occurrence_local():
    source,evidence,calc=fixture()
    text='按 FHWA NHI-06-088 手册第340页，在用户假设 Is(50)=2 MPa 情景下，估计为48.00 MPa。'
    masked,annotations=annotate_context(text,['E1'],evidence,[calc],{'s':source})
    assert len(masked)==len(text)
    assert '06-088' not in masked and '340' not in masked and '2 MPa' not in masked
    assert '48.00 MPa' in masked
    assert {a['kind'] for a in annotations}=={'page_label','source_identifier','user_assumption'}
    checked=_claims({'claims':[{'text':text,'evidence_ids':['E1'],'calculation_ids':['K1']}]},evidence,{'K1':calc},question=GIVEN,sources={'s':source})
    assert checked[0]['text']==text and len(checked[0]['context_annotations'])==3
    for addition in ['实际金额340元。','实际压力2 MPa。','实际数量88项。']:
        with pytest.raises(CrossSourceError,match='数值'):
            _claims({'claims':[{'text':text+addition,'evidence_ids':['E1'],'calculation_ids':['K1']}]},evidence,{'K1':calc},question=GIVEN,sources={'s':source})


@pytest.mark.parametrize('text',[
    '按 NHI-06-099 手册第341页，假设 Is(50)=3 MPa。',
    '按 NHI-06-088 手册，实际测得 Is(50)=2 MPa。',
    '假设情景下，实际 Is(50)=2 MPa。',
    '假设 Is(50)=2 MPa / m。',
    '假设 Is(50)=2 MPa 每米。',
    '假设 x=2 MPa。',
    '撤销假设，实际 Is(50)=2 MPa。',
    '不是假设，Is(50)=2 MPa是实际值。',
    '未给定 Is(50)=2 MPa。',
])
def test_wrong_or_actual_quantity_is_not_masked(text):
    source,evidence,calc=fixture()
    masked,annotations=annotate_context(text,['E1'],evidence,[calc],{'s':source})
    assert not any(a['kind']=='user_assumption' for a in annotations)
    if '341' in text: assert '341' in masked and '06-099' in masked


def test_missing_same_page_provenance_and_tampered_input_stay_subject_to_numeric_gate():
    source,evidence,calc=fixture()
    text='按 NHI-06-088 手册第340页，假设 Is(50)=2 MPa。'
    source['pages'][0]['text']='无该文献编号'
    calc['parameters'][0]['user_input']['source_text']='被改写的原问题'
    masked,annotations=annotate_context(text,['E1'],evidence,[calc],{'s':source})
    assert 'NHI-06-088' in masked and '2 MPa' in masked
    source['sha256']='changed'
    masked,_=annotate_context(text,['E1'],evidence,[],{'s':source})
    assert '340' in masked


def test_annotations_are_not_evidence_or_global_numeric_allowances():
    source,evidence,calc=fixture();before=copy.deepcopy((source,evidence,calc))
    masked,annotations=annotate_context('假设 Is(50)=2 MPa，实测压力2 MPa。',['E1'],evidence,[calc],{'s':source})
    assert masked.endswith('实测压力2 MPa。')
    assert annotations[0]['is_document_evidence'] is False
    assert (source,evidence,calc)==before


def test_query_fact_failure_shows_only_real_selected_row_without_repairing():
    from backend.query_fact_renderer import failure_feedback,expand_candidate_claims
    from backend.query_result_context import build_query_result_context
    e={'id':'E1','source_id':'s','sha256':'h','parser_version':'p','kind':'query',
       'query':{'sql':'SELECT SUM(amount) AS amount,COUNT(*) AS count,COUNT(missing) AS missing FROM t','parameters':[]},
       'query_result':{'columns':['amount','count','missing'],'rows':[[81000,9,2]],'truncated':False}}
    e['query_result_context']=build_query_result_context(e);c=e['query_result_context'];cells=c['rows'][0]['cells']
    candidate={'text':'合计81000，记录9笔，其中缺失2笔。','query_fact':{'evidence_id':'E1','context_id':c['context_alias'],'cell_id':cells[0]['cell_id'],'entity_cells':[]}}
    before=copy.deepcopy(candidate)
    with pytest.raises(ValueError,match='还有未由本格'):expand_candidate_claims([candidate],[e])
    feedback=failure_feedback([candidate],1,[e])
    assert [x['value'] for x in feedback['existing_query_rows'][0]['cells']]==[81000,9,2]
    assert feedback['action_rewritten'] is False and candidate==before
    assert '无需重发' in feedback['repair_action']
    with pytest.raises(ValueError,match='还有未由本格'):expand_candidate_claims([candidate],[e])


@pytest.mark.parametrize('disclaimer',[
    '不是实际登记结果', '并非实际金额', '不代表实际值', '不属于实际记录',
    '这不是实测值', '该结果并非观测结果', '不是实际登记结果，不修改原始数据',
])
def test_explicit_negative_actual_status_does_not_block_local_percentage(disclaimer):
    from test_user_scenarios import percentage
    calculation=percentage()
    text=f'假设情景（{disclaimer}）：按用户假设减少10%，假设金额为90.00元。'
    masked,annotations=annotate_context(text,[],{},[calculation],{})
    assert len(annotations)==1 and annotations[0]['label']=='减少10%'
    assert annotations[0]['is_document_evidence'] is False
    assert '10%' not in masked and '90.00' in masked and len(masked)==len(text)
    assert text[annotations[0]['start']:annotations[0]['end']]=='减少10%'


@pytest.mark.parametrize('text',[
    '假设情景（不是实际登记结果），实际按用户假设减少10%。',
    '假设情景（不是实际登记结果），观测减少10%。',
    '假设情景（不是实际登记结果），按用户假设减少10%为真实值。',
    '假设情景（不是实际登记结果），按用户假设减少11%。',
    '假设情景（不是实际登记结果），按用户假设增加10%。',
    '假设情景（不是实际登记结果），按用户假设减少10%/年。',
    '假设情景（不是实际登记结果），按用户假设减少10%每年。',
    '假设已撤销，按用户假设减少10%。',
    '取消此前假设，按用户假设减少10%。',
    '不是假设（不是实际登记结果），减少10%。',
    '假设情景（并非不是实际登记结果），按用户假设减少10%。',
    '假设情景（不能说不是实际登记结果），按用户假设减少10%。',
    '假设情景（不是实际登记结果10元），按用户假设减少10%。',
])
def test_actual_confusion_wrong_rate_unit_or_revocation_still_rejects(text):
    from test_user_scenarios import percentage
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert masked==text and annotations==[]


def test_real_failure_wording_keeps_second_percentage_expression_unmasked():
    from test_user_scenarios import percentage
    text='假设情景（不是实际登记结果，不修改原始数据）：若2024年已完成订单的登记金额按用户明确假设的减少10%计算，即按2024年原金额乘以(1-10%)，假设金额为9000.00分。'
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert len(annotations)==1 and annotations[0]['label']=='减少10%'
    assert '乘以(1-10%)' in masked and '9000.00分' in masked
    # Only the local input phrase is annotated. This patch does not certify the
    # repeated arithmetic expression or make the entire actual candidate valid.
    assert '10%' in masked


def test_negative_status_does_not_authorize_other_same_valued_business_fact():
    from test_user_scenarios import percentage
    text='假设情景（不是实际登记结果）：减少10%，假设金额为90元。实际手续费10元。'
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert len(annotations)==1 and masked.endswith('实际手续费10元。')


def test_negative_actual_status_also_preserves_typed_quantity_identity():
    source,evidence,calc=fixture()
    text='假设情景（并非实测值）：用户给定Is(50)=2 MPa，估计为48 MPa。'
    masked,annotations=annotate_context(text,['E1'],evidence,[calc],{'s':source})
    assert any(a['kind']=='user_assumption' for a in annotations)
    assert '2 MPa' not in masked and '48 MPa' in masked
    wrong=text.replace('Is(50)=2 MPa','Is(50)=2 kPa')
    assert not any(a['kind']=='user_assumption' for a in annotate_context(wrong,['E1'],evidence,[calc],{'s':source})[1])


def test_percentage_with_explicit_user_modifier_stays_exact_direction_and_rate():
    from test_user_scenarios import percentage
    text='假设情景（不是实际登记结果）：减少用户明确假设的10%，假设金额为90元。'
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert len(annotations)==1 and annotations[0]['label']=='减少用户明确假设的10%'
    assert '10%' not in masked
    for changed in [text.replace('10%','20%'),text.replace('减少','增加'),text.replace('10%','10%/年')]:
        assert annotate_context(changed,[],{},[percentage()],{})[1]==[]


@pytest.mark.parametrize('text',[
    '登记金额减少用户明确假设的10%后，假设金额为90元。',
    '登记金额减少用户假定的10%后，假设金额为90元。',
])
def test_explicit_user_assumption_inside_matched_phrase_is_a_label(text):
    from test_user_scenarios import percentage
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert len(annotations)==1 and '10%' not in masked and '90' in masked


@pytest.mark.parametrize('text',[
    '不要减少用户明确假设的10%。',
    '未采用减少用户明确假设的10%。',
    '不是减少用户明确假设的10%。',
    '不采用减少用户明确假设的10%。',
    '不得减少用户明确假设的10%。',
    '取消假设后，减少用户明确假设的10%。',
    '实际登记金额减少用户明确假设的10%。',
    '观测金额减少用户明确假设的10%。',
    '登记金额减少用户明确假设的11%。',
    '登记金额增加用户明确假设的10%。',
    '登记金额减少用户明确假设的10%/年。',
    '登记金额减少10%。',
])
def test_internal_user_label_does_not_bypass_negation_actuals_or_parameters(text):
    from test_user_scenarios import percentage
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert masked==text and annotations==[]


def test_internal_label_does_not_authorize_repeated_percentage_expression():
    from test_user_scenarios import percentage
    text='登记金额减少用户明确假设的10%后，按原金额乘以(1-10%)，假设金额为90元。'
    masked,annotations=annotate_context(text,[],{},[percentage()],{})
    assert len(annotations)==1 and '(1-10%)' in masked and '90元' in masked
