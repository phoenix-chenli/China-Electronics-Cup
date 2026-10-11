"""Stage 4 integration tests, always using disposable isolated data."""
import os
import tempfile
import threading
import time
import unittest
import uuid
import json
from pathlib import Path

TEMP = tempfile.TemporaryDirectory(prefix="evidenceflow-stage4-")
os.environ["EVIDENCEFLOW_DATA_DIR"] = TEMP.name
from backend import main, conversations as conv, store as s, engine
import socket
import urllib.request
import urllib.error
import uvicorn
from types import SimpleNamespace

class LocalClient:
    def __init__(self):
        sock = socket.socket(); sock.bind(('127.0.0.1', 0))
        self.url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(main.app, log_level='error'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline: time.sleep(.01)
        if not self.server.started: raise RuntimeError('Test server failed to start')
    def request(self, method, path, **kwargs):
        body = json.dumps(kwargs['json']).encode() if 'json' in kwargs else b'' if method != 'GET' else None
        req = urllib.request.Request(self.url + path, data=body, method=method, headers={'Content-Type': 'application/json'})
        # A model-driven query may make several bounded calls before responding.
        # Keep the client alive long enough to avoid leaking an unfinished run
        # into the next scripted fixture, especially on Windows CPU runtimes.
        try: response = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc: response = exc
        text = response.read().decode()
        return SimpleNamespace(status_code=response.code, text=text, json=lambda: json.loads(text))
    def get(self, path, **kwargs): return self.request('GET', path, **kwargs)
    def post(self, path, **kwargs): return self.request('POST', path, **kwargs)
    def patch(self, path, **kwargs): return self.request('PATCH', path, **kwargs)
    def close(self):
        self.server.should_exit = True; self.thread.join(5)



class ConversationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = LocalClient()

    @classmethod
    def tearDownClass(cls):
        conv.manager.pool.shutdown(wait=True)
        cls.client.close()
        TEMP.cleanup()

    def new_session(self, project="default"):
        response = self.client.post(f"/api/projects/{project}/sessions")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def wait(self, manager, tid):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = manager.get(tid)
            if task["status"] not in conv.ACTIVE: return task
            time.sleep(.01)
        self.fail("Task did not reach a terminal state")

    def query(self, sid, question, **extra):
        response = self.client.post("/api/tasks", json={"session_id": sid, "question": question, "request_id": str(uuid.uuid4()), **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return self.wait(conv.manager, response.json()["id"])

    def detail(self, sid): return self.client.get(f"/api/sessions/{sid}").json()

    def test_multi_turn_refresh_and_context_isolation(self):
        sid = self.new_session()
        # Oracle reads the fixture data independently; production may contain imports.
        east = s.read("SELECT MAX(total) AS amount FROM (SELECT SUM(sales_amount-returned_amount) AS total FROM orders WHERE order_date >= '2024-01-01' AND order_date <= '2024-12-31' AND region='华东' GROUP BY product_id)")[0][0]["amount"]
        north = s.read("SELECT MAX(total) AS amount FROM (SELECT SUM(sales_amount-returned_amount) AS total FROM orders WHERE order_date >= '2024-01-01' AND order_date <= '2024-12-31' AND region='华北' GROUP BY product_id)")[0][0]["amount"]
        r = self.query(sid, "2024 年华东销售冠军是谁？说明奖金。")
        self.assertEqual(r["result"]["metrics"][0]["value"], east)
        mid = self.detail(sid)["selected_message_id"]
        second = self.query(sid, "那华北呢？", parent_message_id=mid)
        self.assertEqual(second["result"]["query_spec"]["year"], 2024)
        self.assertEqual(second["result"]["query_spec"]["region"], "华北")
        self.assertEqual(second["result"]["metrics"][0]["value"], north)
        detail = self.detail(sid)
        self.assertEqual(len(detail["messages"]), 4)
        self.assertEqual(detail["messages"][1]["result"]["metrics"][0]["value"], east)
        pid = self.client.post("/api/projects", json={"name": "独立项目"}).json()["id"]
        other = self.new_session(pid)
        blocked = self.client.post("/api/tasks", json={"session_id": other, "question": "它的奖金", "parent_message_id": mid})
        self.assertEqual(blocked.status_code, 400)
        isolated = self.query(other, "华北销售额是多少？", previous_context={"year": 2024})
        self.assertEqual(isolated["result"]["status"], "needs_clarification")

    def test_five_turns_selection_undo_recompute(self):
        sid = self.new_session()
        for text in ["2024 年华东销售冠军是谁？", "它的奖金是多少？", "那华北呢？", "换成华南", "与 2023 年对比增长率是多少？"]:
            d = self.detail(sid)
            task = self.query(sid, text, parent_message_id=d["selected_message_id"])
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["result"]["status"], "completed")
        self.assertEqual(task["result"]["query_spec"]["year"], 2024)
        self.assertEqual(task["result"]["query_spec"]["compare_year"], 2023)
        d = self.detail(sid)
        self.assertEqual(len(d["messages"]), 10)
        first = d["messages"][1]
        self.client.patch(f"/api/sessions/{sid}", json={"selected_message_id": first["id"]})
        self.assertEqual(self.detail(sid)["selected_message_id"], first["id"])
        changed = self.query(sid, first["result"]["question"], mode="recompute", parent_message_id=first["id"], confirmed={"region": "华北"})
        self.assertEqual(changed["result"]["query_spec"]["region"], "华北")
        self.assertEqual(changed["result"]["parent_run_id"], first["result"]["run_id"])
        self.client.patch(f"/api/sessions/{sid}", json={"selected_message_id": first["id"]})
        self.assertEqual(len(self.detail(sid)["messages"]), 12)

    def test_idempotency_and_no_parallel_turns(self):
        entered, release = threading.Event(), threading.Event()
        def gated(*args, **kwargs):
            entered.set(); release.wait(4)
            return engine.execute(*args, **kwargs)
        manager = conv.TaskManager(workers=1, executor=gated)
        sid = self.new_session()
        body = {"session_id": sid, "question": "2024 年华东净销售额", "request_id": "same"}
        try:
            a = manager.submit(body); self.assertTrue(entered.wait(2))
            self.assertEqual(manager.submit(body)["id"], a["id"])
            with self.assertRaises(Exception) as cm: manager.submit({**body, "request_id": "new"})
            self.assertEqual(cm.exception.status_code, 409)
            release.set(); self.assertEqual(self.wait(manager, a["id"])["status"], "completed")
            self.assertEqual(manager.submit(body)["id"], a["id"])
            self.assertEqual(len(self.detail(sid)["messages"]), 2)
        finally: release.set(); manager.pool.shutdown(wait=True)

    def test_running_cancel_wins_late_result_and_queued_cancel(self):
        entered, release = threading.Event(), threading.Event()
        def late(*args, **kwargs):
            result = engine.execute(*args, **kwargs)
            entered.set(); release.wait(4)
            return result
        manager = conv.TaskManager(workers=1, executor=late)
        sid, sid2 = self.new_session(), self.new_session()
        try:
            a = manager.submit({"session_id": sid, "question": "2024 年华东净销售额"})
            self.assertTrue(entered.wait(2))
            b = manager.submit({"session_id": sid2, "question": "2024 年华北净销售额"})
            self.assertEqual(manager.cancel(b["id"])["status"], "cancelled")
            self.assertEqual(manager.cancel(a["id"])["status"], "cancelling")
            release.set()
            done = self.wait(manager, a["id"])
            self.assertEqual(done["status"], "cancelled")
            self.assertIsNone(done["result"])
            self.assertIsNone(self.detail(sid)["messages"][1]["result"])
            self.assertEqual(self.detail(sid)["messages"][0]["status"], "completed")
            retry = manager.retry(a["id"], "retry-1")
            self.assertEqual(self.wait(manager, retry["id"])["status"], "completed")
            self.assertEqual(manager.cancel(retry["id"])["status"], "completed")
        finally: release.set(); manager.pool.shutdown(wait=True)

    def test_interrupt_actual_sql(self):
        entered = threading.Event()
        def sql_query(*args, **kwargs):
            entered.set()
            s.read("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n")
            return engine.execute(*args, **kwargs)
        manager = conv.TaskManager(workers=1, executor=sql_query)
        try:
            t = manager.submit({"session_id": self.new_session(), "question": "2024 年净销售额"})
            self.assertTrue(entered.wait(2)); manager.cancel(t["id"])
            self.assertEqual(self.wait(manager, t["id"])["status"], "cancelled")
        finally: manager.pool.shutdown(wait=True)

    def test_failure_retry_keeps_failure(self):
        calls = []
        def fails_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1: raise OSError("测试夹具：数据源暂不可读")
            return engine.execute(*args, **kwargs)
        manager = conv.TaskManager(executor=fails_once)
        sid = self.new_session()
        try:
            a = manager.submit({"session_id": sid, "question": "2024 年华东净销售额"})
            self.assertEqual(self.wait(manager, a["id"])["status"], "failed")
            b = manager.retry(a["id"], "retry-id")
            self.assertEqual(self.wait(manager, b["id"])["status"], "completed")
            self.assertEqual(manager.retry(a["id"], "retry-id")["id"], b["id"])
            self.assertEqual([m["status"] for m in self.detail(sid)["messages"] if m["role"] == "assistant"], ["failed", "completed"])
        finally: manager.pool.shutdown(wait=True)

    def test_restart_recovers_orphan_and_migration_keeps_legacy(self):
        sid, tid = self.new_session(), str(uuid.uuid4())
        with s.conn() as c:
            c.execute("INSERT INTO ef_tasks VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?)", (tid, sid, "orphan", '{}', 'running', '查询中', s.now(), s.now()))
            c.execute("INSERT INTO ef_messages(id,session_id,role,text,status,task_id,created_at) VALUES (?,?,'assistant','','running',?,?)", (str(uuid.uuid4()), sid, tid, s.now()))
            before = c.execute("SELECT COUNT(*) FROM saved_analyses").fetchone()[0]
        conv.initialize()
        self.assertEqual(conv.manager.get(tid)["status"], "failed")
        self.assertIn("服务重启", conv.manager.get(tid)["error"])
        with s.conn() as c: self.assertEqual(c.execute("SELECT COUNT(*) FROM saved_analyses").fetchone()[0], before)
        self.assertTrue((s.DATA / "backups/before-conversations.db").exists())

    def test_restart_terminalizes_model_traces_and_preserves_completed_work(self):
        interrupted = []
        started = "2024-01-01T00:00:00.000+00:00"
        completed_event = {"step_id": "finished", "status": "completed", "duration_ms": 12.5,
                           "started_at": started, "finished_at": "2024-01-01T00:00:00.013+00:00"}
        for state in conv.ACTIVE:
            sid, tid, stamp = self.new_session(), str(uuid.uuid4()), s.now()
            request = {"session_id": sid, "question": "2024 年华东净销售额", "request_id": "restart-" + state}
            trace = {"events": [completed_event,
                                {"step_id": "rejected", "status": "failed", "summary": "此前步骤失败", "duration_ms": 3},
                                {"step_id": "in-flight", "status": "running", "started_at": started, "duration_ms": 0}],
                     "agent": {"mode": "model", "model": "isolated-fixture", "calls": 2, "usage": {"total_tokens": 18},
                               "review": {"status": "pending", "detail": "尚未核验"},
                               "plan": [{"id": "done", "status": "completed"}, {"id": "rejected", "status": "failed"},
                                        {"id": "current", "status": "running"}, {"id": "next", "status": "pending"}]}}
            with s.conn() as c:
                c.execute("INSERT INTO ef_tasks VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
                          (tid, sid, request["request_id"], conv.encode(request), state, "测试中的模型调用", stamp, stamp))
                c.execute("INSERT INTO ef_messages(id,session_id,role,text,status,task_id,created_at) VALUES (?,?,'assistant','',?,?,?)",
                          (str(uuid.uuid4()), sid, state, tid, stamp))
                c.execute("INSERT INTO ef_task_traces VALUES (?,?)", (tid, conv.encode(trace)))
            interrupted.append((sid, tid))
        complete_sid, complete_tid, stamp = self.new_session(), str(uuid.uuid4()), s.now()
        completed_trace = conv.encode({"events": [completed_event], "agent": {"mode": "model", "review": {"status": "passed"}}})
        with s.conn() as c:
            c.execute("INSERT INTO ef_tasks VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
                      (complete_tid, complete_sid, "completed-control", "{}", "completed", "查询完成", stamp, stamp))
            c.execute("INSERT INTO ef_task_traces VALUES (?,?)", (complete_tid, completed_trace))
        conv.initialize()
        for sid, tid in interrupted:
            with self.subTest(task_id=tid):
                task = self.client.get("/api/tasks/" + tid).json()
                self.assertEqual(task["status"], "failed")
                self.assertIn("服务重启", task["error"])
                self.assertIsNone(task["result"])
                self.assertEqual(task["events"][0], completed_event)
                self.assertEqual(task["events"][1]["summary"], "此前步骤失败")
                event = task["events"][2]
                self.assertEqual(event["status"], "failed")
                self.assertIn("服务重启", event["summary"])
                self.assertIsNone(event["finished_at"])
                self.assertIsNone(event["duration_ms"])
                self.assertEqual(event["recovered_at"], task["updated_at"])
                self.assertEqual([p["status"] for p in task["agent"]["plan"]], ["completed", "failed", "failed", "skipped"])
                self.assertEqual(task["agent"]["review"]["status"], "failed")
                self.assertEqual(task["agent"]["calls"], 2)
                self.assertEqual(task["agent"]["usage"], {"total_tokens": 18})
                detail = self.detail(sid)
                self.assertEqual(detail["tasks"][0], task)
                self.assertEqual(detail["messages"][0]["status"], "failed")
                self.assertIsNone(detail["messages"][0]["result"])
        with s.conn() as c:
            self.assertEqual(c.execute("SELECT body FROM ef_task_traces WHERE task_id=?", (complete_tid,)).fetchone()[0], completed_trace)
            recovered_trace = c.execute("SELECT body FROM ef_task_traces WHERE task_id=?", (interrupted[0][1],)).fetchone()[0]
        conv.initialize()
        with s.conn() as c:
            self.assertEqual(c.execute("SELECT body FROM ef_task_traces WHERE task_id=?", (interrupted[0][1],)).fetchone()[0], recovered_trace)
        retry = conv.manager.retry(interrupted[0][1], "restart-retry")
        self.assertEqual(self.wait(conv.manager, retry["id"])["status"], "completed")
        self.assertEqual(conv.manager.get(interrupted[0][1])["status"], "failed")

    def test_failed_task_finishes_running_trace_and_skips_unexecuted_plan(self):
        def fails_after_progress(*args, **kwargs):
            kwargs["progress"]({"stage": "模型调用中", "events": [{"step_id": "call", "status": "running", "started_at": s.now()}],
                                "agent": {"mode": "model", "plan": [{"status": "running"}, {"status": "pending"}]}})
            raise OSError("隔离夹具：模型连接中断")
        manager = conv.TaskManager(workers=1, executor=fails_after_progress)
        try:
            queued = manager.submit({"session_id": self.new_session(), "question": "2024 年华东净销售额"})
            task = self.wait(manager, queued["id"])
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["events"][0]["status"], "failed")
            self.assertIn("模型连接中断", task["events"][0]["summary"])
            self.assertIsNotNone(task["events"][0]["finished_at"])
            self.assertGreaterEqual(task["events"][0]["duration_ms"], 0)
            self.assertNotIn("recovered_at", task["events"][0])
            self.assertEqual([p["status"] for p in task["agent"]["plan"]], ["failed", "skipped"])
            self.assertEqual(task["agent"]["review"]["status"], "failed")
        finally:
            manager.pool.shutdown(wait=True)

    def test_import_saved_snapshot_and_rename(self):
        r = self.query(self.new_session(), "2024 年华东销售冠军是谁？")
        saved = self.client.post('/api/saved', json={"name": "旧保存分析", "run_id": r["result"]["run_id"]}).json()
        sid = self.new_session()
        response = self.client.post(f'/api/sessions/{sid}/snapshot', json={"saved_id": saved["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][1]["result"]["run_id"], r["result"]["run_id"])
        self.client.patch(f'/api/sessions/{sid}', json={"title": "改名后的会话"})
        self.assertEqual(self.detail(sid)["title"], "改名后的会话")
        self.assertTrue(any(x['id'] == saved['id'] for x in self.client.get('/api/saved').json()))

    def test_fully_stated_new_scope_drops_old_product(self):
        sid = self.new_session()
        self.query(sid, "2024 年华北销售冠军是谁？")
        self.query(sid, "它的奖金是多少？", parent_message_id=self.detail(sid)["selected_message_id"])
        result = self.query(sid, "2024 年华东净销售额是多少？", parent_message_id=self.detail(sid)["selected_message_id"])["result"]
        expected = s.read("SELECT SUM(sales_amount-returned_amount) AS amount FROM orders WHERE strftime('%Y',order_date)='2024' AND region='华东'")[0][0]["amount"]
        self.assertIsNone(result["query_spec"]["product"])
        self.assertEqual(result["metrics"][0]["value"], expected)


if __name__ == "__main__": unittest.main(verbosity=2)
