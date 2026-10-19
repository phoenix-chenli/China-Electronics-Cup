"""Received interpretation corrections can be reviewed once without rerunning SQL."""
import copy
import json

import pytest

from backend.model_gateway import ModelError
from backend.query_planner import answer_database
from backend.sql_review_evidence import interpretation_recheck_allowed
from test_sql_review_evidence import source, Fake
from test_sql_answer_contract_repair import reviewed

SQL = 'SELECT SUM(amount_minor) AS known FROM metrics'


def plans():
    first={'status':'ready','normalized_question':'统计已记录的金额合计','intent':{'metric':'amount_minor求和'},'sql':SQL,'parameters':[]}
    corrected={'normalized_question':first['normalized_question'],'intent':{'metric':'金额合计'}}
    reject=reviewed(supported=False,interpretation_supported=False,failure_kind='invalid_plan',
        scope_review={'source_basis':True,'population_grain':True,'quantifier_scope':True,'missing_values':True,'output_contract':False},
        corrected_interpretation=corrected,issues=[{'id':'I1','dimension':'output_contract','kind':'business_interpretation','reason':'解释指标标签需修正'}])
    replacement={**first,**copy.deepcopy(corrected)}
    return first,reject,replacement


def previous(first,reject):
    return {'query':{'sql':first['sql'],'parameters':[],'plan':first,'review_response':reject,
                     'mechanical_contract':{'issues':[],'unresolved_issues':[]}}}


def test_exact_received_correction_is_eligible_but_does_not_approve():
    first,reject,replacement=plans();initial=previous(first,reject)
    assert interpretation_recheck_allowed(replacement,initial)
    assert reject['supported'] is False
    assert not interpretation_recheck_allowed(first,initial)


@pytest.mark.parametrize('mutation',['sql','parameters','interpretation','no_correction','no_change',
                                    'source_failure','null_failure','mechanical','issue_kind','issue_dimension'])
def test_other_semantic_or_structure_errors_do_not_use_same_sql_path(mutation):
    first,reject,replacement=plans();initial=previous(first,reject)
    if mutation=='sql':replacement['sql']='SELECT COUNT(*) AS known FROM metrics'
    elif mutation=='parameters':replacement['parameters']=[1]
    elif mutation=='interpretation':replacement['intent']={'metric':'订单数'}
    elif mutation=='no_correction':reject.pop('corrected_interpretation')
    elif mutation=='no_change':replacement=copy.deepcopy(first)
    elif mutation=='source_failure':reject['scope_review']['source_basis']=False
    elif mutation=='null_failure':reject['scope_review']['missing_values']=False
    elif mutation=='mechanical':initial['query']['mechanical_contract']['unresolved_issues']=[{'code':'unauthorized_nonnull_filter'}]
    elif mutation=='issue_kind':reject['issues'][0]['kind']='runtime_fact'
    elif mutation=='issue_dimension':reject['issues'][0]['dimension']='population_grain'
    assert not interpretation_recheck_allowed(replacement,initial)


def test_recheck_uses_executed_result_and_new_independent_review(source,monkeypatch):
    from backend import sql_engine
    execute=sql_engine.execute_sql;queries=[]
    def observed(s,sql,*args,**kwargs):
        if sql==SQL:queries.append(sql)
        return execute(s,sql,*args,**kwargs)
    monkeypatch.setattr(sql_engine,'execute_sql',observed)
    first,reject,replacement=plans();gateway=Fake(first,reject,replacement,reviewed())
    status,result=answer_database(source,'统计已记录的金额合计',[],gateway)
    assert status=='completed' and result['rows']==[[16]]
    assert queries==[SQL]
    assert [role for role,_ in gateway.calls]==['table','verify','table','verify']
    assert result['query']['interpretation_only_recheck']['execution_reused'] is True
    assert result['query']['plan']['intent']==replacement['intent']
    assert len(result['query']['semantic_reviews'])==2
    assert result['query']['semantic_reviews'][0]['review_response']['supported'] is False
    assert result['query']['semantic_reviews'][1]['review_response']['supported'] is True


def test_second_reviewer_false_is_never_flipped_or_retried_forever(source):
    first,reject,replacement=plans();gateway=Fake(first,reject,replacement,reject)
    status,result=answer_database(source,'统计已记录的金额合计',[],gateway)
    assert status=='failed' and not result['claims'] and not result.get('rows')
    assert len(gateway.calls)==4
    assert result['query']['semantic_recovery_stop']=='no_semantic_progress'


def test_new_review_unknown_outcome_is_not_retried(source):
    first,reject,replacement=plans();gateway=Fake(first,reject,replacement,ModelError('unknown POST outcome'))
    with pytest.raises(ModelError,match='unknown POST outcome'):
        answer_database(source,'统计已记录的金额合计',[],gateway)
    assert len(gateway.calls)==4
