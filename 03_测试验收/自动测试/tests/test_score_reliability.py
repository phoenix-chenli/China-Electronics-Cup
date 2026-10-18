"""Regression contracts on disposable data; scripted models are not quality scores."""
import json
import sqlite3
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from tests import test_agent as fixture
from backend import agent, database_query as query, rag_grounding, rag_retrieval


def schema():
    return {'action': 'tool', 'tool': 'database.schema', 'arguments': {}}


def sql(text, params=None):
    return {'action': 'tool', 'tool': 'database.query',
            'arguments': {'sql': text, 'params': params or [], 'explanation': '按问题指定范围只读查询'}}


class ScoreReliabilityTests(unittest.TestCase):
    def test_hybrid_calculation_guard_preserves_definition_and_inference_questions(self):
        for question in ['从低到高排列浓度，并算最高与最低的差值','请算同比增幅，给出计算结果','算出两地人口合计']:
            self.assertTrue(agent.requests_sql_calculation(question),question)
        for question in ['只解释比例公式的计算方法','这份记录能否计算全年实际量？','仅查文档的日规模能否确定全年总量？']:
            self.assertFalse(agent.requests_sql_calculation(question),question)
        run=agent.Run.__new__(agent.Run)
        run.database_context={'query_scope':'hybrid'};run.query_snapshots={}
        run.result={'question':'从低到高排列浓度，并算最高与最低的差值'}
        with patch.object(run,'retrieve_documents') as search:
            with self.assertRaisesRegex(ValueError,'尚无实际数据库查询'):
                run.dispatch('document.search',{'query':'相关原文'})
            search.assert_not_called()

    setUpClass = classmethod(fixture.AgentTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.AgentTests.tearDownClass.__func__)
    setUp = fixture.AgentTests.setUp
    tearDown = fixture.AgentTests.tearDown
    configure = fixture.AgentTests.configure
    ready = fixture.AgentTests.ready
    script = fixture.AgentTests.script

    def test_sales_result_keeps_frozen_route_for_short_followup(self):
        from backend import query_routing
        self.ready()
        profile = query_routing.profile
        def auto_profile(project_id=None):
            return {**profile(project_id), 'default_scope': 'auto'}
        with patch.object(query_routing, 'profile', side_effect=auto_profile):
            self.script(fixture.query_action(), fixture.select_fact)
            first = self.ask('2024年华东净销售额是多少？').json()
            self.assertEqual(first['query_spec']['query_scope'], 'sales')
            self.assertEqual(first['query_spec']['routing']['selected_scope'], 'sales')
            self.script(fixture.query_action('那华北呢？'), fixture.select_fact)
            second = self.ask('那华北呢？', previous_context=first['query_spec']).json()
            self.assertEqual(second['query_spec']['query_scope'], 'sales')
            self.assertEqual(second['query_spec']['year'], 2024)
            self.assertEqual(second['query_spec']['region'], '华北')
            self.assertTrue(second['query_spec']['routing']['inherited'])
            self.configure('local')
            local = self.ask('2024年华东净销售额是多少？').json()
            self.assertEqual(local['query_spec']['query_scope'], 'sales')
            self.assertEqual(local['query_spec']['routing']['selected_scope'], 'sales')

    def database(self, scope='database'):
        model = self.ready()
        path = fixture.s.DATA / ('reliability-' + uuid.uuid4().hex + '.sqlite')
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE sites(site_id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE devices(device_id INTEGER PRIMARY KEY, device_name TEXT,
                    site_id INTEGER REFERENCES sites(site_id));
                CREATE TABLE tickets(ticket_id INTEGER PRIMARY KEY, device_id INTEGER REFERENCES devices(device_id),
                    opened_at TEXT, amount REAL);
                INSERT INTO sites VALUES (1,'东站'),(2,'西站');
                INSERT INTO devices VALUES (1,'甲型设备',1),(2,'甲型设备',2),(3,'乙型设备',1);
                INSERT INTO tickets VALUES (1,1,'2025-03-01',12),(2,2,'2025-03-17',18),(3,3,'2025-04-01',5);
            ''')
            db.commit()
        response = self.client.post('/api/connections', json={'name': '隔离可靠性库', 'kind': 'sqlite', 'path': str(path)})
        cid = response.json()['id']
        tested = self.client.post('/api/connections/' + cid + '/test').json()
        self.assertTrue(tested['last_test']['ok'])
        self.client.patch('/api/connections/' + cid + '/enabled', json={'enabled': True})
        response = self.client.request('PUT', '/api/agent/config', json={'mode': 'model', 'connection_id': model['id'],
            'query_scope': scope, 'database_connection_id': cid})
        self.assertEqual(response.status_code, 200, response.text)
        self.database_path = path
        return cid

    def statistics_database(self):
        cid = self.database()
        with closing(sqlite3.connect(self.database_path)) as db:
            db.executescript('''
                CREATE TABLE observations(region_id TEXT,year INTEGER,metric_id TEXT,value REAL,population_type TEXT,
                    PRIMARY KEY(region_id,year,metric_id));
                INSERT INTO observations VALUES ('星城',2023,'gdp',8,NULL),('星城',2024,'gdp',9,NULL),
                    ('星城',2024,'resident',100,'常住人口'),('星城',2024,'registered',80,'户籍人口');
            ''')
            db.commit()
        return cid

    def ask(self, question, **extra):
        return self.client.post('/api/query', json={'question': question, **extra})

    def test_same_name_singular_question_clarifies_before_business_sql(self):
        self.database()
        self.script(schema())
        response = self.ask('甲型设备属于哪个站点？我只指其中一台，没有给设备编号。')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['answer_status'], 'needs_clarification')
        self.assertEqual(result['clarification_reason'], 'ambiguous_entity')
        self.assertIn('2 条同名记录', result['answer'])
        self.assertIn('东站', result['answer'])
        self.assertIn('西站', result['answer'])
        self.assertNotIn('device_id', result['answer'])
        self.assertFalse(any(e['tool'] == 'database.query' for e in result['events']))
        self.assertEqual(result['agent']['calls'], 1)

    def test_subjective_statistical_ranking_requires_a_criterion_before_business_sql(self):
        cid=self.statistics_database()
        self.script(schema())
        response=self.ask('星城与月城哪个发展得最好？给我唯一排名。')
        self.assertEqual(response.status_code,200,response.text)
        result=response.json();self.assertEqual(result['answer_status'],'needs_clarification')
        self.assertIn('用于排名的指标',result['answer'])
        self.assertFalse(any(e['tool']=='database.query' for e in result['events']))
        snapshot=query.inspect(cid)
        self.assertIsNone(query.dimension_clarification('按2024年GDP比较发展表现，给出排名',snapshot,cid))

    def test_definite_singular_reference_to_a_duplicated_name_clarifies(self):
        # "这台甲型设备" is as singular as "我只指其中一台"; both point at one row of
        # a name that has two. The same-name lookup must fire without the explicit
        # "我只指其中一" wording, so a definite singular is not answered as if the
        # name were unique.
        self.database()
        self.script(schema())
        response = self.ask('这台甲型设备在哪个站点？')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['answer_status'], 'needs_clarification')
        self.assertEqual(result['clarification_reason'], 'ambiguous_entity')
        self.assertIn('东站', result['answer'])
        self.assertIn('西站', result['answer'])
        self.assertFalse(any(e['tool'] == 'database.query' for e in result['events']))
        self.assertEqual(result['agent']['calls'], 1)

    def test_same_name_duplicates_group_case_insensitively_with_a_label(self):
        cid = self.database()
        with closing(sqlite3.connect(self.database_path)) as db:
            db.executescript('''
                CREATE TABLE songs(song_id INTEGER PRIMARY KEY, title TEXT, site_id INTEGER REFERENCES sites(site_id));
                INSERT INTO songs VALUES (1,'Dazed and Confused',1),(2,'Dazed And Confused',2),(3,'Other Song',1);
            ''')
            db.commit()
        snapshot = query.inspect(cid)
        findings = query.entity_ambiguities('Dazed And Confused 这首歌有多长？', snapshot, cid)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['name'].casefold(), 'dazed and confused')
        self.assertEqual({item['song_id'] for item in findings[0]['candidates']}, {1, 2})
        self.assertEqual(findings[0]['label_fields'], ['sites'])
        self.assertEqual({item['sites'] for item in findings[0]['candidates']}, {'东站', '西站'})

    def test_a_named_entity_with_no_distinguishing_field_still_reports_the_duplicate(self):
        # A question can single out one entity by naming it ("播放列表 Music") with
        # no determiner; the name carries the singularity. When the duplicates have
        # no distinguishing column the finding still surfaces, so the model cannot
        # silently sum two same-named records into one number.
        cid = self.database()
        with closing(sqlite3.connect(self.database_path)) as db:
            db.executescript('''
                CREATE TABLE playlists(playlist_id INTEGER PRIMARY KEY, name TEXT);
                INSERT INTO playlists VALUES (1,'Music'),(2,'Music'),(3,'Movies');
            ''')
            db.commit()
        snapshot = query.inspect(cid)
        findings = query.entity_ambiguities('播放列表 Music 里有多少首曲目？', snapshot, cid)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['name'], 'Music')
        self.assertEqual(findings[0]['label_fields'], [])
        self.assertEqual({item['playlist_id'] for item in findings[0]['candidates']}, {1, 2})
        # A unique name in the same question must not be reported.
        self.assertEqual(query.entity_ambiguities('播放列表 Movies 里有多少首曲目？', snapshot, cid), [])

    def test_explicit_key_and_all_entities_do_not_trigger_singular_guard(self):
        cid = self.database()
        snapshot = query.inspect(cid)
        self.assertEqual(query.entity_ambiguities('查询设备编号1的甲型设备属于哪个站点', snapshot, cid), [])
        self.assertEqual(query.entity_ambiguities('全部甲型设备分别属于哪个站点', snapshot, cid), [])
        self.script(schema(), sql('SELECT site_id FROM devices WHERE device_id=?', [1]), fixture.select_fact)
        response = self.ask('设备编号1的甲型设备属于哪个站点？')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['rows'], [{'site_id': 1}])

    def test_count_for_one_month_cannot_silently_split_into_days(self):
        self.database()
        self.script(schema(), sql('SELECT DATE(opened_at) AS day,COUNT(*) AS total FROM tickets '
            "WHERE opened_at>='2025-03-01' AND opened_at<'2025-04-01' GROUP BY DATE(opened_at)"),
            sql("SELECT COUNT(*) AS total FROM tickets WHERE opened_at>='2025-03-01' AND opened_at<'2025-04-01'"), fixture.select_fact)
        response = self.ask('2025年3月创建的工单有多少个？只返回计数。')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['rows'], [{'total': 2}])
        stages = [e for e in result['events'] if e['tool'] == 'database.query']
        self.assertEqual([e['status'] for e in stages], ['failed', 'completed'])
        self.assertIn('GROUP BY', stages[0]['summary'])

    def test_monthly_grain_guard_understands_expression_and_alias(self):
        for text in ['SELECT DATE(opened_at) AS d,COUNT(*) FROM tickets GROUP BY DATE(opened_at)',
                     'SELECT DATE(opened_at) AS d,COUNT(*) FROM tickets GROUP BY d']:
            with self.assertRaisesRegex(ValueError, '按月'):
                query.validate_question_grain(text, '按月统计工单总数')
        query.validate_question_grain("SELECT strftime('%Y-%m',opened_at) AS month,COUNT(*) FROM tickets GROUP BY month", '按月统计工单总数')
        query.validate_question_grain('SELECT DATE(opened_at),COUNT(*) FROM tickets GROUP BY DATE(opened_at)', '按日分别统计工单计数')
        query.validate_question_grain('WITH daily AS (SELECT opened_at,COUNT(*) AS n FROM tickets GROUP BY opened_at) SELECT SUM(n) FROM daily', '只返回总数')

    def test_cross_source_review_receives_filter_bound_to_aggregate_fact(self):
        self.database('hybrid')
        path = fixture.s.UPLOADS / ('scope-' + uuid.uuid4().hex + '.md')
        path.write_text('amount 的单位为人民币元。需要更换部件时，成本超过500元必须先经站点负责人批准。', encoding='utf-8')
        doc = fixture.s.index_document(path)
        def requirements(payload):
            return {'requirements': [{'text': '工单2的成本', 'independent': False},
                                     {'text': '审批规则及金额单位', 'independent': False}]}
        def assessment(payload):
            packet = json.loads(payload['messages'][-1]['content'])['evidence']
            fact = next(f for f in packet['facts'] if f.get('kind') == 'database_value')
            provenance = packet['queries'][fact['query_id']]
            self.assertEqual(provenance['params'], [2])
            self.assertIn('ticket_id', provenance['sql'])
            self.assertTrue(provenance['result_sha256'])
            self.assertNotIn('ticket_id', packet['queries'][fact['query_id']]['columns'])
            return {'coverage': [{'requirement_id': 'R1', 'status': 'supported', 'fact_ids': [fact['id']],
                                  'support_span_ids': [], 'reason': '已执行SQL WHERE及参数确认对象'},
                                 {'requirement_id': 'R2', 'status': 'supported', 'fact_ids': [],
                                  'support_span_ids': [packet['spans'][0]['id']], 'reason': '原文确认规则与单位'}]}
        def answer(payload):
            tools = fixture.tool_results(payload)
            fact = next(f for t in tools for f in t['result'].get('facts', []) if f.get('kind') == 'database_value')
            source = next(e for t in tools for e in t['result'].get('evidence', []) if e.get('type') == 'document')
            return {'action': 'answer', 'claims': [{'fact_id': fact['id']}, {'evidence_id': source['id'], 'quote': source['snippet']}]}
        def support(payload):
            body = json.loads(payload['messages'][-1]['content'])
            self.assertEqual(body['queries'][body['claims'][0]['fact']['query_id']]['params'], [2])
            return {'claims': [{'claim_id': c['id'], 'status': 'supported', 'detail': '已绑定服务端快照或原文'} for c in body['claims']],
                    'coverage': [{'requirement_id': 'R1', 'status': 'supported', 'claim_ids': [body['claims'][0]['id']], 'reason': '查询范围正确'},
                                 {'requirement_id': 'R2', 'status': 'supported', 'claim_ids': [body['claims'][1]['id']], 'reason': '有直接原文'}]}
        self.script(schema(), sql('SELECT SUM(amount) AS cost FROM tickets WHERE ticket_id=?', [2]),
            {'action': 'tool', 'tool': 'document.search', 'arguments': {'query': 'amount 审批规则'}},
            requirements, assessment, answer, support)
        response = self.ask('工单2的成本和需要审批的规则是什么？', rag_scope={'scope_type': 'document', 'document_ids': [doc['id']]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['answer_status'], 'supported')
        self.assertEqual(result['rows'], [{'cost': 18.0}])
        self.assertEqual(result['claims'][0]['value'], 18.0)

    def test_repeated_schema_reuses_this_run_but_sql_still_revalidates(self):
        self.database()
        self.script(schema(), schema(), sql('SELECT COUNT(*) AS n FROM tickets'), fixture.select_fact)
        original = query.inspect
        with patch.object(query, 'inspect', wraps=original) as inspect:
            response = self.ask('工单总数是多少？')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(response.json()['rows'], [{'n': 3}])

    def test_exact_repeat_search_reuses_retrieval_and_current_evidence_assessment(self):
        model = self.ready()
        self.client.request('PUT', '/api/agent/config', json={'mode': 'model', 'connection_id': model['id'], 'query_scope': 'knowledge'})
        path = fixture.s.UPLOADS / ('repeat-' + uuid.uuid4().hex + '.md')
        path.write_text('甲产品申请售后须提交发票。', encoding='utf-8')
        doc = fixture.s.index_document(path)
        search = {'action': 'tool', 'tool': 'document.search', 'arguments': {'query': '甲产品售后发票'}}
        def assess(payload):
            body = json.loads(payload['messages'][-1]['content'])
            return {'coverage': [{'requirement_id': 'R1', 'status': 'supported',
                'support_span_ids': [body['evidence']['spans'][0]['id']], 'fact_ids': [], 'reason': '原文提供必要资料'}]}
        def answer(payload):
            evidence = fixture.tool_results(payload)[0]['result']['evidence'][0]
            return {'action': 'answer', 'claims': [{'evidence_id': evidence['id'], 'quote': evidence['snippet']}]}
        def support(payload):
            body = json.loads(payload['messages'][-1]['content'])
            return {'claims': [{'claim_id': body['claims'][0]['id'], 'status': 'supported', 'detail': '原文一致'}],
                'coverage': [{'requirement_id': 'R1', 'status': 'supported', 'claim_ids': [body['claims'][0]['id']], 'reason': '回答完整'}]}
        self.script(search, {'requirements': [{'text': '售后提交资料', 'independent': False}]}, assess,
                    search, {'action': 'tool', 'tool': 'evidence.assess', 'arguments': {}}, answer, support)
        original = rag_retrieval.search
        with patch.object(rag_retrieval, 'search', wraps=original) as retrieval:
            response = self.ask('甲产品售后提交什么资料？', rag_scope={'scope_type': 'document', 'document_ids': [doc['id']]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['answer_status'], 'supported')
        self.assertEqual(retrieval.call_count, 1)
        stages = [e for e in result['events'] if e['tool'] == 'document.search']
        self.assertTrue(stages[1]['outputs']['cached'])
        self.assertEqual(len([e for e in result['events'] if e['tool'] == 'evidence.assess' and e.get('inputs', {}).get('call')]), 1)
        self.assertEqual(result['agent']['calls'], 7)
        self.assertEqual(result['agent']['evidence_assessment_cache_hits'],3)

    def test_provider_failure_is_classified_and_not_empty(self):
        self.ready()
        fixture.ModelFixture.mode = 'http_error'
        response = self.ask('2024年华东净销售额是多少？')
        self.assertEqual(response.status_code, 400, response.text)
        result = fixture.s.get_run(response.json()['run_id'])
        self.assertEqual(result['answer_status'], 'failed')
        self.assertIn('本轮未完成', result['answer'])
        self.assertEqual(result['error_detail']['code'], 'provider_unavailable')
        self.assertFalse(result['error_detail']['automatic_resubmission'])
        self.assertNotIn('private_provider_detail', json.dumps(result))

    def test_query_provenance_does_not_override_unsupported_review(self):
        facts = {'F1': {'id': 'F1', 'text': '合计18', 'query_id': 'q1', 'query_provenance':
            {'sql': 'SELECT SUM(amount) FROM tickets WHERE ticket_id=?', 'params': [2]}}}
        claims = [{'id': 'C1', 'text': '合计18', 'kind': 'database_value', 'fact_id': 'F1'}]
        def judge(*args):
            return {'claims': [{'claim_id': 'C1', 'status': 'unsupported', 'detail': '问题对象为工单1，实际查工单2'}],
                'coverage': [{'requirement_id': 'R1', 'status': 'missing', 'claim_ids': [], 'reason': '对象不匹配'}]}
        checked = rag_grounding.review('工单1成本', [{'id': 'R1', 'text': '工单1成本', 'independent': False}], claims, {}, facts, judge)
        self.assertEqual(checked['answer_status'], 'refused')
        self.assertEqual(checked['claims'], [])

    def test_selected_and_latest_facts_survive_evidence_packet_budget(self):
        run = agent.Run.__new__(agent.Run)
        run.assessment, run.latest_spans, run.spans, run.delivered_spans = None, [], {}, set()
        run.facts = {'F' + str(i): {'id': 'F' + str(i), 'text': '实际工具事实' + str(i),
            'evidence_ids': ['E' + str(i)], 'query_id': 'q' + str(i), 'unit': 'untyped'} for i in range(30)}
        packet = run.evidence_packet([{'fact_id': 'F0'}])
        self.assertEqual(packet['facts'][0]['id'], 'F0')
        self.assertIn('F29', {f['id'] for f in packet['facts']})
        self.assertEqual(len(packet['facts']), 24)

    def test_complete_short_rows_are_not_hidden_by_trailing_numeric_cells(self):
        run=agent.Run.__new__(agent.Run)
        run.assessment,run.latest_spans,run.spans,run.delivered_spans=None,[],{},set()
        rows=[{'id':'R'+str(i),'text':'地区'+str(i)+'；2024年；实际指标=3.2；单位=%',
            'kind':'database_row','evidence_ids':['E1'],'query_id':'q1','unit':None} for i in range(17)]
        cells=[{'id':'F'+str(i),'text':'实际数值'+str(i),'kind':'database_value',
            'evidence_ids':['E1'],'query_id':'q1','unit':'untyped'} for i in range(30)]
        run.facts={f['id']:f for f in rows+cells}
        packet=run.evidence_packet([{'fact_id':'F0'}])
        self.assertEqual(packet['facts'][0]['id'],'F0')
        self.assertTrue({f['id'] for f in rows}<={f['id'] for f in packet['facts']})
        self.assertEqual(len(packet['facts']),24)

    def test_missing_year_with_multiple_periods_has_concrete_clarification(self):
        self.statistics_database()
        self.script(schema())
        response = self.ask('星城GDP是多少？')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['answer_status'], 'needs_clarification')
        self.assertEqual(result['clarification_reason'], 'missing_dimensions')
        self.assertEqual(result['clarification_dimensions']['year']['options'], ['2023', '2024'])
        self.assertEqual(result['agent']['calls'], 1)

    def test_definition_history_deliberate_period_and_other_entities_are_not_blocked(self):
        cid = self.statistics_database()
        snapshot = query.inspect(cid)
        for question in ['GDP的统计定义是什么？', '解释GDP历史修订的原因', '给出2024年GDP',
                         '历年GDP分别是多少？', '最新GDP是多少？', '工单有多少个？',
                         '有哪些人口指标？', '人口指标有多少种？', '2100年GDP是多少？',
                         '三地人口能直接比较吗？请核对常住与户籍口径。']:
            self.assertIsNone(query.dimension_clarification(question, snapshot, cid), question)
        self.assertIsNone(query.dimension_clarification('GDP是多少？', snapshot, cid,
            previous={'metric': 'database', 'last_sql': 'SELECT value FROM observations WHERE year=2024'}))

    def test_year_then_population_clarification_retains_original_question(self):
        self.statistics_database()
        self.script(schema())
        first = self.ask('星城人口是多少？').json()
        self.assertEqual(set(first['clarification_dimensions']), {'year', 'population_type'})
        self.script(schema())
        second = self.ask('2024年', previous_context=first['query_spec'])
        self.assertEqual(second.status_code, 200, second.text)
        second = second.json()
        self.assertEqual(set(second['clarification_dimensions']), {'population_type'})
        self.assertEqual(second['query_spec']['semantic_conditions']['year'], 2024)
        self.assertEqual(second['query_spec']['original_question'], '星城人口是多少？')
        next_call = len(fixture.ModelFixture.calls)
        self.script(schema(), sql("SELECT value FROM observations WHERE region_id=? AND year=? AND population_type=?", ['星城', 2024, '常住人口']), fixture.select_fact)
        third = self.ask('常住人口', previous_context=second['query_spec'])
        self.assertEqual(third.status_code, 200, third.text)
        self.assertEqual(third.json()['rows'], [{'value': 100.0}])
        first_payload = json.loads(fixture.ModelFixture.calls[next_call]['messages'][1]['content'])
        self.assertEqual(first_payload['previous_question'], '星城人口是多少？')
        self.assertEqual(first_payload['previous_dimensions']['year'], 2024)

    def test_population_types_do_not_default_to_a_combined_measure(self):
        cid = self.statistics_database()
        snapshot = query.inspect(cid)
        missing = query.dimension_clarification('比较2024年各地人口数值', snapshot, cid)
        self.assertEqual(set(missing['dimensions']), {'population_type'})
        self.assertEqual(missing['dimensions']['population_type']['options'], ['常住人口', '户籍人口'])
        self.assertIsNone(query.dimension_clarification('2024年常住人口多少？', snapshot, cid))
        self.assertIsNone(query.dimension_clarification('2024年各地常住与户籍人口分别多少？', snapshot, cid))
        self.assertIsNone(query.dimension_clarification('比较2024年公布的人均GDP，引用原文说明人口口径，不用年末人口重算',snapshot,cid))
        mixed=query.dimension_clarification('比较2024年各地人口总量，并说明人口口径',snapshot,cid)
        self.assertIn('population_type',mixed['dimensions'])

    def test_a_selling_superlative_asks_for_the_measure_it_ranks_by(self):
        cid = self.statistics_database()
        snapshot = query.inspect(cid)
        missing = query.ranking_metric_clarification('哪张专辑卖得最好？', snapshot)
        self.assertEqual(set(missing['dimensions']), {'sales_metric'})
        self.assertIn('销量', missing['title'])
        self.assertIn('销售额', missing['title'])
        for question in ['哪张专辑销售额最高？', '哪张专辑销量最高？', '按销量统计卖得最好的专辑',
                         '卖得最好的口径是什么', '数据库里一共有多少张专辑', '曲目数最多的前五位艺术家',
                         '播放时长最长的前五首曲目']:
            self.assertIsNone(query.ranking_metric_clarification(question, snapshot), question)
        self.assertIsNone(query.ranking_metric_clarification('哪张专辑卖得最好？', None))
        self.assertIsNone(query.ranking_metric_clarification('哪张专辑卖得最好？', {'tables': []}))

    def test_explicit_source_override_does_not_compare_another_sources_revision(self):
        previous = {'metric': 'database', 'query_scope': 'database', 'database_connection_id': 'old',
                    'database_connection_revision': 2}
        with (patch('backend.query_routing.resolve_context', return_value={'query_scope': 'database',
                    'database_connection_id': 'new', 'routing': {'selected_scope': 'database'}}),
              patch.object(agent.c, 'ready', return_value={'revision': 3})):
            context = agent.query_context(previous=previous, question='切换数据源查询', scope_override='database')
        self.assertEqual(context['database_connection_id'], 'new')
        self.assertEqual(context['database_connection_revision'], 3)
        with (patch('backend.query_routing.resolve_context', return_value={'query_scope': 'database',
                    'database_connection_id': 'old', 'routing': {'selected_scope': 'database'}}),
              patch.object(agent.c, 'ready', return_value={'revision': 3})):
            with self.assertRaisesRegex(ValueError, '配置已变化'):
                agent.query_context(previous=previous, question='继续查询')


if __name__ == '__main__':
    unittest.main()
