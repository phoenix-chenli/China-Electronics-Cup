"""Controlled role/lineage counterexamples; no model or network calls."""
import copy
import pytest
from backend.suggestion_service import candidate_suggestions
from backend.suggestion_grounding import measure_indexes


@pytest.fixture
def case():
    source={'id':'s','sha256':'a'*64,'parser_version':'v1','revision':'r1','semantic_revision':'m1'}
    answer={'id':'answer','project_id':'project','status':'completed','question':'列出已核对的结果',
            'sources':[source],'context_state':{'slots':{}},'result':{
            'claims':[{'id':'C1','text':'A金额为10元，B金额为7元。','evidence_ids':['E1']}],
            'evidence':[{'id':'E1','source_id':'s'}],'verification':{'status':'passed'},
            'columns':['客户编号','金额'],'rows':[[1,10],[2,7]],'query':{'sql':'SELECT id,amount FROM customers'}}}
    capabilities={'local_actions':['visualize_result','explain_scope','verify_sources'],
        'catalogs':[{'source_snapshot':source,'semantic_layer':{'source_sha256':source['sha256']},
            'tables':[{'name':'customers','primary_key':['id'],'columns':[
                {'name':'id','type':'INTEGER','primary_key_position':1},
                {'name':'amount','type':'REAL'}, {'name':'name','type':'TEXT'}, {'name':'category_code','type':'TEXT'},
                {'name':'owner','type':'INTEGER'}]},
                {'name':'albums','primary_key':['id'],'columns':[{'name':'id','type':'INTEGER','primary_key_position':1},{'name':'title','type':'TEXT'}]}],
            'relationships':[{'table':'customers','columns':['owner'],'referenced_table':'albums','referenced_columns':['id']}]}]}
    return answer,capabilities


def test_numeric_axis_contains_measure_and_never_primary_key(case):
    answer,caps=case
    suggestions=candidate_suggestions(answer,caps)
    chart=next(s for s in suggestions if s['action']=='visualize_result')
    comparison=next(s for s in suggestions if s['kind']=='compare_result')
    assert chart['payload']['numeric_column_indexes']==comparison['payload']['numeric_column_indexes']==[1]


@pytest.mark.parametrize('sql,name,rows',[
    ('SELECT id AS salary FROM customers','salary',[[1],[2]]),
    ('SELECT CAST(id AS REAL) AS salary FROM customers','salary',[[1.0],[2.0]]),
    ('SELECT owner AS amount FROM customers','amount',[[1],[2]]),
    ('SELECT name FROM customers','name',[['1'],['2']]),
    ('SELECT category_code FROM customers','category_code',[['001'],['002']]),
    ('SELECT SUM(id) AS amount FROM customers','amount',[[3]]),
])
def test_identifiers_or_numeric_looking_text_cannot_be_measure_under_alias_or_cast(case,sql,name,rows):
    answer,caps=case;answer['result'].update(columns=[name],rows=rows,query={'sql':sql})
    suggestions=candidate_suggestions(answer,caps)
    assert not any(s['kind'] in {'visualize_result','compare_result','explain_result_scope'} for s in suggestions)
    assert any(s['action']=='verify_sources' for s in suggestions)


def test_count_identifiers_is_a_real_measure_and_scalar_scope_question_keeps_provenance(case):
    answer,caps=case;answer['question']='客户数量是多少？'
    answer['result'].update(columns=['客户数量'],rows=[[2]],query={'sql':'SELECT COUNT(id) AS 客户数量 FROM customers'})
    suggestions=candidate_suggestions(answer,caps)
    ask=next(s for s in suggestions if s['action']=='ask')
    assert ask['kind']=='explain_result_scope' and '客户数量' in ask['text']
    assert ask['payload']['evidence_ids']==['E1'] and ask['payload']['new_facts_asserted'] is False
    assert not any('albums' in s['text'] or s['kind'] in {'breakdown','trend'} for s in suggestions)


def test_changed_catalog_snapshot_does_not_supply_metric_roles(case):
    answer,caps=case;caps=copy.deepcopy(caps);caps['catalogs'][0]['source_snapshot']['revision']='other'
    assert measure_indexes(answer,caps,{})==[]


@pytest.mark.parametrize('entities,text',[
    (['试验站','夜班'],'试验站夜班交接为21:30。'),
    (['设备','值班人员'],'设备复机需要审批，值班人员不得单独批准。'),
    (['手册','成功启动'],'手册记录成功启动18次。'),
    (['断面','独立水样'],'断面取3个独立水样。'),
])
def test_document_cooccurrence_is_not_comparability_or_new_numeric_metric(case,entities,text):
    answer,caps=case;answer['result'].pop('columns');answer['result'].pop('rows');answer['result'].pop('query')
    answer['result']['claims'][0]['text']=text
    answer['context_state']['slots']={'entities':{'value':entities,'verification':'answer_semantic_review'}}
    assert not any(s['action']=='ask' for s in candidate_suggestions(answer,caps))


def test_already_requested_scope_does_not_repeat_the_scalar_scope_question(case):
    answer,caps=case;answer['question']='客户数量和计算口径是什么？'
    answer['result'].update(columns=['客户数量'],rows=[[2]],query={'sql':'SELECT COUNT(id) AS 客户数量 FROM customers'})
    assert not any(s['kind']=='explain_result_scope' for s in candidate_suggestions(answer,caps))


@pytest.mark.parametrize('state_marker', ['unresolved','unknown_fields'])
def test_conflicted_or_unknown_context_is_not_reused_as_a_suggestion_anchor(case,state_marker):
    answer,caps=case
    for key in ('columns','rows','query'):answer['result'].pop(key)
    answer['result']['claims'][0]['text']='金额为10元。'
    answer['context_state']['slots']={'metric':{'value':'金额','confirmed_by_user':True}}
    assert any(s['kind']=='inspect_metric_scope' for s in candidate_suggestions(answer,caps))
    answer['context_state'][state_marker]=([{'field':'metric','kind':'interpretation_differs_from_user_patch'}]
        if state_marker=='unresolved' else {'metric':{'verification':'field_not_verified_for_current_question'}})
    suggestions=candidate_suggestions(answer,caps)
    assert not any(s['kind'] in {'inspect_metric_scope','explain_scope'} for s in suggestions)
    assert any(s['action']=='verify_sources' for s in suggestions)


def test_async_real_service_keeps_sql_roles_but_not_query_audit_or_secrets(case,tmp_path):
    import json,time
    from backend.store import Store
    from backend.suggestion_service import SuggestionService
    answer,caps=case;store=Store(tmp_path/'runtime');store.initialize()
    answer['project_id']=store.create_project('test','')['id'];answer['context_revision']='v1'
    answer['result']['query']['repairs']=[{'secret':'must-not-leak'}]
    payloads=[]
    def fake_factory(_cancelled):
        def rank(messages,_role):
            payload=json.loads(messages[1]['content']);payloads.append(payload)
            return {'ranked_ids':[c['id'] for c in payload['candidates']]}
        return rank
    service=SuggestionService(store,gateway_factory=fake_factory);service.start()
    try:
        item=service.submit(answer,capabilities=caps)
        deadline=time.monotonic()+3
        while item['state']=='preparing' and time.monotonic()<deadline:
            time.sleep(.005);item=service.get(answer['project_id'],item['key'])
        chart=next(s for s in item['suggestions'] if s['action']=='visualize_result')
        assert chart['payload']['numeric_column_indexes']==[1]
        assert any(s['kind']=='compare_result' for s in item['suggestions'])
        assert 'must-not-leak' not in json.dumps(payloads) and 'SELECT' not in json.dumps(payloads)
    finally:service.close()
