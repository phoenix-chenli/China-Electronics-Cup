"""Candidate-only offline repair contracts. No model/network calls."""
import copy
import json
import pytest
from backend.cross_source_engine import (_binding_repair_contract, _candidate_mechanical_ranks,
    _MechanicalRepairProgress, _claims, answer_cross_source, CrossSourceError)
from backend.table_context import context_for_table
from backend.table_bindings import check_table_bindings
from backend.model_gateway import ModelError
from test_cross_source_engine import sources, structured_pdf_table, structured_table_answer, Responses, actions, sql, table_approved


def record(sources):
    structured_pdf_table(sources)
    return {'E1': {'id':'E1','kind':'text','source_id':'db','sha256':'dbhash','parser_version':'test',
        'quote':'1 | 林禾 | 12 | 12', 'table_context':context_for_table(sources[0],'orders',[2])}}


def test_unsupported_cells_and_row_header_get_exact_required_schema_and_real_cell_templates(sources):
    evidence=record(sources)
    candidate={'text':'林禾的金额为12万元。','evidence_ids':['E1'],
        'table_bindings':[{'evidence_id':'E1','cells':[{'row':2,'col':3,'text':'12'}]},
                          {'evidence_id':'E1','row_header':'林禾','column_header':'金额','value':'12'}]}
    before=copy.deepcopy((candidate,evidence))
    feedback=_binding_repair_contract(candidate,evidence,2)
    assert feedback['candidate_claim_id']=='C2'
    assert set(feedback['binding_diagnostics'][0]['missing_required_fields'])=={'table_name','cell_id','value','statement','column_label','row_label'}
    assert feedback['binding_diagnostics'][0]['unsupported_alias_fields']==['cells']
    assert feedback['source_cell_examples']
    assert len(json.dumps(feedback,ensure_ascii=False))<=6500
    for example in feedback['source_cell_examples']:
        claim=example['claim_template'];binding=claim['table_bindings'][0]
        assert set(feedback['required_binding_keys'])<=set(binding)
        assert binding['cell_id'] in evidence['E1']['table_context']['tables'][0]['selected_cell_ids']
        assert check_table_bindings(claim['text'],claim['table_bindings'],list(evidence.values()))['valid']
        assert example['publication_status']=='protocol_example_not_verified'
    assert (candidate,evidence)==before


def test_bad_or_unknown_table_scope_never_creates_source_templates(sources):
    evidence=record(sources);evidence['E1']['table_context']['unresolved_structure']=True
    assert _binding_repair_contract(structured_table_answer()['claims'][0],evidence,1)['source_cell_examples']==[]
    candidate=structured_table_answer(evidence_id='E999')['claims'][0];candidate['evidence_ids']=['E999']
    assert _binding_repair_contract(candidate,evidence,1)['source_cell_examples']==[]


def test_mixed_unit_annotation_remains_source_context_not_one_cell_unit(sources):
    evidence=record(sources)
    table=evidence['E1']['table_context']['tables'][0]
    table['units']=[{'text':'单位：亿元、%','value':'亿元、%','scope':'table_candidate',
        'evidence':{'page':1,'start':0,'end':8}}]
    feedback=_binding_repair_contract(structured_table_answer()['claims'][0],evidence,1)
    assert feedback['source_cell_examples']
    for example in feedback['source_cell_examples']:
        claim=example['claim_template'];binding=claim['table_bindings'][0]
        assert 'unit' not in binding
        assert example['unit_source_annotations']==table['units']
        assert check_table_bindings(claim['text'],claim['table_bindings'],list(evidence.values()))['valid']
    assert table['units'][0]['value']=='亿元、%'


def test_progress_has_finite_high_water_not_prose_or_oscillation(sources):
    evidence=record(sources);valid=structured_table_answer();tracker=_MechanicalRepairProgress()
    one=copy.deepcopy(valid);one['claims'][0]['table_bindings']=[{'evidence_id':'E1','cells':[]}]
    rank=_candidate_mechanical_ranks(one,evidence,{},'',{});assert not tracker.observe(rank)
    two=copy.deepcopy(valid);two['claims'][0]['table_bindings'][0]['statement']='不在正文中的子句'
    rank2=_candidate_mechanical_ranks(two,evidence,{},'',{});assert tracker.observe(rank2)
    changed=copy.deepcopy(two);changed['claims'][0]['text']+='额外修辞。'
    assert not tracker.observe(_candidate_mechanical_ranks(changed,evidence,{},'',{}))
    assert not tracker.observe(rank)
    assert not tracker.observe(rank2)
    assert tracker.observe(_candidate_mechanical_ranks(valid,evidence,{},'',{}))
    assert not tracker.observe([100]*1)
    assert tracker.advances==2


def test_more_than_three_bad_candidates_can_repair_with_real_monotonic_progress(sources):
    structured_pdf_table(sources)
    one=structured_table_answer();one['claims'][0]['table_bindings']=[{'evidence_id':'E1','cells':[]}]
    two=structured_table_answer();two['claims'][0]['table_bindings'][0].pop('cell_id')
    three=structured_table_answer(value='999')
    four=structured_table_answer(statement='林禾的金额不是上述句子。')
    corrected=structured_table_answer()
    gateway=Responses(actions(sql(sql='SELECT customer,amount FROM orders WHERE id=1',parameters=[])),one,two,three,four,corrected,table_approved())
    status,result=answer_cross_source(sources,'林禾的金额是多少？',[],gateway)
    assert status=='completed' and len(gateway.calls)==7
    assert gateway.calls[-1]['purpose']=='verify'
    assert result['claims'][0]['table_bindings'][0]['value']=='12'
    progress=result['verification']['planning_progress']
    assert progress['mechanical_repair_advances']>=3
    assert all('last_table_binding_repair' in call['payload'] for call in gateway.calls[2:] if call['purpose']=='table')
    assert 'last_table_binding_repair' not in gateway.calls[-1]['payload']
    assert result['diagnostics']['rejections'] # previous rejected candidates remain auditable


def test_repeated_invalid_candidate_stops_without_verification(sources):
    structured_pdf_table(sources);bad=structured_table_answer(value='999')
    gateway=Responses(actions(sql(sql='SELECT customer,amount FROM orders WHERE id=1',parameters=[])),bad,bad,bad)
    status,result=answer_cross_source(sources,'林禾金额',[],gateway)
    assert status!='completed' and result['claims']==[] and len(gateway.calls)==4
    assert all(c['purpose']=='table' for c in gateway.calls)
    assert result['verification']['planning_progress']['mechanical_repair_advances']==0


def test_repaired_protocol_does_not_bypass_independent_table_review(sources):
    structured_pdf_table(sources);good=structured_table_answer()
    bad=copy.deepcopy(good);bad['claims'][0]['table_bindings'][0].pop('cell_id')
    denied={**table_approved(), 'supported':False, 'claims':[{'id':'C1','supported':False}], 'table_relations':[{'id':'C1','supported':False}]}
    gateway=Responses(actions(sql(sql='SELECT customer,amount FROM orders WHERE id=1',parameters=[])),bad,good,denied,good,denied)
    status,result=answer_cross_source(sources,'林禾金额',[],gateway)
    assert status!='completed' and result['claims']==[]
    assert sum(c['purpose']=='verify' for c in gateway.calls)==2


def test_repair_unknown_transport_outcome_propagates_without_retry(sources):
    structured_pdf_table(sources);bad=structured_table_answer(value='999');calls=[]
    def gateway(messages,purpose):
        calls.append(purpose)
        if len(calls)==1:return actions(sql(sql='SELECT customer,amount FROM orders WHERE id=1',parameters=[]))
        if len(calls)==2:return bad
        raise ModelError('unknown outcome',code='network_error')
    with pytest.raises(ModelError):answer_cross_source(sources,'金额',[],gateway)
    assert len(calls)==3
