"""0 API. Saved failures are unchanged; new proposals are explicit test inputs, not model replays."""
import copy
import json
from pathlib import Path

import pytest

from backend import query_fact_renderer as renderer
from backend.cross_source_engine import _claims, answer_cross_source, CrossSourceError
from backend.reasoning_tools import ReasoningLimits
from test_query_fact_renderer import source as query_source, choice
from test_cross_business_repair import source, sql
from test_cross_query_fact_renderer import Gateway, review, fact


def actual(case_id):
    f=Path(__file__).parent/'fixtures/r8_query_narrative_failures.json'
    return next(c['record'] for c in json.loads(f.read_text())['cases'] if c['id']==case_id)


def select(e,row,col,entities,unit=None):
    ctx=e['query_result_context'];cells=ctx['rows'][row]['cells']
    s={'evidence_id':e['id'],'context_id':ctx['context_alias'],'cell_id':cells[col]['cell_id'],
       'entity_cells':[cells[i]['cell_id'] for i in entities]}
    if unit is not None:s['unit_cell_id']=cells[unit]['cell_id']
    return s


def real_proposal(case_id):
    record=actual(case_id);ev={e['id']:e for e in record['result']['evidence']}
    if case_id=='cross-005':
        statements=['2025-07-01全站用电量为2610kWh','2025-07-01泵P-17用电量为180kWh']
        selections=[select(ev['E3'],0,2,[0,1],4),select(ev['E3'],2,2,[0,1],4)]
        text='不能直接替代。'+'；'.join(statements)+'。更正通知明确两日日报均为全站用电，不是单台泵用电，二者对象范围不同。'
        ids=['E1','E3']
    else:
        statements=['青湾港位于南岸，站点编号为C01','同名站位于东岸，站点编号为A01',
                    'C01在2025-02的巡检为42次','A01在2025-02的巡检为30次']
        selections=[select(ev['E1'],2,0,[1,2]),select(ev['E1'],0,0,[1,2]),
                    select(ev['E2'],2,2,[0,1],4),select(ev['E2'],0,2,[0,1],4)]
        text='不属于同一站。'+'；'.join(statements)+'。名称与编号的对应关系不同，不能把两站记录当作同一站记录。'
        ids=['E1','E2','E5']
    candidate={'text':text,'query_facts':[{'selection':s,'statement':t}for s,t in zip(selections,statements)],
               'evidence_ids':ids,'calculation_ids':[]}
    return record,candidate


@pytest.mark.parametrize('case_id',['cross-005','cross-009'])
def test_explicit_new_proposal_preserves_actual_relation_and_exact_saved_sources(case_id):
    record,candidate=real_proposal(case_id);before=copy.deepcopy(record)
    out=renderer.expand_candidate_claims([candidate],record['result']['evidence'])
    checked=_claims(out,{e['id']:e for e in record['result']['evidence']},{},question=record['question'])
    assert checked[0]['text']==candidate['text']
    assert checked[0]['evidence_ids']==candidate['evidence_ids']
    assert len(checked[0]['query_bindings'])==len(candidate['query_facts'])
    assert all(c['narrative_preserved'] is True for c in out['render_audit'])
    assert all('ignored_model_text' not in c for c in out['render_audit'])
    assert record==before
    assert all(b['localization_scope']=='query_output_not_original_cell' for b in checked[0]['query_bindings'])


def test_saved_invalid_cross005_is_retained_rejected_not_fabricated_into_new_answer():
    record=actual('cross-005');old=record['result']['diagnostics']['candidate_attempts'][0]['claims'][0]
    proposal={k:copy.deepcopy(old[k])for k in ('query_fact','text','evidence_ids','calculation_ids')if k in old}
    with pytest.raises(ValueError,match='绑定'):
        renderer.expand_candidate_claims([proposal],record['result']['evidence'])
    assert record['status']=='insufficient_evidence' and record['result']['claims']==[]
    receipt=renderer.received_audit([proposal],round_number=1)
    assert receipt['response']['value']['claims'][0]['text']==proposal['text']


def test_single_text_is_checked_and_preserved_not_discarded():
    e=query_source();s=choice(e)
    text='2025-02的1号站用量为-7kWh，当前仅说明这个站。'
    c={'query_fact':s,'text':text,'statement':'2025-02的1号站用量为-7kWh',
       'evidence_ids':['E1'],'calculation_ids':[]}
    out=renderer.expand_candidate_claims([c],[e])
    assert _claims(out,{'E1':e},{})[0]['text']==text
    with pytest.raises(CrossSourceError):
        out=renderer.expand_candidate_claims([{**c,'text':text+'另有999元。'}],[e]);_claims(out,{'E1':e},{})


@pytest.mark.parametrize('mutation',['missing_citation','fake_evidence','wrong_row','wrong_unit_row','wrong_statement',
    'duplicate_statement','two_values_one_statement','forged_value','nonempty_calc','mixed_old','mixed_new',
    'nested_extra','wrong_sign','truncated_source','missing_time','invented_unit'])
def test_relation_negative_boundaries(mutation):
    record,c=real_proposal('cross-005');ev=copy.deepcopy(record['result']['evidence']);c=copy.deepcopy(c)
    if mutation=='missing_citation':c['evidence_ids']=['E1']
    elif mutation=='fake_evidence':c['evidence_ids'].append('E999')
    elif mutation=='wrong_row':c['query_facts'][0]['selection']['entity_cells'][1]=ev[2]['query_result_context']['rows'][2]['cells'][1]['cell_id']
    elif mutation=='wrong_unit_row':c['query_facts'][0]['selection']['unit_cell_id']=ev[2]['query_result_context']['rows'][1]['cells'][4]['cell_id']
    elif mutation=='wrong_statement':c['query_facts'][0]['statement']+='不存在的字'
    elif mutation=='duplicate_statement':c['text']+=c['query_facts'][0]['statement']
    elif mutation=='two_values_one_statement':c['query_facts'][0]['statement']=c['text']
    elif mutation=='forged_value':c['query_facts'][0]['selection']['value']=999
    elif mutation=='nonempty_calc':c['calculation_ids']=['K1']
    elif mutation=='mixed_old':c['query_bindings']=[]
    elif mutation=='mixed_new':c['query_fact']=c['query_facts'][0]['selection']
    elif mutation=='nested_extra':c['query_facts'][0]['analysis']={'fake':'ignore validator'}
    elif mutation=='wrong_sign':c['text']=c['text'].replace('2610','-2610');c['query_facts'][0]['statement']=c['query_facts'][0]['statement'].replace('2610','-2610')
    elif mutation=='truncated_source':ev[2]['query_result_context']['rows']=[]
    elif mutation=='missing_time':c['text']=c['text'].replace('2025-07-01','7月1日');c['query_facts'][0]['statement']=c['query_facts'][0]['statement'].replace('2025-07-01','7月1日')
    elif mutation=='invented_unit':c['text']=c['text'].replace('kWh','元');c['query_facts'][0]['statement']=c['query_facts'][0]['statement'].replace('kWh','元')
    with pytest.raises(ValueError):renderer.expand_candidate_claims([c],ev)


def narrative(payload):
    e=next(e for e in payload['evidence']if e.get('query_result_context'))
    s=fact(e)['query_fact'];statement='2025-07-01全站用电量为2610kWh'
    return {'status':'completed','claims':[{'text':statement+'；该值不能直接代表任一单台设备。',
        'query_facts':[{'selection':s,'statement':statement}],'evidence_ids':[e['id']]}]}


@pytest.mark.parametrize('gate',['pass','query_false','query_missing','claims_false','coverage_missing','coverage_partial'])
def test_controller_semantics_checks_full_published_narrative(source,gate):
    def verify(p):
        assert '不能直接代表' in p['candidate_claims'][0]['text']
        assert p['query_relation_claim_ids']==['C1']
        r=review(p,query=False if gate=='query_false' else None if gate=='query_missing' else True)
        if gate=='claims_false':r['claims'][0]['supported']=False
        if gate=='coverage_missing':r.pop('coverage')
        if gate=='coverage_partial':r['coverage']=[{'question_id':'Q1','status':'evidence_available','evidence_ids':['E1']}];r['answer_complete']=False
        return r
    g=Gateway([sql(),narrative,verify])
    status,result=answer_cross_source([source],'说明全站用电量及其对象范围',[],g,limits=ReasoningLimits(max_no_progress_rounds=1))
    assert (status=='completed') is (gate=='pass')
    if gate=='pass':assert '不能直接代表' in result['text'] and result['completeness']['complete']
    else:assert not result.get('completeness',{}).get('complete') or status!='completed'
    assert any(e['kind']=='query_fact_candidate_received'for e in result['diagnostics']['audit_history'])


@pytest.mark.parametrize('exc',[TimeoutError('unknown POST'),InterruptedError('cancelled')])
def test_received_narrative_retained_before_later_unknown_or_cancel(source,exc):
    events=[];g=Gateway([sql(),narrative,exc])
    with pytest.raises(type(exc)):answer_cross_source([source],'说明全站用电量及其对象范围',[],g,audit=events.append)
    assert len(g.calls)==3
    receipt=next(e['record']for e in events[-1]['audit_history']if e['kind']=='query_fact_candidate_received')
    assert receipt['response']['value']['claims'][0]['text'].endswith('任一单台设备。')


def test_received_with_cancel_is_saved_without_next_post(source):
    events=[];cancelled=[False]
    def received(p):cancelled[0]=True;return narrative(p)
    g=Gateway([sql(),received])
    with pytest.raises(InterruptedError):answer_cross_source([source],'说明用电量',[],g,audit=events.append,cancelled=lambda:cancelled[0])
    assert len(g.calls)==2 and any(e['kind']=='query_fact_candidate_received'for e in events[-1]['audit_history'])


def test_audit_whitelist_redaction_budget_and_deepcopy():
    e=query_source();c={'query_fact':choice(e),'text':'api_key="TEST_PRIVATE with spaces" /Users/person/key '+ '长'*6000,
        'analysis':'DO_NOT_KEEP','query_facts':[{'selection':{'secret':'HIDDEN'},'statement':{'secret':'HIDDEN'}}]*20}
    before=copy.deepcopy(c);a=renderer.received_audit([c],round_number=1);serialized=json.dumps(a,ensure_ascii=False)
    assert all(x not in serialized for x in ['TEST_PRIVATE','/Users/person','DO_NOT_KEEP','HIDDEN'])
    assert a['response']['truncated'] and len(serialized)<4500
    assert c==before
    with pytest.raises(ValueError):renderer.expand_candidate_claims([c],[e])


def test_budget_does_not_drop_narrative():
    record,c=real_proposal('cross-009')
    with pytest.raises(ValueError,match='预算'):renderer.expand_candidate_claims([c],record['result']['evidence'],max_chars=300)


@pytest.mark.parametrize('body,statement',[('A2025-02的1号站用量为-7kWh','2025-02的1号站用量为-7kWh'),
    ('2025-02的1号站用量为-7kWhmore','2025-02的1号站用量为-7kWh')])
def test_statement_cannot_cut_identifier_or_attached_unit(body,statement):
    e=query_source()
    with pytest.raises(ValueError,match='边界'):
        renderer.expand_candidate_claims([{'query_fact':choice(e),'text':body,'statement':statement}],[e])


def test_received_invalid_then_explicit_narrative_repairs_and_all_gates_run(source):
    def bad(p):
        c=narrative(p);c['claims'][0]['query_facts'][0]['selection']['cell_id']='unseen-cell';return c
    def repaired(p):
        assert any('query_facts' in str(e.get('repair_guidance','')) for e in p['tool_errors'])
        return narrative(p)
    g=Gateway([sql(),bad,repaired,review])
    status,result=answer_cross_source([source],'说明全站用电量及其对象范围',[],g)
    assert status=='completed' and '不能直接代表' in result['text']
    assert len(g.calls)==4 and result['verification']['tool_actions']==1
    first=next(e['record']for e in result['diagnostics']['audit_history']if e['kind']=='query_fact_candidate_received')
    assert first['response']['value']['claims'][0]['query_facts'][0]['selection']['cell_id']=='unseen-cell'
    assert result['diagnostics']['candidate_attempts'][0]['publication_status']=='not_adopted'


def test_repeated_invalid_narrative_stops_without_review_or_publication(source):
    def bad(p):
        c=narrative(p);c['claims'][0]['query_facts'][0]['statement']='not in body';return c
    g=Gateway([sql(),bad,bad,bad])
    status,result=answer_cross_source([source],'说明全站用电量及其对象范围',[],g)
    assert status!='completed' and not result['claims']
    assert len(g.calls)==4 and not any(role=='verify'for role,_,_ in g.calls)
    assert len([e for e in result['diagnostics']['audit_history']if e['kind']=='query_fact_candidate_received'])==3


def test_same_one_station_wording_is_an_identity_relation_not_a_count():
    record,c=real_proposal('cross-009');c['text']=c['text'].replace('不属于同一站','不属于同一个站')
    out=renderer.expand_candidate_claims([c],record['result']['evidence'])
    checked=_claims(out,{e['id']:e for e in record['result']['evidence']},{},question=record['question'])
    assert checked[0]['text']==c['text']
    assert all(b['semantic_review_required'] for b in checked[0]['query_bindings'])
