"""Deterministic protocol/controller tests; no model or network accuracy claim."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from backend import clarification_review as cr
from backend.model_gateway import ModelError
from backend.parsers import sqlite_tables
from backend.cross_source_engine import answer_cross_source
from test_clarification_review import Gateway, plan, proposal, records, run, source


def approved(**changes):
    return {**dict.fromkeys(cr.REVIEW_FIELDS, True), **changes}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_r8_received_fragment_replay_replans_without_publishing_or_fabricating_options(source):
    fixture = json.loads((Path(__file__).parent/'fixtures/r8-clarify002-received.json').read_text())
    raw = fixture['candidate']
    assert digest(raw) == fixture['candidate_identity']['sha256']
    assert digest(fixture['review']) == fixture['review_identity']['sha256']
    assert raw == {'question': '数据库中有两个station_id不同但名称均为'}
    assert fixture['review']['clarification_supported'] is True
    assert {e['code'] for e in cr.candidate_errors(raw)} == {'missing_field', 'unfinished_question_tail'}
    # Necessity-only approval saved in R8 is not sufficient under the new schema.
    assert len(cr.review_errors(fixture['review'])) == 3
    events=[]
    g=Gateway(plan(), {'status':'needs_clarification','clarification':raw}, proposal(), approved())
    status,result=run(source,g,audit=events.append)
    assert status=='needs_clarification' and result['clarification']==proposal()['clarification']
    assert [c['purpose'] for c in g.calls]==['table','table','table','verify']
    repair=g.calls[2]['payload']['last_clarification_review_repair']
    assert repair['previous_candidate_identity']==fixture['candidate_identity']
    assert 'options' not in repair['previous_clarification']
    assert {e['field'] for e in repair['feedback']['validation_errors']}=={'question','field','options'}
    assert records(events)[0]['record']['candidate_identity']==fixture['candidate_identity']
    assert not result['claims'] and not result['calculations']


def test_r8_same_sha_source_queries_replay_but_repaired_positive_is_explicitly_scripted():
    fixtures=Path(__file__).parent/'fixtures'
    fixture=json.loads((fixtures/'r8-clarify002-received.json').read_text())
    database=fixtures/'r7_station_register.sqlite'
    before=hashlib.sha256(database.read_bytes()).hexdigest()
    assert before=='da097e1987c8bfdd1673397b7d5fed6059f84c8ea1479c91d127709056dd0046'
    tables,warnings=sqlite_tables(database)
    original_source={'id':'db','name':'station_register.sqlite','data_path':str(database),
        'sha256':before,'parser_version':'zhiheng-multimodal-3.1','tables':tables,'warnings':warnings,'metadata':{}}
    actions=[{'id':f'S{i}','kind':'sql','source_id':'db',**item['query'],'depends_on':[]}
             for i,item in enumerate(fixture['query_results'],1)]
    repaired={'question':'请选择同名站的 station_id：A01 或 B01','field':'station_id','options':['A01','B01']}
    g=Gateway({'actions':actions},{'status':'needs_clarification','clarification':fixture['candidate']},
              {'status':'needs_clarification','clarification':repaired},approved())
    status,result=answer_cross_source([original_source],'同名站有多少巡检？',[],g)
    assert status=='needs_clarification' and result['clarification']==repaired
    actual=g.calls[-1]['payload']['evidence']
    for expected in fixture['query_results']:
        matching=[e for e in actual if e.get('query')==expected['query']]
        assert matching and all(e['query_result']['rows']==expected['query_result']['rows'] for e in matching)
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before


@pytest.mark.parametrize('candidate,field,code', [
    (None,'$','expected_object'),
    ([], '$','expected_object'),
    ({'field':'year','options':[]},'question','missing_field'),
    ({'question':'  ','field':'year','options':[]},'question','empty_string'),
    ({'question':'请提供年度','field':3,'options':[]},'field','expected_string'),
    ({'question':'请提供年度','field':'','options':[]},'field','empty_string'),
    ({'question':'请提供年度','field':'year'},'options','missing_field'),
    ({'question':'请提供年度','field':'year','options':'2025'},'options','expected_array'),
    ({'question':'请提供年度','field':'year','options':[True]},'options[0]','expected_string'),
    ({'question':'请选择对象','field':'id','options':['D01',' D01 ']},'options[1]','duplicate_option'),
    ({'question':'请选择对象','field':'id','options':['']},'options[0]','empty_string'),
])
def test_invalid_candidate_has_exact_feedback_then_uses_only_repaired_candidate(source,candidate,field,code):
    g=Gateway(plan(),{'status':'needs_clarification','clarification':candidate},proposal(),approved())
    status,result=run(source,g)
    assert status=='needs_clarification' and result['clarification']==proposal()['clarification']
    feedback=g.calls[2]['payload']['last_clarification_review_repair']['feedback']
    assert {'field':field,'code':code} in feedback['validation_errors']
    assert [c['purpose'] for c in g.calls].count('verify')==1


@pytest.mark.parametrize('changed,field,code', [
    ({'question':'完整问题'*201},'question','too_long'),
    ({'field':'f'*101},'field','too_long'),
    ({'options':['D01','D02','D03','D04','D05']},'options','too_many_options'),
    ({'options':['甲'*300+'甲站','甲'*300+'乙站']},'options[0]','too_long'),
])
def test_overlimit_is_feedback_not_silent_identity_clipping(source,changed,field,code):
    original=proposal();original['clarification'].update(changed)
    saved=copy.deepcopy(original)
    g=Gateway(plan(),original,proposal(),approved())
    status,result=run(source,g)
    assert original==saved and status=='needs_clarification'
    assert result['clarification']==proposal()['clarification']
    feedback=g.calls[2]['payload']['last_clarification_review_repair']['feedback']
    assert any(e['field']==field and e['code']==code for e in feedback['validation_errors'])
    assert g.calls[-1]['payload']['clarification']==proposal()['clarification']


def test_maximum_valid_strings_and_options_reach_review_and_publication_unchanged(source):
    p=proposal()
    p['clarification']={'question':'请说明目标条件。'+'甲'*792,'field':'f'*100,
                        'options':[f'{i}'+('站'*299) for i in range(4)]}
    # This is a scripted shape/preservation test, not source support assessment.
    assert len(p['clarification']['question'])==800
    g=Gateway(plan(),p,approved())
    status,result=run(source,g)
    assert status=='needs_clarification'
    assert g.calls[-1]['payload']['clarification']==p['clarification']==result['clarification']


@pytest.mark.parametrize('question', ['请提供需要统计的年份', '统计年份由你补充', 'Which reporting year should be used'])
def test_complete_open_question_needs_no_question_mark_or_enumeration(source,question):
    candidate={'question':question,'field':'year','options':[]}
    g=Gateway({'status':'needs_clarification','clarification':candidate},approved())
    status,result=run(source,g)
    assert status=='needs_clarification' and result['clarification']==candidate
    assert len(g.calls)==2


@pytest.mark.parametrize('field',cr.REVIEW_FIELDS)
@pytest.mark.parametrize('other_fields',['complete','missing','wrong_type'])
def test_every_explicit_false_vetoes_without_protocol_reapproval(source,field,other_fields):
    verdict=approved() if other_fields=='complete' else ({} if other_fields=='missing' else dict.fromkeys(cr.REVIEW_FIELDS,'true'))
    verdict[field]=False
    events=[];g=Gateway(plan(),proposal(),verdict)
    status,result=run(source,g,audit=events.append)
    assert status=='insufficient_evidence' and len(g.calls)==3 and 'clarification' not in result
    assert records(events)[-1]['record']['review'][field] is False
    assert result['diagnostics']['rejections'][-1]['stage']=='clarification_review'


@pytest.mark.parametrize('field',cr.REVIEW_FIELDS)
def test_each_missing_boolean_is_feedback_not_approval(source,field):
    verdict=approved();del verdict[field]
    g=Gateway(plan(),proposal(),verdict,proposal(),approved())
    status,result=run(source,g)
    assert status=='needs_clarification' and len(g.calls)==5
    feedback=g.calls[3]['payload']['last_clarification_review_repair']['feedback']
    assert feedback['validation_errors']==[{'field':field,'code':'missing_field'}]
    assert all(feedback['required_schema'][key]=='boolean' for key in cr.REVIEW_FIELDS)


def test_fake_option_is_not_derived_from_review_reason(source):
    p=proposal();p['clarification']['options']=['D01 北岸','D99 新站']
    verdict=approved(options_supported=False,reason='实际只有D01北岸和D02南岸，D99没有依据')
    g=Gateway(plan(),p,verdict)
    status,result=run(source,g)
    assert status=='insufficient_evidence' and 'clarification' not in result
    assert g.calls[-1]['payload']['clarification']['options']==['D01 北岸','D99 新站']
    assert g.calls[-1]['payload']['evidence'][0]['query_result']['rows']==[
        ['D01','重名点','北岸'],['D02','重名点','南岸']]
    assert len(g.calls)==3


def test_complete_question_cannot_pass_unnecessary_clarification(source):
    g=Gateway(plan(),proposal(),approved(clarification_supported=False,reason='原问题已经指定D01'))
    status,result=run(source,g)
    assert status=='insufficient_evidence' and len(g.calls)==3 and result['claims']==[]


def test_semantically_incomplete_but_not_dangling_question_requires_independent_usability(source):
    p=proposal();p['clarification'].update(question='数据库中存在两个不同对象。',options=[])
    assert cr.candidate_errors(p['clarification'])==[]
    g=Gateway(plan(),p,approved(question_answerable=False,requested_information_explicit=False))
    status,result=run(source,g)
    assert status=='insufficient_evidence' and 'clarification' not in result


def test_repeated_fragment_stops_at_existing_no_progress_limit_without_verify(source):
    p={'status':'needs_clarification','clarification':{'question':'数据库中有两个station_id不同但名称均为'}}
    g=Gateway(plan(),p,p,p)
    status,result=run(source,g)
    assert status=='insufficient_evidence' and result['verification']['reason']=='no_progress'
    assert len(g.calls)==4 and all(c['purpose']=='table' for c in g.calls)
    received=[event for event in result['diagnostics']['audit_history'] if event['kind']=='clarification_candidate_received']
    assert len(received)==3
    assert all(event['record']['candidate_identity']['sha256']==digest(p['clarification']) for event in received)
    assert all(event['record']['publication_status']=='audit_only' for event in received)


def test_unknown_post_after_invalid_candidate_is_not_replayed_and_raw_identity_survives(source):
    events=[];p={'status':'needs_clarification','clarification':{'question':''}}
    g=Gateway(plan(),p,ModelError('controlled unknown outcome','timeout'))
    with pytest.raises(ModelError):run(source,g,audit=events.append)
    assert len(g.calls)==3 and len(records(events))==1
    assert records(events)[0]['record']['candidate_identity']['sha256']==digest(p['clarification'])


def test_instruction_example_matches_all_four_required_booleans():
    example=cr.INSTRUCTIONS.split('结构为：',1)[1].split('。',1)[0]
    value=json.loads(example)
    assert cr.review_errors(value)==[] and cr.has_explicit_rejection(value)
    assert all(value[field] is False for field in cr.REVIEW_FIELDS)
