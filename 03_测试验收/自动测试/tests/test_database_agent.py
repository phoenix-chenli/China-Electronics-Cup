"""Unknown-schema NL-query protocol integration; fixture replies do not prove model quality."""
import json
import sqlite3
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from tests import test_agent as fixture
from backend import agent, connections, store as s


def schema_action():
    return {'action': 'tool', 'tool': 'database.schema', 'arguments': {}}


def sql_action(sql, params=None):
    return {'action': 'tool', 'tool': 'database.query', 'arguments': {
        'sql': sql, 'params': params or [], 'explanation': '沿已声明的项目与结算关系汇总实际结算金额。'}}


class DatabaseAgentTests(unittest.TestCase):
    setUpClass = classmethod(fixture.AgentTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.AgentTests.tearDownClass.__func__)
    setUp = fixture.AgentTests.setUp
    tearDown = fixture.AgentTests.tearDown
    configure = fixture.AgentTests.configure
    ready = fixture.AgentTests.ready
    query = fixture.AgentTests.query
    script = fixture.AgentTests.script
    assert_trace = fixture.AgentTests.assert_trace
    wait_task = fixture.AgentTests.wait_task

    def database(self):
        self.ready()
        path = s.DATA / ('unseen-projects-' + uuid.uuid4().hex + '.sqlite')
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
              CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
              CREATE TABLE projects (id INTEGER PRIMARY KEY, department_id INTEGER NOT NULL REFERENCES departments(id), title TEXT);
              CREATE TABLE settlements (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), amount REAL);
              CREATE TABLE activities (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), label TEXT);
              INSERT INTO departments VALUES (1,'研发部'),(2,'交付部');
              INSERT INTO projects VALUES (10,1,'云平台'),(20,2,'网络升级');
              INSERT INTO settlements VALUES (101,10,12.375),(102,10,7.125),(103,20,6.25);
              INSERT INTO activities VALUES (201,10,'评审'),(202,10,'验收'),(203,20,'交付');
            ''')
            db.commit()
        response = self.client.post('/api/connections', json={'name': '新项目库', 'kind': 'sqlite', 'path': str(path)})
        self.assertEqual(response.status_code, 200, response.text)
        cid = response.json()['id']
        tested = self.client.post('/api/connections/' + cid + '/test').json()
        self.assertTrue(tested['last_test']['ok'])
        self.client.patch('/api/connections/' + cid + '/enabled', json={'enabled': True})
        model = agent.saved_config()['connection_id']
        response = self.client.request('PUT', '/api/agent/config', json={
            'mode': 'model', 'connection_id': model, 'query_scope': 'database', 'database_connection_id': cid})
        self.assertEqual(response.status_code, 200, response.text)
        return cid, path

    def run_sql(self, sql, params=None, question='每个部门的项目结算金额是多少？'):
        self.script(schema_action(), sql_action(sql, params), fixture.select_fact)
        response = self.query(question)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_unknown_schema_three_table_join_and_original_values(self):
        cid, path = self.database()
        before = s.digest(path)
        result = self.run_sql('SELECT d.name AS department, SUM(s.amount) AS amount FROM departments d '
                              'JOIN projects p ON p.department_id=d.id JOIN settlements s ON s.project_id=p.id '
                              'GROUP BY d.name ORDER BY amount DESC')
        self.assertEqual(result['rows'], [{'department': '研发部', 'amount': 19.5}, {'department': '交付部', 'amount': 6.25}])
        self.assertEqual(result['metrics'], [])
        self.assertEqual(result['conditions'], [])
        self.assertEqual(result['query_spec']['database_connection_id'], cid)
        self.assertEqual(len(result['database_query']['joins']), 2)
        self.assertFalse(result['database_query']['truncated'])
        self.assertEqual(result['database_query']['attempts'], 1)
        self.assertNotIn('year', result['query_spec'])
        self.assertEqual(s.digest(path), before)
        self.assert_trace(result)
        payload = json.dumps(fixture.ModelFixture.calls, ensure_ascii=False)
        self.assertNotIn(str(path), payload)
        self.assertNotIn(str(s.DATA), payload)
        self.assertIn('primary_key', payload)
        self.assertTrue(result['evidence'][0]['locator']['result_sha256'])

    def test_cte_preaggregation_avoids_fanout(self):
        self.database()
        result = self.run_sql('WITH totals AS (SELECT project_id, SUM(amount) AS total FROM settlements GROUP BY project_id), '
                              'logs AS (SELECT project_id, COUNT(*) AS count FROM activities GROUP BY project_id) '
                              'SELECT p.title, t.total, l.count FROM projects p JOIN totals t ON p.id=t.project_id '
                              'JOIN logs l ON p.id=l.project_id ORDER BY p.id')
        self.assertEqual(result['rows'], [{'title': '云平台', 'total': 19.5, 'count': 2}, {'title': '网络升级', 'total': 6.25, 'count': 1}])

    def test_nested_aggregate_and_parameter_binding(self):
        self.database()
        result = self.run_sql('SELECT title FROM projects WHERE id IN (SELECT project_id FROM settlements '
                              'GROUP BY project_id HAVING SUM(amount)>?) ORDER BY id', [10])
        self.assertEqual(result['rows'], [{'title': '云平台'}])
        self.assertEqual(result['database_query']['params'], [10])
        self.assertIn('SUBQUERY_KEY', json.dumps(result['database_query']['joins']))

    def test_invalid_field_correction_retains_failed_call_and_duration(self):
        self.database()
        self.script(schema_action(), sql_action('SELECT missing_field FROM projects'),
                    sql_action('SELECT title FROM projects ORDER BY id'), fixture.select_fact)
        response = self.query('列出项目名称')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        calls = [e for e in result['events'] if e['tool'] == 'database.query']
        self.assertEqual([e['status'] for e in calls], ['failed', 'completed'])
        self.assertEqual(result['database_query']['attempts'], 2)
        self.assertTrue(all(e['duration_ms'] >= 0 for e in calls))
        self.assertIn('error', fixture.ModelFixture.calls[2]['messages'][-1]['content'])

    def test_three_attempts_stop_writes_without_fourth_model_repair(self):
        self.database()
        source = Path(connections.get(agent.saved_config()['database_connection_id'])['path'])
        before = s.digest(source)
        self.script(schema_action(), *[sql_action('DELETE FROM projects') for _ in range(3)])
        response = self.query('删除全部项目')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(fixture.ModelFixture.calls), 4)
        self.assertEqual(len([e for e in response.json()['events'] if e['tool'] == 'database.query']), 3)
        self.assertEqual(s.digest(source), before)
        self.assertEqual(s.get_run(response.json()['run_id'])['claims'], [])

    def test_unrelated_join_is_rejected_and_ambiguity_is_clarified(self):
        self.database()
        self.script(schema_action(), sql_action('SELECT p.title,d.name FROM projects p JOIN departments d ON p.id=d.id'),
                    {'action': 'clarify', 'question': '请确认按项目所属部门汇总，还是按负责人员所属部门汇总？'})
        response = self.query('按部门汇总项目')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['clarification']['fields'], [])
        self.assertTrue(any(e['status'] == 'failed' for e in result['events']))

    def test_database_requires_model_and_cannot_fall_back_to_sales_tool(self):
        response = self.client.request('PUT', '/api/agent/config', json={'mode': 'local', 'query_scope': 'database'})
        self.assertEqual(response.status_code, 400)
        self.database()
        self.script(fixture.query_action(), {'action': 'answer', 'claims': []})
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['rows'], [])
        self.assertEqual(response.json()['metrics'], [])
        self.assertIn('不可切换', fixture.ModelFixture.calls[-1]['messages'][-1]['content'])

    def test_empty_result_is_real_fact_without_invented_statistic(self):
        self.database()
        result = self.run_sql('SELECT title FROM projects WHERE id=?', [999])
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['claims'][0]['kind'], 'database_empty')
        self.assertIn('0行', result['answer'])

    def test_answer_restores_the_query_snapshot_it_actually_cites(self):
        self.database()
        def select_first(payload):
            result = fixture.tool_results(payload)[1]['result']
            return {'action': 'answer', 'claims': [{'fact_id': result['facts'][0]['id']}]}
        self.script(schema_action(), sql_action('SELECT title FROM projects WHERE id=10'),
                    sql_action('SELECT title FROM projects WHERE id=20'), select_first)
        response = self.query('查询云平台项目')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['rows'], [{'title': '云平台'}])
        self.assertEqual(result['database_query']['query_id'], 'database-1')
        self.assertIn('10', result['sql'])
        self.assertEqual(len([e for e in result['events'] if e['tool'] == 'database.query']), 2)

    def test_mixed_query_claims_rejected(self):
        self.database()
        def mixed(payload):
            results = fixture.tool_results(payload)
            return {'action': 'answer', 'claims': [{'fact_id': results[i]['result']['facts'][0]['id']} for i in (1, 2)]}
        self.script(schema_action(), sql_action('SELECT title FROM projects WHERE id=10'),
                    sql_action('SELECT title FROM projects WHERE id=20'), mixed, mixed)
        response = self.query('查询项目')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('不同查询范围', response.json()['detail'])

    def test_followup_keeps_original_source_after_configuration_switch(self):
        cid, _ = self.database()
        first = self.run_sql('SELECT title FROM projects WHERE id=10')
        config = agent.saved_config()
        config['database_connection_id'] = None
        self.client.request('PUT', '/api/agent/config', json=config)
        self.script(schema_action(), sql_action('SELECT title FROM projects WHERE id=20'), fixture.select_fact)
        response = self.query('另一个项目呢？', previous_context=first['query_spec'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['query_spec']['database_connection_id'], cid)
        self.assertEqual(response.json()['rows'], [{'title': '网络升级'}])

    def test_persisted_followup_uses_server_owned_database_source(self):
        cid, _ = self.database()
        sid = self.client.post('/api/projects/default/sessions').json()['id']
        self.script(schema_action(), sql_action('SELECT title FROM projects WHERE id=10'), fixture.select_fact)
        first = self.client.post('/api/tasks', json={'session_id': sid, 'question': '查询云平台项目', 'request_id': str(uuid.uuid4())})
        first = self.wait_task(first.json()['id'])
        self.assertEqual(first['status'], 'completed', first)
        session = self.client.get('/api/sessions/' + sid).json()
        parent = next(m['id'] for m in session['messages'] if m.get('result'))
        config = agent.saved_config()
        config['database_connection_id'] = None
        self.client.request('PUT', '/api/agent/config', json=config)
        self.script(schema_action(), sql_action('SELECT title FROM projects WHERE id=20'), fixture.select_fact)
        task = self.client.post('/api/tasks', json={'session_id': sid, 'question': '另一个项目呢？',
            'parent_message_id': parent, 'previous_context': {'metric': 'database', 'database_connection_id': 'forged'},
            'inherit_context': True, 'request_id': str(uuid.uuid4())})
        task = self.wait_task(task.json()['id'])
        self.assertEqual(task['status'], 'completed', task)
        self.assertEqual(task['result']['query_spec']['database_connection_id'], cid)
        self.assertEqual(task['result']['rows'], [{'title': '网络升级'}])

    def test_followup_refuses_a_reconfigured_connection_id(self):
        cid, _ = self.database()
        first = self.run_sql('SELECT title FROM projects WHERE id=10')
        changed_path = s.DATA / ('changed-source-' + uuid.uuid4().hex + '.sqlite')
        with closing(sqlite3.connect(changed_path)) as db:
            db.execute('CREATE TABLE other_source(id INTEGER PRIMARY KEY)')
            db.commit()
        current = connections.get(cid)
        changed = self.client.request('PUT', '/api/connections/' + cid, json={
            'name': current['name'], 'kind':'sqlite', 'path':str(changed_path), 'expected_revision':current['revision']})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.client.post('/api/connections/' + cid + '/test')
        self.client.patch('/api/connections/' + cid + '/enabled', json={'enabled':True})
        self.script()
        before_calls = len(fixture.ModelFixture.calls)
        response = self.query('另一个项目呢？', previous_context=first['query_spec'])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('重新确认数据源', response.json()['detail'])
        self.assertEqual(len(fixture.ModelFixture.calls), before_calls)

    def test_queued_query_source_is_frozen_before_configuration_changes(self):
        cid, _ = self.database()
        manager = fixture.conv.TaskManager(workers=1)
        gate = threading.Event()
        try:
            manager.pool.submit(lambda: gate.wait(5))
            self.script(schema_action(), sql_action('SELECT title FROM projects WHERE id=10'), fixture.select_fact)
            task = manager.submit({'question': '查询云平台项目', 'workspace_id': 'sales-demo'})
            config = agent.saved_config()
            config['database_connection_id'] = None
            self.client.request('PUT', '/api/agent/config', json=config)
            gate.set()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                task = manager.get(task['id'])
                if task['status'] not in fixture.conv.ACTIVE:
                    break
                time.sleep(.01)
            self.assertEqual(task['status'], 'completed', task)
            self.assertEqual(task['result']['query_spec']['database_connection_id'], cid)
            self.assertEqual(task['result']['rows'], [{'title': '云平台'}])
        finally:
            gate.set()
            manager.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
