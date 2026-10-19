"""Real saved failure shape + local SQLite/scripted gateway; zero model API."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from decimal import Decimal

import pytest

from backend import cross_source_engine as cross
from backend.empty_entity_inspection import needs_text_inspection, text_filter_feedback, MAX_FEEDBACK_CHARS
from backend.parsers import parse, sqlite_tables
from backend.reasoning_tools import ReasoningLimits
from backend.schema_catalog import build_catalog, SchemaCatalogError
from backend.sql_engine import execute_sql
from backend.tool_progress import overstrong_empty_claim
from backend.model_gateway import ModelError
from backend.formula_engine import calculate_formula, FormulaError
from test_cross_business_repair import Gateway, review

FIXTURES = Path(__file__).parent / 'fixtures'
SAVED = json.loads((FIXTURES / 'r9-cross006-received.json').read_text())
SQL = SAVED['steps'][0]['query']['sql']


@pytest.fixture
def tariff(tmp_path):
    original = FIXTURES / 'energy_tariff.csv'
    result = parse(original, 'csv', tmp_path / 'tariff.sqlite', lambda *a: None)
    return dict(result, id='s', name=original.name, format='csv',
                sha256=hashlib.sha256(original.read_bytes()).hexdigest(), parser_version='test-frozen')


def hints(source, sql=SQL, parameters=None, question=None):
    return text_filter_feedback(source, sql, parameters or ['P-17'], question or SAVED['question'],
                                build_catalog(source, ['数据表']))


def action(identifier='S1', value='P-17'):
    return {'actions': [{'id': identifier, 'kind': 'sql', 'source_id': 's', 'sql': SQL, 'parameters': [value]}]}


def test_saved_failure_selects_actual_column_and_full_user_text_without_rewrite(tariff):
    assert tariff['sha256'] == SAVED['source_sha256']['tariff']
    parameters = ['P-17']; original_sql = SQL
    assert execute_sql(tariff, SQL, parameters)['rows'] == []
    result = hints(tariff, parameters=parameters)
    item = result['filters'][0]
    assert (item['table'], item['column'], item['requested_value']) == ('数据表', '对象', 'P-17')
    assert item['candidates'] == [{'value': '泵P-17', 'relations': [
        'filter_text_is_substring_not_entity_equivalence', 'full_sample_text_occurs_in_user_question'],
        'entity_equivalence_verified': False}]
    assert result['sha256'] == tariff['sha256'] and result['source_id'] == 's'
    assert result['query_changed'] is False and result['absence_proven'] is False
    assert parameters == ['P-17'] and SQL == original_sql
    # Only a separate, explicit exact query produces the row used in arithmetic.
    assert execute_sql(tariff, SQL, ['泵P-17'])['rows'][0][2:4] == ['180', '0.65']


def test_received_wrong_absence_is_rejected_without_inventing_old_feedback():
    empty = SAVED['evidence'][0]
    assert 'empty_result_inspection' not in empty
    assert overstrong_empty_claim(SAVED['claims'][0]['text'], SAVED['evidence']) == ['E1']
    with pytest.raises(cross.CrossSourceError, match='本次筛选未命中'):
        cross._claims({'claims': SAVED['claims']}, {e['id']: e for e in SAVED['evidence']}, {},
                      negative=True, question=SAVED['question'])


@pytest.mark.parametrize('text', [
    '本次查询未找到匹配记录，请核对筛选条件。',
    '当前查询未命中，尚不能确认是否有相关记录。',
    '本次查询未找到资料中的相关记录。',
    '不能据此证明资料不存在对应记录。',
    '尚不能确认数据库是否没有相关记录。',
    'revision_note.pdf未提供电价。',
])
def test_narrow_miss_or_other_named_document_limitation_is_not_source_absence(text):
    assert overstrong_empty_claim(text, SAVED['evidence']) == []


@pytest.mark.parametrize('text', [
    'energy_tariff.csv中无对象为P-17的记录。',
    '资料未提供对应对象数据。',
    '本次查询未命中，原文件不存在对应记录。',
    '本次查询证明数据库没有记录。',
    '没有该对象的记录。',
    '文件中无该对象的记录。',
    '不能据此证明资料没有记录，但资料确实不存在该对象的记录。',
    '不能证明资料没有记录而资料确实没有对应记录。',
])
def test_all_empty_queries_do_not_prove_source_absence_even_without_samples(text):
    for inspection in [None, {'status': 'not_run_budget'}, {'status': 'sampled', 'filters': []},
                       {'status': 'sampled', 'sample_truncated': True}]:
        e = copy.deepcopy(SAVED['evidence'][0])
        e['empty_result_inspection'] = inspection
        assert overstrong_empty_claim(text, [e]) == ['E1']


def test_or_cte_named_placeholders_use_lexical_binding_order_not_ast_order(tariff):
    # The two OR predicates remain independent; neither proves a whole-query match.
    sql = 'SELECT * FROM "数据表" WHERE "对象"=? OR ("时段"=? AND "对象"=?)'
    r = hints(tariff, sql, ['P-17', '2025-07-02', '全站'])
    assert [(f['column'], f['requested_value']) for f in r['filters']] == [
        ('对象', 'P-17'), ('时段', '2025-07-02'), ('对象', '全站')]
    assert all(f['predicate_scope'] == 'independent_not_complete_query' for f in r['filters'])
    sql = 'WITH x AS (SELECT * FROM "数据表" WHERE "对象"=?) SELECT * FROM "数据表" WHERE "时段"=?'
    r = hints(tariff, sql, ['P-17', '2025-07-02'])
    assert {(f['column'], f['requested_value']) for f in r['filters']} == {('对象', 'P-17'), ('时段', '2025-07-02')}
    r = hints(tariff, 'SELECT * FROM "数据表" WHERE "对象"=:object OR "时段"=:period',
              {'object': 'P-17', 'period': '2025-07-02'})
    assert {(f['column'], f['requested_value']) for f in r['filters']} == {('对象', 'P-17'), ('时段', '2025-07-02')}


def test_no_match_oversize_truncated_and_many_matches_cannot_become_evidence(tariff):
    r = hints(tariff, parameters=['P-99'], question='查P-99。')
    assert r['filters'][0]['candidates'] == [] and r['absence_proven'] is False
    cat = build_catalog(tariff, ['数据表'])
    profile = next(p for p in cat['tables'][0]['details']['column_profiles'] if p['name'] == '对象')
    profile['distinct_values'] = ['甲泵P-17', '乙泵P-17', 'P-170', 'P-171', 'P-172', '超长' + 'a'*200]
    cat['tables'][0]['details']['sample_truncated'] = True
    r = text_filter_feedback(tariff, SQL, ['P-17'], '查P-17。', cat)
    assert r['truncated'] and r['filters'][0]['multiple_sample_candidates']
    assert len(r['filters'][0]['candidates']) == 4
    assert all(c['entity_equivalence_verified'] is False for c in r['filters'][0]['candidates'])
    assert all('full_sample_text_occurs_in_user_question' not in c['relations'] for c in r['filters'][0]['candidates'])
    assert len(json.dumps(r, ensure_ascii=False)) <= MAX_FEEDBACK_CHARS
    r = hints(tariff, parameters=['X'*1000], question='查某对象')
    assert r['filters'] == [] and r['omitted_filters'] == 1 and r['truncated']


@pytest.mark.parametrize('sql,parameters', [
    ('SELECT "对象"=? FROM "数据表"', ['P-17']),
    ('SELECT * FROM "数据表" WHERE NOT "对象"=?', ['P-17']),
    ('SELECT * FROM "数据表" WHERE "对象"=?2', ['x','P-17']),
])
def test_untrusted_filter_shapes_are_not_silently_reinterpreted(tariff, sql, parameters):
    assert not needs_text_inspection(sql, parameters)
    r = hints(tariff, sql, parameters)
    assert not r['filters']


def test_ambiguous_field_or_derived_alias_does_not_get_first_table_sample(tariff):
    sql = 'SELECT a."对象" FROM "数据表" a JOIN "数据表" b ON a."时段"=b."时段" WHERE "对象"=?'
    r = hints(tariff, sql)
    assert r['filters'][0]['status'] == 'unsupported' and not r['filters'][0]['candidates']
    sql = 'WITH x AS (SELECT * FROM "数据表") SELECT * FROM x WHERE "对象"=?'
    r = hints(tariff, sql)
    assert r['filters'][0]['status'] == 'unsupported' and not r['filters'][0]['candidates']


def test_real_controller_requeries_then_calculates_with_independent_operation_review(tariff):
    calculate = {'actions': [{'id':'S3','kind':'calculate','expression':'q*p','output_unit':'元',
        'operation': {'kind':'quantity_times_unit_price','pairs':[{'quantity':'q','unit_price':'p'}]},
        'bindings': {'q': {'evidence_id':'E2','row':0,'column':'用电量','unit':'kWh','unit_column':'计量单位'},
                     'p': {'evidence_id':'E2','row':0,'column':'电价','unit':'元/kWh','unit_column':'价格单位'}}}]}
    g = Gateway([action(), action('S2','泵P-17'), calculate,
        {'status':'completed','claims':[{'text':'泵P-17不含税电费为117元。','calculation_ids':['K1']}]}, review()])
    status, result = cross.answer_cross_source([tariff], SAVED['question'], [], g)
    assert status == 'completed'
    assert Decimal(result['calculations'][0]['value']) == Decimal('117')
    assert result['steps'][0]['query']['parameters'] == ['P-17']
    assert result['steps'][0]['query']['rows'] == []
    assert result['steps'][1]['query']['parameters'] == ['泵P-17']
    hint = g.calls[1][1]['steps'][0]['query']['empty_result_inspection']
    assert hint['action_budget_charged'] == 1
    assert hint['text_filter_feedback']['filters'][0]['candidates'][0]['value'] == '泵P-17'
    assert g.calls[-1][0] == 'verify' and g.calls[-1][1]['user_operation_calculation_ids'] == ['K1']
    assert result['verification']['tool_actions'] == 4
    assert all('E1' not in c['evidence_ids'] for c in result['claims'])


def test_actual_absence_claim_gets_feedback_and_safe_narrow_failure(tariff):
    bad={'status':'insufficient_evidence','reason':'missing_evidence','claims':[
        {'text':'energy_tariff.csv中无对象为P-17的记录。','evidence_ids':['E1']}]}
    safe={'status':'insufficient_evidence','reason':'missing_evidence','claims':[
        {'text':'本次查询未命中，请核对筛选条件。','evidence_ids':['E1']}]}
    g=Gateway([action(),bad,safe,{'negative_supported':True,'conflict':False,'claims':[{'id':'C1','supported':True}]}])
    status,result=cross.answer_cross_source([tariff],SAVED['question'],[],g)
    assert status=='insufficient_evidence' and '中无对象' not in result['text']
    assert any(r['kind']=='empty_result_scope' for r in result['diagnostics']['rejections'])
    assert sum(role=='verify' for role,_ in g.calls)==1


def test_budget_exhausted_retains_original_empty_query_and_does_not_sample(tariff, monkeypatch):
    real = cross.build_catalog; calls=[]
    def catalog(*a, **k):
        calls.append(k.get('table_names'))
        assert k.get('table_names') == []  # Initial overview only, never detailed empty-result sampling.
        return real(*a, **k)
    monkeypatch.setattr(cross,'build_catalog',catalog)
    g=Gateway([action(), action('S2','泵P-17')])
    status,result=cross.answer_cross_source([tariff],SAVED['question'],[],g,limits=ReasoningLimits(max_actions=1))
    assert status!='completed' and result['verification']['tool_actions']==1
    inspection=result['steps'][0]['query']['empty_result_inspection']
    assert inspection['status']=='not_run_budget' and inspection['action_budget_charged']==0
    assert len(g.calls)==2 and len(result['steps'])==1


def test_catalog_failure_is_explicit_and_cancel_during_inspection_propagates(tariff, monkeypatch):
    real=cross.build_catalog;cancel=[False]
    def fail(source,table_names=None,cancelled=None):
        if table_names==[]:return real(source,table_names,cancelled)
        raise SchemaCatalogError('timeout')
    monkeypatch.setattr(cross,'build_catalog',fail)
    g=Gateway([action(), action('S2','泵P-17')])
    status,result=cross.answer_cross_source([tariff],SAVED['question'],[],g,limits=ReasoningLimits(max_actions=2))
    assert status!='completed' and result['verification']['tool_actions']==2
    assert result['steps'][0]['query']['empty_result_inspection']['status']=='unavailable'
    def abort(source,table_names=None,cancelled=None):
        if table_names==[]:return real(source,table_names,cancelled)
        cancel[0]=True
        raise SchemaCatalogError('cancelled')
    monkeypatch.setattr(cross,'build_catalog',abort)
    g=Gateway([action()])
    with pytest.raises(InterruptedError):
        cross.answer_cross_source([tariff],SAVED['question'],[],g,cancelled=lambda:cancel[0])
    assert len(g.calls)==1


def test_unknown_post_after_empty_hint_propagates_without_retry(tariff):
    class Unknown:
        def __init__(self): self.calls=0
        def __call__(self,messages,role):
            self.calls+=1
            if self.calls==1: return action()
            raise ModelError('unknown POST outcome', 'read_timeout')
    g=Unknown()
    with pytest.raises(ModelError,match='unknown POST'):
        cross.answer_cross_source([tariff],SAVED['question'],[],g)
    assert g.calls==2


def test_feedback_is_not_numeric_or_calculation_support(tariff):
    record=copy.deepcopy(SAVED['evidence'][0])
    record['empty_result_inspection']={'text_filter_feedback':{
        'filters':[{'column':'对象','candidates':[{'value':'泵999'}]}]}}
    assert '999' not in {str(n) for n in cross._evidence_numbers(record)}
    with pytest.raises(FormulaError):
        calculate_formula('q*p', {
            'q':{'evidence_id':'E1','row':0,'column':'用电量','unit':'kWh','unit_column':'计量单位'},
            'p':{'evidence_id':'E1','row':0,'column':'电价','unit':'元/kWh','unit_column':'价格单位'}},
            {'E1':record}, '元', user_formula=SAVED['question'],
            operation={'kind':'quantity_times_unit_price','pairs':[{'quantity':'q','unit_price':'p'}]})


def test_hint_character_budget_omits_hints_not_actual_empty_query(tariff,monkeypatch):
    g=Gateway([action(),action('S2','泵P-17')])
    _,first=cross.answer_cross_source([tariff],SAVED['question'],[],g,limits=ReasoningLimits(max_actions=2))
    original=copy.deepcopy(first['evidence'][0]);original.pop('empty_result_inspection',None)
    monkeypatch.setattr(cross,'MAX_EVIDENCE_CHARACTERS',cross.evidence_cost(original)+1)
    g=Gateway([action(),action('S2','泵P-17')])
    status,result=cross.answer_cross_source([tariff],SAVED['question'],[],g,limits=ReasoningLimits(max_actions=2))
    assert status!='completed' and len(result['evidence'])==1
    assert result['steps'][0]['query']['sql']==SQL
    assert result['steps'][0]['query']['parameters']==['P-17']
    assert result['steps'][0]['query']['empty_result_inspection']['status']=='omitted_context_budget'
    assert 'empty_result_inspection' not in result['evidence'][0]
    assert result['verification']['tool_actions']==2


def test_multiple_text_candidates_go_to_explicit_clarification_not_automatic_selection(tariff):
    with sqlite3.connect(tariff['data_path']) as c:
        c.execute('INSERT INTO "数据表" VALUES (?,?,?,?,?,?,?)',
                  ['2025-07-01','备用泵P-17','200','0.65','kWh','元/kWh','不含税'])
    tariff['tables'],tariff['warnings']=sqlite_tables(Path(tariff['data_path']))
    clarify={'status':'needs_clarification','clarification':{
        'question':'请选择泵P-17还是备用泵P-17？','field':'entity','options':['泵P-17','备用泵P-17']}}
    g=Gateway([action(),clarify,{'clarification_supported':True,'question_answerable':True,
        'requested_information_explicit':True,'options_supported':True}])
    status,result=cross.answer_cross_source([tariff],'计算P-17电费。',[],g)
    assert status=='needs_clarification' and result['claims']==[] and result['calculations']==[]
    hint=g.calls[1][1]['steps'][0]['query']['empty_result_inspection']['text_filter_feedback']
    assert len(hint['filters'][0]['candidates'])==2
    assert hint['filters'][0]['multiple_sample_candidates']
    assert result['steps'][0]['query']['parameters']==['P-17']
