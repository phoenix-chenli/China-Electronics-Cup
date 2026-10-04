"""Main-agent contracts with disposable data and a local HTTP model fixture.

These tests verify orchestration and evidence guards, not a real model's quality.
The existing per-module runner starts this file in a separate process.
"""
import copy
import json
import re
import threading
import time
import unittest
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

# This import sets EVIDENCEFLOW_DATA_DIR before importing any backend module.
from tests.test_conversations import LocalClient, TEMP, conv, s
from backend import agent, connections


def tool_results(payload):
    out = []
    for message in payload["messages"]:
        content = message.get("content", "")
        if not isinstance(content, str) or not content.startswith("真实工具返回"):
            continue
        out.append(json.loads(content.split("\n", 1)[1]))
    return out


def select_fact(payload):
    facts = tool_results(payload)[-1]["result"]["facts"]
    return {"action": "answer", "claims": [{"fact_id": facts[0]["id"]}]}


def select_excerpt(payload):
    evidence = tool_results(payload)[-1]["result"]["evidence"][0]
    return {"action": "answer", "claims": [
        {"evidence_id": evidence["id"], "quote": evidence["snippet"]}]}


def query_action(question="2024年华东净销售额是多少？", **extra):
    return {"action": "tool", "tool": "business.query",
            "arguments": {"question": question, **extra}}


class ModelFixture(BaseHTTPRequestHandler):
    script = []
    calls = []
    errors = []
    mode = "ok"
    on_request = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Connection tests use a random arithmetic probe, separate from plans.
        content = payload["messages"][-1]["content"]
        probe = re.fullmatch(r"计算 (\d+)\+(\d+)。只返回整数，不要其他内容。", content)
        if probe:
            text = str(int(probe[1]) + int(probe[2]))
        else:
            self.calls.append(copy.deepcopy(payload))
            if type(self).on_request:
                type(self).on_request(payload)
            if self.mode == "http_error":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"private_provider_detail":"must-not-appear"}')
                return
            try:
                value = self.script.pop(0)
                value = value(payload) if callable(value) else value
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            except Exception as exc:
                self.errors.append(repr(exc))
                text = "fixture-script-error"
        body = {"model": "fixture-main-agent", "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(body).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = LocalClient()
        cls.provider = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
        cls.provider_thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.provider_thread.start()
        cls.endpoint = "http://127.0.0.1:" + str(cls.provider.server_port) + "/v1"

    @classmethod
    def tearDownClass(cls):
        conv.manager.pool.shutdown(wait=True)
        cls.provider.shutdown()
        cls.provider.server_close()
        cls.provider_thread.join(3)
        cls.client.close()
        TEMP.cleanup()

    def setUp(self):
        ModelFixture.script = []
        ModelFixture.calls = []
        ModelFixture.errors = []
        ModelFixture.mode = "ok"
        ModelFixture.on_request = None
        self.configure("local")
        for item in self.client.get("/api/connections").json()["items"]:
            self.client.request("DELETE", "/api/connections/" + item["id"])
        self.client.patch("/api/settings/privacy", json={"local_only": True})

    def tearDown(self):
        self.assertEqual(ModelFixture.errors, [], "HTTP fixture failed to process the protocol")

    def configure(self, mode, connection_id=None):
        response = self.client.request("PUT", "/api/agent/config", json={
            "mode": mode, "connection_id": connection_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ready(self, mode="model"):
        response = self.client.post("/api/connections", json={
            "name": "仅测试的本机主模型", "kind": "text", "endpoint": self.endpoint,
            "model": "fixture-contract", "timeout": 3})
        self.assertEqual(response.status_code, 200, response.text)
        connection = response.json()
        tested = self.client.post("/api/connections/" + connection["id"] + "/test").json()
        self.assertTrue(tested["last_test"]["ok"], tested)
        response = self.client.patch("/api/connections/" + connection["id"] + "/enabled",
                                     json={"enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.configure(mode, connection["id"] if mode == "model" else None)
        return response.json()

    def query(self, question="2024年华东净销售额是多少？", **extra):
        return self.client.post("/api/query", json={"question": question, **extra})

    def script(self, *actions):
        ModelFixture.script = list(actions)

    def expected_sales(self, region="华东"):
        return s.read("SELECT SUM(sales_amount-returned_amount) AS total FROM orders "
                      "WHERE order_date >= ? AND order_date < ? AND region=?",
                      ["2024-01-01", "2025-01-01", region])[0][0]["total"]

    def assert_trace(self, result, terminal="completed"):
        self.assertTrue(result["events"])
        for event in result["events"]:
            self.assertIn(event["status"], {"completed", "failed", "cancelled"})
            self.assertGreaterEqual(event["duration_ms"], 0)
            self.assertIn("started_at", event)
            self.assertIn("finished_at", event)
        if terminal != "completed":
            self.assertTrue(any(e["status"] == terminal for e in result["events"]), result)

    def wait_task(self, tid):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = self.client.get("/api/tasks/" + tid).json()
            if task["status"] not in conv.ACTIVE:
                return task
            time.sleep(.01)
        self.fail("Isolated agent task did not terminate")

    def test_auto_without_model_runs_local_and_local_mode_overrides_ready_model(self):
        self.configure("auto")
        local = self.query().json()
        self.assertEqual(local["agent"]["mode"], "local")
        self.assertEqual(local["metrics"][0]["value"], self.expected_sales())
        self.ready()
        self.configure("local")
        local = self.query().json()
        self.assertEqual(local["agent"]["calls"], 0)
        self.assertEqual(ModelFixture.calls, [])

    def test_auto_one_ready_model_runs_plan_tools_review_and_records_usage(self):
        self.ready("auto")
        action = query_action()
        action["plan"] = [{"id": "query", "label": "读取真实销售数据", "tool": "business.query"},
                          {"id": "review", "label": "核对来源", "tool": "evidence.verify"}]
        self.script(action, {"action": "tool", "tool": "evidence.verify", "arguments": {}},
                    lambda p: {"action": "answer", "claims": [{"fact_id": tool_results(p)[0]["result"]["facts"][0]["id"]}]})
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["agent"]["mode"], "model")
        self.assertEqual(result["agent"]["review"]["status"], "passed")
        self.assertEqual(result["claims"][0]["value"], self.expected_sales())
        self.assertEqual(result["agent"]["calls"], 3)
        self.assertEqual(result["agent"]["usage"]["total_tokens"], 54)
        self.assertEqual(result["agent"]["model"], "fixture-main-agent")
        self.assertEqual([p["status"] for p in result["agent"]["plan"]], ["completed", "completed"])
        self.assertTrue(all(c["evidence_ids"] for c in result["claims"]))
        self.assertEqual(s.get_run(result["run_id"])["agent"], result["agent"])
        self.assert_trace(result)
        query_event = next(e for e in result["events"] if e["tool"] == "business.query")
        self.assertEqual(query_event["outputs"]["facts"][0]["value"], self.expected_sales())
        self.assertTrue(query_event["outputs"]["sql"])
        self.assertEqual(ModelFixture.calls[0]["max_completion_tokens"], 1800)

    def test_document_tool_returns_locatable_exact_excerpt(self):
        self.ready()
        self.script({"action": "tool", "tool": "document.search", "arguments": {
            "query": "2024年激励规则奖金公式", "year": 2024}}, select_excerpt)
        response = self.query("2024年激励规则的奖金公式是什么？")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["claims"][0]["kind"], "excerpt")
        evidence = next(e for e in result["evidence"] if e["id"] == result["claims"][0]["evidence_ids"][0])
        self.assertIn(result["claims"][0]["text"], evidence["snippet"])
        self.assertEqual(evidence["location_status"], "located")
        self.assertTrue(evidence["locator"]["sha256"])
        search_event = next(e for e in result["events"] if e["tool"] == "document.search")
        self.assertGreater(search_event["outputs"]["evidence_count"], 0)
        self.assertTrue(search_event["outputs"]["evidence"][0]["version"])
        self.assertLessEqual(len(search_event["outputs"]["evidence"][0]["snippet"]), 500)

    def test_schema_tool_sends_metadata_without_samples_paths_or_arbitrary_sql(self):
        self.ready()
        self.script({"action": "tool", "tool": "schema.inspect", "arguments": {}},
                    {"action": "answer", "claims": []})
        result = self.query("查看工作区的表结构").json()
        tables = tool_results(ModelFixture.calls[-1])[0]["result"]["tables"]
        self.assertTrue(any(t["name"] == "orders" for t in tables))
        self.assertTrue(all("sample" not in t and "source" not in t and "provenance" not in t for t in tables))
        self.assertNotIn(str(s.DATA), json.dumps(ModelFixture.calls, ensure_ascii=False))
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["agent"]["review"]["status"], "needs_evidence")

    def test_invented_fact_and_repeated_invalid_answer_fail_without_local_fallback(self):
        self.ready()
        wrong = {"action": "answer", "claims": [{"fact_id": "query-unknown-fact"}]}
        self.script(query_action(), wrong, wrong)
        response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        failed = response.json()
        self.assertIn("不存在", failed["detail"])
        self.assertEqual(failed["agent"]["review"]["status"], "failed")
        self.assertEqual(len(ModelFixture.calls), 3)
        self.assertIn("answer_rejected", ModelFixture.calls[-1]["messages"][-1]["content"])
        self.assert_trace(failed, "failed")
        saved = s.get_run(failed["run_id"])
        self.assertEqual(saved["claims"], [])
        self.assertEqual(saved["answer"], "")

    def test_false_number_with_real_fact_id_is_rejected_then_can_be_repaired(self):
        self.ready()
        def wrong_number(payload):
            fact = tool_results(payload)[-1]["result"]["facts"][0]
            return {"action": "answer", "claims": [{"fact_id": fact["id"], "value": 999999999}]}
        self.script(query_action(), wrong_number, select_fact)
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["claims"][0]["value"], self.expected_sales())
        self.assertNotIn("999999999", result["answer"])
        self.assertEqual([e["status"] for e in result["events"] if e["tool"] == "answer.verify"], ["failed", "completed"])

    def test_unknown_tool_and_direct_sql_cannot_mutate_business_data(self):
        self.ready()
        before = s.digest(s.BUSINESS_DB)
        self.script({"action": "tool", "tool": "python.exec", "arguments": {"code": "delete database"}})
        response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("未授权工具", response.json()["detail"])
        self.assertEqual(s.digest(s.BUSINESS_DB), before)
        self.script(query_action(sql="DELETE FROM orders"), {"action": "answer", "claims": []})
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["claims"], [])
        self.assertTrue(any(e["tool"] == "business.query" and e["status"] == "failed" for e in result["events"]))
        self.assertIn("不接受 SQL", ModelFixture.calls[-1]["messages"][-1]["content"])
        self.assertEqual(s.digest(s.BUSINESS_DB), before)

    def test_formula_uses_sourced_values_and_preserves_decimal_result(self):
        self.ready()
        def calculate(payload):
            fact = tool_results(payload)[-1]["result"]["facts"][0]
            return {"action": "tool", "tool": "formula.calculate", "arguments": {
                "expression": "sales + sales", "parameters": [{"name": "sales", "value": str(fact["value"]),
                    "unit": fact["unit"], "evidence_ids": fact["evidence_ids"]}]}}
        self.script(query_action(), calculate, select_fact)
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(Decimal(result["claims"][0]["value"]), Decimal(str(self.expected_sales())) * 2)
        self.assertEqual(result["claims"][0]["unit"], "derived")
        self.assertEqual(result["formulas"][-1]["parameters"][0]["evidence_ids"], result["claims"][0]["evidence_ids"])
        calculation_event = next(e for e in result["events"] if e["tool"] == "formula.calculate")
        self.assertEqual(calculation_event["inputs"]["parameters"][0]["value"], str(self.expected_sales()))
        self.assertEqual(calculation_event["outputs"]["facts"][0]["value"], result["claims"][0]["value"])

    def test_formula_rejects_unsourced_or_falsified_parameters_and_coefficients(self):
        self.ready()
        def forged(payload):
            fact = tool_results(payload)[-1]["result"]["facts"][0]
            return {"action": "tool", "tool": "formula.calculate", "arguments": {
                "expression": "sales", "parameters": [{"name": "sales", "value": "999", "unit": "CNY",
                    "evidence_ids": fact["evidence_ids"]}]}}
        self.script(query_action(), forged, {"action": "tool", "tool": "formula.calculate", "arguments": {
            "expression": "sales", "parameters": [{"name": "sales", "value": "10", "unit": "CNY", "evidence_ids": []}]}},
            {"action": "answer", "claims": []})
        result = self.query().json()
        failed = [e for e in result["events"] if e["tool"] == "formula.calculate"]
        self.assertEqual([e["status"] for e in failed], ["failed", "failed"])
        self.assertIn("事实不一致", failed[0]["summary"])
        self.assertIn("有效来源", failed[1]["summary"])
        self.assertEqual(result["formulas"], [])

    def test_provider_failure_is_visible_with_failed_trace_and_no_fallback(self):
        self.ready()
        ModelFixture.mode = "http_error"
        response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        failed = response.json()
        self.assertIn("HTTP 503", failed["detail"])
        self.assertNotIn("must-not-appear", response.text)
        self.assertEqual(failed["agent"]["mode"], "model")
        self.assertEqual(failed["agent"]["calls"], 1)
        self.assert_trace(failed, "failed")
        self.assertEqual(s.get_run(failed["run_id"])["claims"], [])

    def test_invalid_planner_json_is_not_treated_as_an_answer(self):
        self.ready()
        self.script("这里是模型直接给出的未核验答案：999万元")
        response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("规划 JSON", response.json()["detail"])
        self.assert_trace(response.json(), "failed")

    def test_tool_budget_stops_repeated_calls(self):
        self.ready()
        self.script(*[{"action": "tool", "tool": "schema.inspect", "arguments": {}} for _ in range(4)])
        with patch.object(agent, "MAX_STEPS", 2):
            response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("工具调用次数", response.json()["detail"])
        self.assertEqual(len(ModelFixture.calls), 3)
        self.assertEqual(len([e for e in response.json()["events"] if e["tool"] == "schema.inspect"]), 2)

    def test_model_call_budget_and_total_deadline_are_enforced(self):
        self.ready()
        self.script({"action": "tool", "tool": "schema.inspect", "arguments": {}})
        with patch.object(agent, "MAX_MODEL_CALLS", 1):
            response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("模型调用次数", response.json()["detail"])
        self.assertEqual(len(ModelFixture.calls), 1)
        with patch.object(agent, "MAX_SECONDS", 0):
            response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("总时限", response.json()["detail"])
        self.assertEqual(len(ModelFixture.calls), 1)

    def test_cancel_interrupts_inflight_model_before_any_tool_runs(self):
        self.ready()
        stop, entered, release = threading.Event(), threading.Event(), threading.Event()
        def gate(_):
            entered.set()
            release.wait(3)
        ModelFixture.on_request = gate
        self.script(query_action())
        caught, progress = [], []
        def run():
            try:
                agent.execute("2024年华东净销售额是多少？", cancelled=stop, progress=progress.append, persist=False)
            except BaseException as exc:
                caught.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertIsNone(progress[-1]["events"][0]["duration_ms"])
            stop.set()
            worker.join(1.5)
            self.assertFalse(worker.is_alive(), "Cancel should not wait for provider completion")
            self.assertEqual(len(caught), 1)
            self.assertIsInstance(caught[0], InterruptedError)
            trace = progress[-1]
            self.assertFalse(any(e["tool"] == "business.query" for e in trace["events"]))
            self.assertEqual(trace["events"][0]["status"], "cancelled")
        finally:
            release.set()
            worker.join(3)
            time.sleep(.05)
            ModelFixture.on_request = None

    def test_selected_model_becoming_unavailable_never_silently_runs_local(self):
        connection = self.ready()
        self.client.patch("/api/connections/" + connection["id"] + "/enabled", json={"enabled": False})
        response = self.query()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("测试并启用", response.json()["detail"])
        self.assertEqual(ModelFixture.calls, [])

    def test_business_clarification_reaches_user_without_model_fabricating_year(self):
        self.ready()
        self.script(query_action("华东净销售额是多少？"))
        result = self.query("华东净销售额是多少？").json()
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["clarification"]["field"], "year")
        self.assertEqual(len(ModelFixture.calls), 1)
        self.assertEqual(result["claims"], [])

    def test_model_cannot_change_explicit_time_region_or_user_confirmed_scope(self):
        self.ready()
        self.script(query_action('2025年华北净销售额是多少？',confirmed={'year':2025,'region':'华北'}),select_fact)
        result=self.query().json()
        self.assertEqual(result['query_spec']['year'],2024)
        self.assertEqual(result['query_spec']['region'],'华东')
        self.assertEqual(result['claims'][0]['value'],self.expected_sales())
        self.script(query_action(),select_fact)
        result=self.query(confirmed={'region':'华北'}).json()
        self.assertEqual(result['query_spec']['region'],'华北')
        self.assertEqual(result['claims'][0]['value'],self.expected_sales('华北'))

    def test_model_invented_year_still_requires_user_clarification(self):
        self.ready()
        self.script(query_action('2024年华东净销售额是多少？'))
        result=self.query('华东净销售额是多少？').json()
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(result['clarification']['field'],'year')
        self.assertEqual(result['claims'],[])

    def test_canonical_evidence_gap_is_preserved_without_verified_coverage(self):
        self.ready()
        def gap(payload):
            fact=next(f for f in tool_results(payload)[-1]['result']['facts'] if f['kind']=='gap')
            return {'action':'answer','claims':[{'fact_id':fact['id']}]}
        self.script(query_action('2024年华北销售冠军并说明成功经验'),gap)
        response=self.query('2024年华北销售冠军并说明成功经验')
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['agent']['review']['status'],'needs_evidence')
        self.assertEqual(result['claims'][0]['status'],'needs_evidence')
        self.assertEqual(result['coverage']['supported'],0)
        self.assertEqual(result['coverage']['percent'],0)

    def test_canonical_document_fact_does_not_bypass_original_location_check(self):
        self.ready()
        self.script(query_action('2024年激励规则的奖金公式是什么？'),select_fact,select_fact)
        with patch.object(agent.documents,'resolve',return_value={'status':'unlocated','locator':{}}):
            response=self.query('2024年激励规则的奖金公式是什么？')
        self.assertEqual(response.status_code,400,response.text)
        self.assertIn('原文无法定位',response.json()['detail'])
        self.assert_trace(response.json(),'failed')

    def test_task_route_inherits_server_context_and_persists_failed_events(self):
        self.ready()
        sid = self.client.post("/api/projects/default/sessions").json()["id"]
        self.script(query_action(), select_fact)
        first = self.client.post("/api/tasks", json={"session_id": sid, "question": "2024年华东净销售额是多少？",
                                                     "request_id": str(uuid.uuid4())})
        first = self.wait_task(first.json()["id"])
        self.assertEqual(first["status"], "completed", first)
        mid = self.client.get("/api/sessions/" + sid).json()["selected_message_id"]
        self.script(query_action("那华北呢？"), select_fact)
        second = self.client.post("/api/tasks", json={"session_id": sid, "question": "那华北呢？",
            "parent_message_id": mid, "previous_context": {"year": 2099}, "request_id": str(uuid.uuid4())})
        second = self.wait_task(second.json()["id"])
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["result"]["query_spec"]["year"], 2024)
        self.assertEqual(second["result"]["claims"][0]["value"], self.expected_sales("华北"))
        initial = json.loads(ModelFixture.calls[-2]["messages"][1]["content"])
        self.assertEqual(initial["previous_conditions"]["year"], 2024)
        self.assertNotIn("available_products", initial["previous_conditions"])
        ModelFixture.mode = "http_error"
        failed = self.client.post("/api/tasks", json={"session_id": sid, "question": "2024年华南净销售额是多少？",
                                                      "request_id": str(uuid.uuid4())})
        failed = self.wait_task(failed.json()["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["result"])
        self.assertEqual(failed["agent"]["review"]["status"], "failed")
        self.assert_trace(failed, "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
