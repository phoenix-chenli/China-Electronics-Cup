"""Synthetic source witnesses and fake reviewers; no key or model/network use."""
import copy
import hashlib
import json
import sqlite3

import pytest

from backend.model_gateway import ModelError
from backend.parsers import sqlite_tables
from backend.query_planner import answer_database
from backend.schema_catalog import build_catalog
from backend.sql_engine import execute_sql
from backend.sql_review_evidence import build_review_evidence, review_challenges, MAX_CHARS
from test_sql_answer_contract_repair import reviewed


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'runtime-witness.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript("""CREATE TABLE metrics(region TEXT,period TEXT,amount_minor INTEGER);
        INSERT INTO metrics VALUES('甲','2026-12',8),('甲','2027-01',10),
          ('甲','2027-02',NULL),('甲','2027-03',-2),('乙','2027-01',0);
        CREATE TABLE complete_dates(day TEXT); INSERT INTO complete_dates VALUES('2027-01-01');
        """)
    tables, warnings = sqlite_tables(path)
    return dict(id='fixture', name=path.name, data_path=str(path), tables=tables, warnings=warnings,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(), parser_version='test')


def plan(sql):
    return dict(status='ready', normalized_question='列出实际记录', intent={}, sql=sql, parameters=[])


class Fake:
    def __init__(self, *items): self.items = list(items); self.calls = []
    def __call__(self, messages, role):
        payload = json.loads(messages[-1]['content']); self.calls.append((role, payload))
        assert self.items, 'Unexpected extra request'
        value = self.items.pop(0)
        if isinstance(value, Exception): raise value
        return value(payload) if callable(value) else copy.deepcopy(value)


SQL = "SELECT period,amount_minor FROM metrics WHERE period LIKE '2027-%' ORDER BY period,region"


def evidence(source, sql=SQL, question='列出金额，单位分。', execute=execute_sql, cancelled=lambda: False):
    return build_review_evidence(source, question, [], sql, [], build_catalog(source), execute, cancelled)


def wrong_runtime(payload):
    fact = next(f for f in payload['review_evidence']['facts'] if f.get('source_value') == '2026-12')
    return reviewed(supported=False, reason='该条件仍会保留早一年记录', issues=[
        {'id': 'I1', 'dimension': 'output_contract', 'kind': 'runtime_fact',
         'fact_id': fact['id'], 'asserted_value': True}])


def decision(review=None, **patch):
    return {'decision': 'retract', 'resolved_issue_ids': ['I1'],
            'review': reviewed() if review is None else review, **patch}


def test_runtime_probes_show_like_exclusion_and_invalid_month_dates_without_guessing_business(source):
    before = open(source['data_path'], 'rb').read()
    like = evidence(source)
    witness = next(f for f in like['facts'] if f.get('source_value') == '2026-12')
    assert witness['value'] is False and like['probes'][0]['sample_only']
    date = evidence(source, "SELECT period FROM metrics WHERE strftime('%Y',period)='2027'")
    assert date['probes'] and all(row[1] is None for row in date['probes'][0]['rows'])
    full = evidence(source, "SELECT day FROM complete_dates WHERE strftime('%Y',day)='2027'")
    assert full['probes'][0]['rows'] == [['2027-01-01', 1]]
    assert open(source['data_path'], 'rb').read() == before


def test_runtime_null_rules_do_not_invent_missing_value_policy(source):
    facts = {f['id']: f for f in evidence(source)['facts']}
    assert facts['SQL_SUM_MIXED_NULL']['value'] == 2
    assert facts['SQL_SUM_ALL_NULL']['value'] is None
    assert facts['SQL_HAS_COALESCE']['value'] is False
    result = evidence(source, 'SELECT COALESCE(amount_minor,0) FROM metrics')
    assert next(f for f in result['facts'] if f['id'] == 'SQL_HAS_COALESCE')['value'] is True
    assert result['user_contract']['interpretation'] == 'not_inferred'


def test_happy_path_still_has_one_independent_review_and_no_adjudication(source):
    gateway = Fake(plan(SQL), reviewed())
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'completed' and len(gateway.calls) == 2
    assert 'review_adjudication' not in result['query']
    assert 'review_evidence' in gateway.calls[1][1]


def test_contradiction_is_new_independent_review_with_initial_false_preserved(source):
    events = []; gateway = Fake(plan(SQL), wrong_runtime, decision())
    status, result = answer_database(source, '列出本年金额', [], gateway, audit=events.append)
    assert status == 'completed' and [r for r, _ in gateway.calls] == ['table', 'verify', 'verify']
    query = result['query']; initial = query['review_response']
    assert initial['supported'] is False
    assert query['effective_review_source'] == 'independent_adjudication'
    assert query['review_adjudication']['response']['review']['supported'] is True
    assert query['semantic_reviews'][0]['review_response'] == initial
    assert events[0]['semantic_reviews'][0]['review_response'] == initial
    assert events[0]['semantic_reviews'][0]['review_adjudication']['status'] == 'pending'
    assert result['rows'] == [['2027-01', 0], ['2027-01', 10], ['2027-02', None], ['2027-03', -2]]
    assert query['review_adjudication']['passed_remaining_gates'] is True


@pytest.mark.parametrize('response', [
    decision(resolved_issue_ids=[]), decision(resolved_issue_ids=['I2']),
    decision(review={'supported': True}), decision(decision='uphold'),
    decision(review=reviewed(supported=False)),
])
def test_unresolved_or_unreviewed_adjudication_cannot_publish(source, response):
    gateway = Fake(plan(SQL), wrong_runtime, response)
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'failed' and not result['claims'] and not result.get('rows')
    assert len(gateway.calls) == 3 and result['query']['review_response']['supported'] is False


def test_all_scope_gates_are_reapplied_to_adjudication(source):
    scope = reviewed()['scope_review']; scope['population_grain'] = False
    gateway = Fake(plan(SQL), wrong_runtime, decision(review=reviewed(scope_review=scope)))
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'failed' and not result['claims']
    assert result['query']['scope_review']['population_grain'] is False
    assert result['query']['review_adjudication']['passed_remaining_gates'] is False


def test_table_relation_gate_is_reapplied_when_required(source, monkeypatch):
    monkeypatch.setattr('backend.sql_engine.table_review_requirements', lambda _: (['C1'], []))
    gateway = Fake(plan(SQL), wrong_runtime, decision())
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'failed' and not result['claims'] and '原格关系' in result['text']


def test_mechanical_rejection_cannot_be_overridden_by_technical_challenge(source):
    gateway = Fake(plan(SQL), wrong_runtime, {'status': 'failed'})
    status, result = answer_database(source, '只输出金额。', [], gateway)
    assert status == 'failed' and not result['claims']
    assert [r for r, _ in gateway.calls] == ['table', 'verify', 'table']
    assert 'review_adjudication' not in result['query']


def test_unknown_adjudication_post_preserves_initial_audit_without_retry(source):
    events = []; unknown = ModelError('Unknown post result', 'timeout')
    gateway = Fake(plan(SQL), wrong_runtime, unknown)
    with pytest.raises(ModelError) as caught:
        answer_database(source, '列出本年金额', [], gateway, audit=events.append)
    assert caught.value is unknown and len(gateway.calls) == 3 and len(events) == 1
    saved = events[0]['semantic_reviews'][0]
    assert saved['review_response']['supported'] is False and saved['execution_preview']['rows']
    assert saved['review_adjudication']['status'] == 'pending'


def test_cancel_after_received_false_does_not_send_adjudication(source):
    events = []; stopped = [False]
    def persist(event): events.append(event); stopped[0] = True
    gateway = Fake(plan(SQL), wrong_runtime)
    with pytest.raises(InterruptedError):
        answer_database(source, '列出本年金额', [], gateway, audit=persist, cancelled=lambda: stopped[0])
    assert len(gateway.calls) == 2 and events[0]['semantic_reviews'][0]['review_response']['supported'] is False


def test_received_adjudication_survives_concurrent_cancel_without_publication(source):
    stopped = [False]; events = []
    def late(_): stopped[0] = True; return decision()
    gateway = Fake(plan(SQL), wrong_runtime, late)
    with pytest.raises(InterruptedError):
        answer_database(source, '列出本年金额', [], gateway, audit=events.append, cancelled=lambda: stopped[0])
    assert len(gateway.calls) == 3 and len(events) == 2
    record = events[-1]['semantic_reviews'][0]
    assert record['review_response']['supported'] is False
    assert record['review_adjudication']['response']['review']['supported'] is True
    assert record['review_adjudication']['passed_remaining_gates'] is False
    assert record['status'] == 'review_pending'


def test_repair_after_adjudication_uses_received_effective_scope_failure(source):
    scope = reviewed()['scope_review']; scope['population_grain'] = False
    alternative = reviewed(scope_review=scope, failure_kind='invalid_plan', reason='重新核对发现真实粒度问题')
    gateway = Fake(plan(SQL), wrong_runtime, decision(review=alternative), {'status': 'failed'})
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'failed' and gateway.calls[-1][0] == 'table'
    assert gateway.calls[-1][1]['received_review'] == alternative
    assert result['query']['semantic_reviews'][0]['review_response']['supported'] is False


def test_only_one_technical_adjudication_per_answer(source):
    def refused(payload):
        value = wrong_runtime(payload); value['failure_kind'] = 'invalid_plan'; return value
    gateway = Fake(plan(SQL), refused, decision(decision='uphold'),
                   plan(SQL.replace('ORDER BY period,region', 'ORDER BY period DESC,region')), refused)
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'failed' and len(gateway.calls) == 5
    assert sum('initial_review' in p for _, p in gateway.calls) == 1
    assert len(result['query']['semantic_reviews']) == 2


def test_literal_basis_challenges_do_not_choose_currency_or_null_policy(source):
    ev = evidence(source, question='列金额（均分）')
    issue = {'id': 'I1', 'dimension': 'output_contract', 'kind': 'user_requirement',
             'requirement': 'currency_conversion', 'user_quote': '（均分）', 'target_unit': '元'}
    check = reviewed(supported=False, issues=[issue])
    assert review_challenges(check, ev, '列金额（均分）', [])[0]['kind'] == 'conversion_not_literal_in_cited_quote'
    explicit = {**issue, 'user_quote': '换算为元'}
    assert review_challenges(reviewed(supported=False, issues=[explicit]), ev, '金额换算为元', []) == []
    assert ev['user_contract']['interpretation'] == 'not_inferred'
    propagation = {**issue, 'requirement': 'null_propagation', 'user_quote': '累计金额'}
    assert review_challenges(reviewed(supported=False, issues=[propagation]), ev, '列累计金额', [])
    propagation['user_quote'] = '遇到未知则累计金额为未知'
    assert review_challenges(reviewed(supported=False, issues=[propagation]), ev, propagation['user_quote'], []) == []


@pytest.mark.parametrize('issues', [
    [{'id': 'I1', 'dimension': 'output_contract', 'kind': 'runtime_fact', 'fact_id': 'SQL_SUM_MIXED_NULL', 'asserted_value': 2}],
    [{'id': 'I1', 'dimension': 'output_contract', 'kind': 'runtime_fact', 'fact_id': 'missing', 'asserted_value': False, 'probe_sql': 'DELETE FROM metrics'}],
    [{'id': 'I1', 'dimension': 'output_contract', 'kind': 'runtime_fact', 'fact_id': ['bad'], 'asserted_value': {'nested': 'huge'}}],
    [{'id': 'I1', 'dimension': 'output_contract', 'kind': 'runtime_fact', 'fact_id': 'SQL_SUM_MIXED_NULL', 'asserted_value': 10**400}],
    [{'id': 'I1', 'dimension': 'output_contract', 'kind': 'runtime_fact', 'fact_id': 'SQL_SUM_MIXED_NULL', 'asserted_value': None}] * 13,
])
def test_matching_invalid_oversized_or_malicious_issues_do_not_trigger_requests(source, issues):
    gateway = Fake(plan(SQL), reviewed(supported=False, issues=issues))
    status, result = answer_database(source, '列出本年金额', [], gateway)
    assert status == 'failed' and len(gateway.calls) == 2
    assert execute_sql(source, 'SELECT COUNT(*) FROM metrics', [])['rows'] == [[5]]


def test_probe_budget_parameter_boundary_and_cancellation(source):
    many = 'SELECT period FROM metrics WHERE ' + ' AND '.join(f"period != '2000-{i:02d}'" for i in range(1, 9))
    calls = []
    def execute(*args): calls.append(args[1]); return execute_sql(*args)
    result = evidence(source, many, execute=execute)
    assert len(calls) == len(result['probes']) == 3 and result['truncated']
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_CHARS
    assert all(len(p['rows']) <= 12 and p['sample_only'] for p in result['probes'])
    parameterized = build_review_evidence(source, '列指定年', [], 'SELECT period FROM metrics WHERE period LIKE ?', ['2027-%'], build_catalog(source), execute)
    assert not parameterized['probes'] and parameterized['unresolved']
    with pytest.raises(InterruptedError): evidence(source, cancelled=lambda: True)


def test_same_named_cte_does_not_probe_an_unrelated_physical_expression(source):
    sql = "WITH metrics AS (SELECT '2099-01' AS period) SELECT period FROM metrics WHERE period LIKE '2099-%'"
    result = evidence(source, sql)
    assert result['probes'] == []


def test_rejected_probe_attempts_still_consume_local_budget(source):
    many = 'SELECT period FROM metrics WHERE ' + ' AND '.join(f"period != '2000-{i:02d}'" for i in range(1, 9))
    calls = []
    def rejected(*args):
        calls.append(args[1])
        raise ValueError('Local readonly execution refused')
    result = evidence(source, many, execute=rejected)
    assert len(calls) == result['probe_attempts'] == 3
    assert result['probes'] == [] and result['truncated']
    assert len(result['unresolved']) == 3


def test_legitimate_empty_result_not_rejected_by_nonmatching_probe_values(source):
    gateway = Fake(plan(SQL.replace('2027-%', '2099-%')), reviewed())
    status, result = answer_database(source, '列出2099年记录', [], gateway)
    assert status == 'completed' and result['rows'] == [] and len(gateway.calls) == 2


def test_current_literal_fragment_is_retained_in_long_history(source):
    history = [{'original_question': f'第{i}轮普通问题'} for i in range(30)]
    result = build_review_evidence(source, '金额单位分', history, SQL, [], build_catalog(source), execute_sql)
    assert result['user_contract']['literal_fragments'][0]['quote'] == '单位分'
    assert result['user_contract']['literal_fragments'][0]['original_question_index'] == 0


def test_oversized_source_values_are_omitted_instead_of_truncated_into_false_witnesses(source):
    with sqlite3.connect(source['data_path']) as c:
        c.execute('INSERT INTO metrics VALUES(?,?,?)', ['超长', 'x' * 5000, 1])
    result = evidence(source, "SELECT period FROM metrics WHERE period != 'none'")
    assert result['probes'][0]['truncated']
    assert all(len(str(f.get('source_value', ''))) <= 256 for f in result['facts'])
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_CHARS
