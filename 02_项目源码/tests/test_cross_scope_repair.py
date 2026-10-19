import copy
import sqlite3
import pytest
from backend.parsers import sqlite_tables
from backend.cross_source_engine import answer_cross_source, _claims, CrossSourceError
from backend.tool_progress import observed_month_like_feedback, unresolved_name_aggregation
from test_cross_business_repair import Gateway

@pytest.fixture
def stations(tmp_path):
    p=tmp_path/'stations.sqlite'
    with sqlite3.connect(p) as db:
        db.executescript('CREATE TABLE stations(station_id TEXT PRIMARY KEY,name TEXT,bank TEXT);'
            'CREATE TABLE inspections(station_id TEXT,period TEXT,checks INTEGER,unit TEXT);')
        db.executemany('INSERT INTO stations VALUES(?,?,?)',[('A01','同名站','东岸'),('B01','同名站','西岸')])
        db.executemany('INSERT INTO inspections VALUES(?,?,?,?)',[('A01','2025-02',30,'次'),('B01','2025-02',45,'次')])
    tables,warnings=sqlite_tables(p)
    return {'id':'s','name':'stations.sqlite','format':'sqlite','data_path':str(p),'sha256':'hash','parser_version':'p','tables':tables,'warnings':warnings,'metadata':{}}

AGG='SELECT SUM(i.checks) AS total_checks,i.unit FROM inspections i JOIN stations s ON i.station_id=s.station_id WHERE s.name=? GROUP BY i.unit'
def query():return {'actions':[{'id':'S1','kind':'sql','source_id':'s','sql':AGG,'parameters':['同名站']}]}
CLAIM={'status':'completed','claims':[{'text':'同名站巡检75次。','evidence_ids':['E1','E2']}]}

@pytest.mark.parametrize('claim_ids',[['E1','E2'],['E2']])
def test_homonym_sum_requires_selection_not_model_self_approval(stations,claim_ids):
    clarify={'status':'needs_clarification','clarification':{'question':'请选择东岸A01还是西岸B01？','field':'entity','options':['A01 东岸','B01 西岸']}}
    claim=copy.deepcopy(CLAIM);claim['claims'][0]['evidence_ids']=claim_ids
    g=Gateway([query(),claim,clarify,{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True}])
    status,r=answer_cross_source([stations],'同名站巡检多少次？',[],g)
    assert status=='needs_clarification' and r['claims']==[]
    issue=g.calls[2][1]['tool_errors'][-1]
    assert issue['kind']=='entity_selection' and issue['ambiguities'][0]['primary_key']==['station_id']
    assert r['evidence'][0]['entity_checks'][0]['matched_rows']==2

def test_explicit_all_stations_sum_keeps_existing_success(stations):
    g=Gateway([query(),CLAIM,{'supported':True,'answer_complete':True,'conflict':False,
        'claims':[{'id':'C1','supported':True}],'coverage':[{'question_id':'Q1','status':'answered','claim_ids':['C1']}]}])
    status,r=answer_cross_source([stations],'两站同名站的巡检合计多少次？',[],g)
    assert status=='completed' and '75' in r['text']

def test_name_ambiguity_does_not_treat_categories_or_repeated_same_pk_as_people():
    f={'status':'checked','column':'name','value':'同名站','table':'stations','primary_key':['id'],
       'matched_rows':2,'candidate_columns':['id','name','bank'],'candidates':[['A01','同名站','东岸'],['B01','同名站','西岸']],'truncated':False}
    assert unresolved_name_aggregation('同名站巡检多少',AGG,[f])
    assert unresolved_name_aggregation('东岸同名站巡检多少',AGG,[f])==[]
    assert unresolved_name_aggregation('同名站巡检多少',AGG,[{**f,'column':'country'}])==[]
    assert unresolved_name_aggregation('同名站巡检多少',AGG,[{**f,'candidates':[f['candidates'][0]]*2}])==[]
    assert unresolved_name_aggregation('分别列出同名站巡检次数',AGG,[f])
    assert unresolved_name_aggregation('不要全部合计同名站巡检次数',AGG,[f])
    assert unresolved_name_aggregation('不是全部同名站的合计',AGG,[f])
    separate='SELECT s.id,SUM(i.checks) FROM inspections i JOIN stations s ON i.station_id=s.id WHERE s.name=? GROUP BY s.id'
    assert unresolved_name_aggregation('分别列出同名站巡检次数',separate,[f])==[]
    assert unresolved_name_aggregation('分别列出同名站巡检次数',separate.replace('s.id,SUM','SUM'),[f])

@pytest.mark.parametrize('question',[
    '同名站巡检多少次？另查所有员工奖金合计。',
    '统计所有员工奖金合计；同名站巡检多少次？',
    '同名站巡检多少次？另查东岸员工奖金。',
])
def test_other_object_scope_cannot_authorize_or_select_homonym(question):
    f={'status':'checked','column':'name','value':'同名站','table':'stations','primary_key':['id'],
       'matched_rows':2,'candidate_columns':['id','name','bank'],
       'candidates':[['A01','同名站','东岸'],['B01','同名站','西岸']],'truncated':False}
    assert unresolved_name_aggregation(question,AGG,[f])
    assert unresolved_name_aggregation('所有同名站的巡检合计多少次？另查员工奖金。',AGG,[f])==[]

def test_composite_key_does_not_establish_multiple_entities():
    f={'status':'checked','column':'name','value':'同名站','table':'stations','primary_key':['id','period'],
       'matched_rows':2,'candidate_columns':['id','period','name'],
       'candidates':[['A01','2025-01','同名站'],['A01','2025-02','同名站']],'truncated':False}
    assert unresolved_name_aggregation('同名站全年巡检多少次？',AGG,[f])==[]

def test_negative_envelope_cannot_publish_ambiguous_aggregate(stations):
    wrong={'status':'insufficient_evidence','reason':'missing_evidence','claims':[
        {'text':'同名站巡检75次，但现有资料尚不足以确认其他要求。','evidence_ids':['E1','E2']}]}
    gap={'status':'insufficient_evidence','reason':'missing_evidence','claims':[
        {'text':'同名站对应不同对象，需先选择具体站点。','evidence_ids':['E1','E2']}]}
    g=Gateway([query(),wrong,gap,{'negative_supported':True,'conflict':False,'claims':[{'id':'C1','supported':True}]}])
    status,r=answer_cross_source([stations],'同名站巡检多少次？',[],g)
    assert status=='insufficient_evidence' and all('75' not in c['text'] for c in r['claims'])
    assert any(v['stage']=='negative_validation' and v['kind']=='entity_selection'
        for v in r['diagnostics']['rejections'])
    assert [role for role,_ in g.calls].count('verify')==1

def test_known_homonym_cannot_be_erased_by_removing_name_filter(stations):
    first={'actions':[{'id':'S1','kind':'sql','source_id':'s','sql':'SELECT station_id,name,bank FROM stations WHERE name=?','parameters':['同名站']}]}
    second={'actions':[{'id':'S2','kind':'sql','source_id':'s','sql':'SELECT SUM(checks) AS total FROM inspections','parameters':[]}]}
    claim={'status':'completed','claims':[{'text':'同名站巡检75次。','evidence_ids':['E2']}]}
    clarify={'status':'needs_clarification','clarification':{'question':'请选择A01东岸还是B01西岸？','field':'entity','options':['A01 东岸','B01 西岸']}}
    g=Gateway([first,second,claim,clarify,{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True}])
    status,r=answer_cross_source([stations],'同名站巡检多少次？',[],g)
    assert status=='needs_clarification' and r['claims']==[]
    assert g.calls[3][1]['tool_errors'][-1]['kind']=='entity_selection'

def test_newly_observed_homonym_also_constrains_prior_aggregate(stations):
    total={'actions':[{'id':'S1','kind':'sql','source_id':'s','sql':'SELECT SUM(checks) AS total FROM inspections','parameters':[]}]}
    names={'actions':[{'id':'S2','kind':'sql','source_id':'s','sql':'SELECT station_id,name,bank FROM stations WHERE name=?','parameters':['同名站']}]}
    claim={'status':'completed','claims':[{'text':'同名站巡检75次。','evidence_ids':['E1']}]}
    clarify={'status':'needs_clarification','clarification':{'question':'请选择A01东岸还是B01西岸？','field':'entity','options':['A01 东岸','B01 西岸']}}
    g=Gateway([total,names,claim,clarify,{'clarification_supported':True,'question_answerable':True,'requested_information_explicit':True,'options_supported':True}])
    status,r=answer_cross_source([stations],'同名站巡检多少次？',[],g)
    assert status=='needs_clarification'
    assert r['evidence'][0]['query_result']['rows']==[[75]]
    assert r['evidence'][0]['entity_checks'][0]['matched_rows']==2

def test_month_like_feedback_does_not_invent_year_or_rewrite_sql():
    sql='SELECT * FROM data WHERE 对象 LIKE \'%甲区%\' AND 时期 LIKE \'%1月%\''
    cat={'tables':[{'name':'data','details':{'column_profiles':[{'name':'时期','distinct_values':['2024-01','2025-01','2025-02']}]}}]}
    result=observed_month_like_feedback({'id':'s','sha256':'h','parser_version':'p'},sql,cat)
    assert result[0]['observed_same_month_candidates']==['2024-01','2025-01']
    assert result[0]['year_not_inferred'] is True
    assert '%1月%' in sql

def test_empty_date_mismatch_cannot_be_global_missing_source_claim():
    e={'id':'E2','kind':'table','query_result':{'columns':['amount'],'rows':[],'truncated':False},
       'empty_result_inspection':{'observed_date_spellings':[{'observed_same_month_candidates':['2025-01']}]}}
    with pytest.raises(CrossSourceError,match='不能说资料没有'):
        _claims({'claims':[{'text':'所给资料中未找到甲区1月收入数据。','evidence_ids':['E2']}]},{'E2':e},{},negative=True)
    # Narrow current-query scope is not rewritten into a global absence claim.
    assert _claims({'claims':[{'text':'当前查询未命中，请核对筛选条件。','evidence_ids':['E2']}]},{'E2':e},{},negative=True)
