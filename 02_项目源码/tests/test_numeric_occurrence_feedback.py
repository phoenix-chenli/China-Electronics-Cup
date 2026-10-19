"""Numeric feedback regression; no paid requests or product file writes."""
import copy
from decimal import Decimal
import pytest
from backend.cross_source_engine import _claims, _numeric_occurrence_feedback, _numbers, CrossSourceError
from test_user_scenarios import percentage

TEXT='假设情景（不是实际登记结果，不修改原始数据）：若2024年已完成订单登记金额按用户明确假设减少10%，按2024年原登记金额乘以(1-10%)计算，假设金额为9000.00分。'
EVIDENCE={'E1':{'id':'E1','kind':'text','quote':'2024年实际登记金额10000分。人民币100分=1元。'},'E2':{'id':'E2','kind':'text','quote':'2024年已完成订单登记金额10000分。'}}

def check(text):
    return _claims({'claims':[{'text':text,'evidence_ids':['E1','E2'],'calculation_ids':['K1']}]},EVIDENCE,{'K1':percentage(output_unit='分')})

def feedback(text):
    with pytest.raises(CrossSourceError) as caught:check(text)
    return caught.value.details['claim_failures'][0]['numeric_occurrence_feedback']

def corrected():return TEXT.replace('，按2024年原登记金额乘以(1-10%)计算','')

def test_actual_repeated_rate_identifies_second_occurrence_and_accepted_first():
    out=feedback(TEXT);first=TEXT.index('10%');second=TEXT.index('10%',first+1)
    assert out['offset_basis']=='original_candidate_text'
    assert len(out['unverified_occurrences'])==1
    span=out['unverified_occurrences'][0]
    # The existing scanner skips a subtraction separator after a digit.
    assert span['start']==second and span['end']==second+2
    assert TEXT[span['start']:span['end']]=='10' and span['unverified_tokens']==['10']
    accepted=out['accepted_context_spans'][0]
    assert accepted['label']=='减少10%' and accepted['start']<first<accepted['end']
    assert '10%' in out['unbound_numeric_text'][second:]
    assert out['unlocated_tokens']==[]
    assert '用户未要求的重复代入式' in out['repair_guidance']
    assert '完整独立语义' in out['repair_guidance']

def test_manual_revision_retains_condition_rate_result_and_citations_no_auto_change():
    text=corrected();result=check(text)
    assert result[0]['text']==text and '减少10%' in text and '9000.00分' in text
    assert result[0]['calculation_ids']==['K1']

@pytest.mark.parametrize('mutation',[
    lambda s:s.replace('减少10%','减少11%'),lambda s:s.replace('9000.00','9100.00'),
    lambda s:s+'实际手续费10元。',lambda s:s.replace('减少10%','增加10%')])
def test_repair_does_not_authorize_other_tokens(mutation):
    with pytest.raises(CrossSourceError):check(mutation(corrected()))

def test_actual_status_cannot_use_hypothetical_calculation():
    with pytest.raises(CrossSourceError,match='假设'):check('2024年实际登记金额减少10%，金额为9000.00分。')

@pytest.mark.parametrize('text,expected',[
    ('日期2025年2月1日，日期2025-02-01。',[(2,11),(14,24)]),
    ('日期二零二五年二月一日。',[(2,11)]),
])
def test_complete_date_tokens_keep_exact_spans(text,expected):
    out=_numeric_occurrence_feedback(text,text,{'date:2025-02-01'},[])
    assert [(x['start'],x['end']) for x in out['unverified_occurrences']]==expected
    assert out['unlocated_tokens']==[]

def test_chinese_number_sign_and_subtraction_keep_gate_meaning():
    text='同一个对象有一项预算-10元，按1-10计算。'
    out=_numeric_occurrence_feedback(text,text,{Decimal(1),Decimal(-10),Decimal(10)},[])
    got=[(x['text'],x['unverified_tokens']) for x in out['unverified_occurrences']]
    assert got==[('一',['1']),('-10',['-10']),('1',['1']),('10',['10'])]

def test_sensitive_feedback_is_detached_redacted_and_bounded():
    text=TEXT+' api_key="fake-secret-value" /Users/private_person/private/data.csv'
    before=copy.deepcopy(EVIDENCE);out=feedback(text)
    assert 'fake-secret-value' not in str(out) and '/Users/private_person' not in str(out)
    assert out['unbound_numeric_text_redacted'] is True and EVIDENCE==before
    many=' '.join('10' for _ in range(40));out=_numeric_occurrence_feedback(many,many,{Decimal(10)},[])
    assert len(out['unverified_occurrences'])==24 and out['omitted_occurrences']==16

def test_unknown_token_and_offset_mismatch_report_limits_instead_of_inventing_spans():
    out=_numeric_occurrence_feedback('original','10',{Decimal(99)},[{'start':0,'end':1,'label':'o'}])
    assert out['unlocated_tokens']==['99'] and out['accepted_context_spans']==[]
    assert out['offset_basis']=='unbound_numeric_text_before_redaction'
