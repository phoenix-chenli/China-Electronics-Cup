"""Offline citation and statistical qualifier contracts, not model accuracy."""
import copy
import json
import pytest

from backend.claim_evidence_scope import (check_declared_qualifiers, claim_review_payload,
    review_image_keys)
from backend.cross_source_engine import _claims, CrossSourceError, answer_cross_source
from test_cross_source_engine import (Responses, actions, approved, sources,
    structured_pdf_table, structured_table_answer)
from backend.table_context import context_for_table


def e(identifier='E1', quote='2024年增加值为10亿元，比上年增长5%。', **kwargs):
    return dict(id=identifier, kind='text', source_id='s', quote=quote, page=1,
                sha256='version1', parser_version='test', **kwargs)


def test_unreferenced_qualifier_cannot_backfill_claim_or_be_auto_cited():
    evidence={'E1':e(), 'E2':e('E2','2024年增长按不变价格计算。')}
    candidate={'claims':[{'text':'2024年增加值按不变价格计算增长5%。','evidence_ids':['E1']}]}
    before=copy.deepcopy((candidate,evidence))
    with pytest.raises(CrossSourceError) as error:
        _claims(candidate,evidence,{},question='2024年增长')
    assert error.value.details['kind']=='citation_qualifier'
    assert error.value.details['claim_evidence_ids']==['E1']
    assert error.value.details['unsupported_qualifiers'][0]['family']=='constant_price'
    assert (candidate,evidence)==before
    candidate['claims'][0]['evidence_ids'].append('E2')
    claim=_claims(candidate,evidence,{},question='2024年增长')[0]
    assert claim['citation_qualifiers'][0]['evidence_ids']==['E2']


def test_other_claim_citation_not_this_claim_support():
    evidence={'E1':e(), 'E2':e('E2','2024年增长按不变价格计算。')}
    with pytest.raises(CrossSourceError,match='限定词引用不自足'):
        _claims({'claims':[{'text':'增长按不变价格计算。','evidence_ids':['E2']},
                           {'text':'增加值按不变价格计算增长5%。','evidence_ids':['E1']}]},evidence,{})


@pytest.mark.parametrize('claim,source',[
    ('按不变价计算增长5%。','按现价计算增长5%。'),
    ('按可比价计算增长5%。','按不变价计算增长5%。'),
    ('初步核算增加值为10亿元。','预测增加值为10亿元。'),
    ('预计增加值为10亿元。','初步统计增加值为10亿元。')])
def test_statistical_qualifier_families_not_interchangeable(claim,source):
    assert check_declared_qualifiers(claim,[e(quote=source)])['unsupported']


@pytest.mark.parametrize('claim', ['不能确认按不变价格计算。','不变价格口径尚未确认。','无法判断是否为初步统计数据。'])
def test_explicit_unknown_scope_not_positive_statistical_assertion(claim):
    assert not check_declared_qualifiers(claim,[e()])['unsupported']


def test_source_qualifier_does_not_automatically_add_fact_or_citation():
    evidence={'E1':e(quote='初步核算，2024年增加值为10亿元。')}
    result=_claims({'claims':[{'text':'2024年增加值为10亿元。','evidence_ids':['E1']}]},evidence,{})
    assert result[0]['text']=='2024年增加值为10亿元。'
    assert 'citation_qualifiers' not in result[0]


def table_evidence(sources):
    structured_pdf_table(sources)
    return {'E1':dict(id='E1',kind='text',source_id='db',sha256='dbhash',parser_version='test',
         quote='1 | 林禾 | 12 | 12',table_context=context_for_table(sources[0],'orders',[2]))}


@pytest.mark.parametrize('location',['header_path','footnotes','context'])
def test_real_bound_table_header_or_note_is_in_citation_scope(sources,location):
    evidence=table_evidence(sources); table=evidence['E1']['table_context']['tables'][0]
    candidate=structured_table_answer()
    candidate['claims'][0]['text']='按不变价格计算，林禾的金额为12万元。'
    candidate['claims'][0]['table_bindings'][0]['statement']=candidate['claims'][0]['text']
    annotation={'text':'按不变价格计算','evidence':{'page':1,'start':0,'end':8}}
    if location=='header_path':
        table['columns'][2]['header_path'].insert(0,annotation)
        candidate['claims'][0]['text']='林禾按不变价格计算 / 金额（万元）为12万元。'
        candidate['claims'][0]['table_bindings'][0].update(column_label='按不变价格计算 / 金额（万元）',statement=candidate['claims'][0]['text'])
    else: table[location]=[annotation]
    assert _claims(candidate,evidence,{})[0]['citation_qualifiers'][0]['evidence_ids']==['E1']


def test_other_table_column_not_bound_qualifier_support(sources):
    evidence=table_evidence(sources); table=evidence['E1']['table_context']['tables'][0]
    table['columns'][3]['header_path'].insert(0,{'text':'初步统计'})
    candidate=structured_table_answer()
    candidate['claims'][0]['text']='初步统计，林禾的金额为12万元。'
    candidate['claims'][0]['table_bindings'][0]['statement']=candidate['claims'][0]['text']
    with pytest.raises(CrossSourceError,match='限定词引用不自足'):_claims(candidate,evidence,{})


def test_table_qualifiers_cannot_be_supplied_by_sql_alias_or_model_binding_text():
    record=e(quote='SELECT amount AS 初步核算');record['kind']='sql'
    assert check_declared_qualifiers('初步核算为10亿元。',[record])['unsupported']


def test_review_packages_exclude_other_reads_and_keep_executed_dependencies():
    evidence={'E1':e(), 'E2':e('E2','按不变价格计算。'), 'E3':e('E3','无关的初步统计数。'),
              'E4':e('E4','真实参数5%。')}
    calculations={'K1':{'id':'K1','value':'5','parameters':[],'evidence_ids':['E4']}}
    steps=[{'id':'S1','kind':'search','evidence_ids':['E1','E2','E3'],'depends_on':[]},
           {'id':'S2','kind':'search','evidence_ids':['E4'],'depends_on':[]},
           {'id':'S3','kind':'calculate','evidence_ids':['E4'],'depends_on':['S2'],'calculation':calculations['K1']}]
    context={'question':'问题','history':[],'sources':[{'id':'s','tables':[{'sample':'不可用目录样本补事实'}]}]}
    claims=[{'id':'C1','text':'增长5%。','evidence_ids':['E1']},
            {'id':'C2','text':'计算结果5。','evidence_ids':['E4'],'calculation_ids':['K1']}]
    before=copy.deepcopy((context,claims,evidence,calculations,steps))
    payload=claim_review_payload(context,claims,evidence,calculations,steps)
    assert [x['id'] for x in payload['evidence']]==['E1','E4']
    assert payload['claim_support'][0]['evidence_ids']==['E1']
    assert payload['claim_support'][1]['calculation_ids']==['K1']
    assert payload['steps'][0]['evidence_ids']==['E1']
    assert 'tables' not in payload['sources'][0]
    assert 'candidate_feedback' not in payload and 'retained_claims' not in payload
    assert (context,claims,evidence,calculations,steps)==before
    assert review_image_keys(payload)=={('s',1)}


def test_bad_qualifier_is_feedback_then_explicit_citation_repaired_and_independently_reviewed():
    source=dict(id='s',name='原文.txt',sha256='hash',parser_version='test',format='txt',tables=[],warnings=[],metadata={},pages=[
        {'number':1,'text':'2024年增加值为10亿元，比上年增长5%。'},
        {'number':2,'text':'2024年增加值增长按不变价格计算。'},
        {'number':3,'text':'无关项目按现价计算。'}])
    bad={'status':'completed','claims':[{'text':'2024年增加值按不变价格计算增长5%。','evidence_ids':['E1']}]}
    good=copy.deepcopy(bad);good['claims'][0]['evidence_ids']=['E1','E2']
    gateway=Responses(actions(*[{'id':f'S{i}','kind':'search','source_id':'s','query':'增加值增长','page':i,'depends_on':[]} for i in (1,2,3)]),bad,good,approved())
    status,result=answer_cross_source([source],'2024年增加值增长多少，保留不变价格口径？',[],gateway)
    assert status=='completed' and len(gateway.calls)==4
    repair=gateway.calls[2]['payload']['tool_errors'][-1]
    assert repair['kind']=='citation_qualifier' and repair['claim_evidence_ids']==['E1']
    review=gateway.calls[-1]['payload']
    assert review['claim_support'][0]['evidence_ids']==['E1','E2']
    assert [item['id'] for item in review['evidence']]==['E1','E2']
    assert 'tool_errors' not in review and 'last_candidate_feedback' not in review
    assert result['diagnostics']['rejections'][0]['kind']=='citation_qualifier'


def test_repaired_qualifier_does_not_override_independent_semantic_rejection():
    source=dict(id='s',name='原文.txt',sha256='hash',parser_version='test',tables=[],warnings=[],metadata={},
                pages=[{'number':1,'text':'初步统计，增加值为10亿元。'}])
    candidate={'status':'completed','claims':[{'text':'初步核算，增加值为10亿元。','evidence_ids':['E1']}]}
    denied=approved(supported=False,claims=[{'id':'C1','supported':False}])
    gateway=Responses(actions({'id':'S1','kind':'search','source_id':'s','query':'增加值','depends_on':[]}),candidate,denied,candidate,denied,candidate,denied)
    status,result=answer_cross_source([source],'增加值是多少？',[],gateway)
    assert status!='completed' and result['claims']==[]
    assert sum(c['purpose']=='verify' for c in gateway.calls)>=1


def test_year_only_legacy_citation_recovery_cannot_authorize_unrequested_qualifier():
    evidence={'E1':e(quote='增加值增长5%。'), 'E2':e('E2','2024年增长按不变价格计算。')}
    with pytest.raises(CrossSourceError,match='限定词引用不自足') as exc:
        _claims({'claims':[{'text':'2024年按不变价格计算增长5%。','evidence_ids':['E1']}]},evidence,{},question='2024年增长')
    assert exc.value.details['claim_evidence_ids']==['E1']


@pytest.mark.parametrize('source',['不是初步统计数据。','并非按不变价格计算。','未按现价计算。'])
def test_source_denial_does_not_license_positive_qualifier(source):
    claim=source.replace('不是','').replace('并非','').replace('未按','按')
    assert check_declared_qualifiers(claim,[e(quote=source)])['unsupported']
