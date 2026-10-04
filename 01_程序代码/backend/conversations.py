"""Durable local conversations and cancellable query jobs.

Projects organise conversations; all currently use the same sales-demo data source.
No task result is published until its terminal transaction wins against cancellation.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from . import store as s, engine, agent

router = APIRouter(prefix="/api")
ACTIVE = ("queued", "running", "cancelling")


def encode(value): return json.dumps(value, ensure_ascii=False)
def uid(): return str(uuid.uuid4())


def terminalize_trace(c, tid, status, stamp, error=None, interrupted=False):
    """Keep the persisted execution snapshot consistent with a terminal task."""
    trace = c.execute("SELECT body FROM ef_task_traces WHERE task_id=?", (tid,)).fetchone()
    if not trace or status == "completed": return
    body = json.loads(trace[0])
    detail = "查询已取消" if status == "cancelled" else error or "查询未完成"
    for event in body.get("events", []):
        if event.get("status") == "running":
            event.update(status="cancelled" if status == "cancelled" else "failed", summary=detail)
            if interrupted:
                # Recovery time is known; the process's actual finish time and
                # execution duration before a crash are not.
                event.update(finished_at=None, duration_ms=None, recovered_at=stamp)
            else:
                event["finished_at"] = stamp
                if event.get("started_at"):
                    event["duration_ms"] = round((datetime.fromisoformat(stamp) - datetime.fromisoformat(event["started_at"])).total_seconds() * 1000, 2)
    if body.get("agent"):
        body["agent"]["review"] = {"status": status, "detail": detail}
        for item in body["agent"].get("plan", []):
            if item.get("status") in {"pending", "running"}:
                item["status"] = "cancelled" if status == "cancelled" else "failed" if item["status"] == "running" else "skipped"
    c.execute("UPDATE ef_task_traces SET body=? WHERE task_id=?", (encode(body), tid))


def initialize():
    # A SQLite backup includes committed WAL pages, unlike copying the db file.
    with s.conn() as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='ef_sessions'").fetchone()
    if not exists:
        backup = s.DATA / "backups" / "before-conversations.db"
        backup.parent.mkdir(exist_ok=True)
        if not backup.exists():
            with s.conn() as src:
                dst = sqlite3.connect(backup)
                try: src.backup(dst)
                finally: dst.close()
    with s.conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ef_task_traces(task_id TEXT PRIMARY KEY,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_projects(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,workspace_id TEXT NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_sessions(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL,title TEXT NOT NULL,
          selected_message_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_messages(
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
          session_id TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,
          status TEXT NOT NULL,result_json TEXT,task_id TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_tasks(
          id TEXT PRIMARY KEY,session_id TEXT,request_id TEXT NOT NULL,
          request_json TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,error TEXT,
          result_json TEXT,attempt_of TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          UNIQUE(session_id,request_id));
        CREATE INDEX IF NOT EXISTS ef_messages_session ON ef_messages(session_id,sequence);
        CREATE INDEX IF NOT EXISTS ef_sessions_project ON ef_sessions(project_id,updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ef_one_active_task ON ef_tasks(session_id)
          WHERE status IN ('queued','running','cancelling');
        """)
        c.execute("BEGIN IMMEDIATE")
        stamp = s.now()
        c.execute("INSERT OR IGNORE INTO ef_projects VALUES (?,?,?,?,?)",
                  ("default", "销售分析", "sales-demo", stamp, stamp))
        # Interrupted work cannot be reported as completed or silently replayed.
        error = "服务重启，查询已中断。可以重试。"
        interrupted = c.execute("SELECT id FROM ef_tasks WHERE status IN ('queued','running','cancelling')").fetchall()
        for row in interrupted:
            terminalize_trace(c, row["id"], "failed", stamp, error, interrupted=True)
        c.execute("UPDATE ef_messages SET status='failed',text=? WHERE role='assistant' AND task_id IN (SELECT id FROM ef_tasks WHERE status IN ('queued','running','cancelling'))", (error,))
        c.execute("UPDATE ef_tasks SET status='failed',stage='查询中断',error=?,updated_at=? WHERE status IN ('queued','running','cancelling')", (error, stamp))


def require_session(c, sid):
    row = c.execute("SELECT * FROM ef_sessions WHERE id=?", (sid,)).fetchone()
    if not row: raise HTTPException(404, "会话不存在")
    return dict(row)


def result_message(c, sid, mid):
    row = c.execute("SELECT * FROM ef_messages WHERE id=? AND session_id=? AND role='assistant' AND result_json IS NOT NULL", (mid, sid)).fetchone()
    if not row: raise HTTPException(400, "这条结果不属于当前会话")
    return json.loads(row["result_json"])


class Name(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def name(value):
    if not value.strip(): raise ValueError("名称不能为空")
    return value.strip()


@router.get("/projects")
def projects():
    with s.conn() as c:
        return [dict(r) for r in c.execute("SELECT p.*, (SELECT COUNT(*) FROM ef_sessions s WHERE s.project_id=p.id) AS session_count FROM ef_projects p ORDER BY p.created_at")]


@router.post("/projects")
def create_project(req: Name):
    pid, stamp = uid(), s.now()
    with s.conn() as c: c.execute("INSERT INTO ef_projects VALUES (?,?,?,?,?)", (pid, name(req.name), "sales-demo", stamp, stamp))
    return {"id": pid, "name": name(req.name)}


@router.patch("/projects/{pid}")
def rename_project(pid: str, req: Name):
    with s.conn() as c:
        if not c.execute("UPDATE ef_projects SET name=?,updated_at=? WHERE id=?", (name(req.name), s.now(), pid)).rowcount: raise HTTPException(404, "项目不存在")
    return {"ok": True}


@router.get("/projects/{pid}/sessions")
def sessions(pid: str):
    with s.conn() as c:
        return [dict(r) for r in c.execute("""SELECT s.*,
          (SELECT COUNT(*) FROM ef_messages m WHERE m.session_id=s.id AND m.role='user') AS turn_count,
          (SELECT status FROM ef_tasks t WHERE t.session_id=s.id ORDER BY t.created_at DESC LIMIT 1) AS task_status
          FROM ef_sessions s WHERE s.project_id=? ORDER BY s.updated_at DESC""", (pid,))]


@router.post("/projects/{pid}/sessions")
def create_session(pid: str):
    sid, stamp = uid(), s.now()
    with s.conn() as c:
        if not c.execute("SELECT 1 FROM ef_projects WHERE id=?", (pid,)).fetchone(): raise HTTPException(404, "项目不存在")
        c.execute("INSERT INTO ef_sessions VALUES (?,?,?,NULL,?,?)", (sid, pid, "新会话", stamp, stamp))
    return session(sid)


@router.get("/sessions/{sid}")
def session(sid: str):
    with s.conn() as c:
        c.execute("BEGIN")
        detail = require_session(c, sid)
        messages = []
        for row in c.execute("SELECT * FROM ef_messages WHERE session_id=? ORDER BY sequence", (sid,)):
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json") or "null")
            messages.append(item)
        detail["messages"] = messages
        detail["tasks"] = [task_row(r) for r in c.execute("SELECT * FROM ef_tasks WHERE session_id=? ORDER BY created_at", (sid,))]
    detail["data_version"] = s.data_version()
    return detail


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    selected_message_id: str | None = None


@router.patch("/sessions/{sid}")
def update_session(sid: str, req: SessionPatch):
    with s.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        require_session(c, sid)
        if req.title is not None: c.execute("UPDATE ef_sessions SET title=?,updated_at=? WHERE id=?", (name(req.title), s.now(), sid))
        if req.selected_message_id:
            result_message(c, sid, req.selected_message_id)
            c.execute("UPDATE ef_sessions SET selected_message_id=? WHERE id=?", (req.selected_message_id, sid))
    return session(sid)


class SnapshotImport(BaseModel):
    saved_id: str


@router.post("/sessions/{sid}/snapshot")
def import_snapshot(sid: str, req: SnapshotImport):
    with s.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        require_session(c, sid)
        if c.execute("SELECT 1 FROM ef_messages WHERE session_id=?", (sid,)).fetchone(): raise HTTPException(409, "请在新会话中打开快照")
        saved = c.execute("SELECT * FROM saved_analyses WHERE id=? AND deleted_at IS NULL", (req.saved_id,)).fetchone()
        if not saved: raise HTTPException(404, "保存记录不存在")
        result = json.loads(saved["result_json"])
        mid, stamp = uid(), s.now()
        c.execute("INSERT INTO ef_messages(id,session_id,role,text,status,created_at) VALUES (?,?,'user',?,'completed',?)", (uid(), sid, saved["question"], stamp))
        c.execute("INSERT INTO ef_messages(id,session_id,role,text,status,result_json,created_at) VALUES (?,?,'assistant',?,'completed',?,?)", (mid, sid, result.get("answer") or result.get("headline", ""), saved["result_json"], stamp))
        c.execute("UPDATE ef_sessions SET title=?,selected_message_id=?,updated_at=? WHERE id=?", (saved["name"], mid, stamp, sid))
    return session(sid)


def task_row(row):
    d = dict(row)
    d.pop("request_json", None)
    d["result"] = json.loads(d.pop("result_json") or "null")
    with s.conn() as c:
        trace=c.execute("SELECT body FROM ef_task_traces WHERE task_id=?",(d['id'],)).fetchone()
    d.update(json.loads(trace[0]) if trace else {"events":[],"agent":None})
    return d


class TaskManager:
    def __init__(self, workers=4, executor=None):
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.executor = executor or agent.execute
        self.stops = {}
        self.lock = threading.Lock()

    def get(self, tid):
        with s.conn() as c: row = c.execute("SELECT * FROM ef_tasks WHERE id=?", (tid,)).fetchone()
        if not row: raise HTTPException(404, "查询不存在")
        return task_row(row)

    def submit(self, req, attempt_of=None):
        req = dict(req)
        sid, tid, stamp = req.get("session_id"), uid(), s.now()
        reqid = req.get("request_id") or uid()
        with s.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if sid:
                conversation = require_session(c, sid)
                existing = c.execute("SELECT * FROM ef_tasks WHERE session_id=? AND request_id=?", (sid, reqid)).fetchone()
                if existing: return task_row(existing)
                if c.execute("SELECT 1 FROM ef_tasks WHERE session_id=? AND status IN ('queued','running','cancelling')", (sid,)).fetchone(): raise HTTPException(409, "这个会话仍在查询，可以等待或取消")
                parent_id = req.get("parent_message_id")
                parent = result_message(c, sid, parent_id) if parent_id else None
                if req.get("mode") == "recompute":
                    if not parent: raise ValueError("请先选择要修改的结果")
                    changed = req.get("confirmed", {})
                    req["preserved_origins"] = {k:v for k,v in parent["query_spec"].get("origins", {}).items() if k not in changed}
                    req["question"] = parent["question"]
                    req["confirmed"] = {**parent["query_spec"], **req.get("confirmed", {})}
                    req["parent_run_id"] = parent["run_id"]
                # Never accept client-provided context for persisted sessions.
                req["previous_context"] = parent["query_spec"] if parent and req.get("inherit_context", True) else {}
                req["workspace_id"] = c.execute("SELECT workspace_id FROM ef_projects WHERE id=?", (conversation["project_id"],)).fetchone()[0]
            if req.get("workspace_id", "sales-demo") != "sales-demo": raise ValueError("工作区不存在")
            if c.execute("SELECT COUNT(*) FROM ef_tasks WHERE status IN ('queued','running','cancelling')").fetchone()[0] >= 12: raise HTTPException(429, "查询队列已满，请稍后重试")
            c.execute("INSERT INTO ef_tasks VALUES (?,?,?,?,?,?,NULL,NULL,?,?,?)", (tid, sid, reqid, encode(req), "queued", "等待查询", attempt_of, stamp, stamp))
            if sid:
                text = req.get("display_question") or req["question"]
                if attempt_of: text = "重试：" + text
                c.execute("INSERT INTO ef_messages(id,session_id,role,text,status,task_id,created_at) VALUES (?,?,'user',?,'completed',?,?)", (uid(), sid, text, tid, stamp))
                c.execute("INSERT INTO ef_messages(id,session_id,role,text,status,task_id,created_at) VALUES (?,?,'assistant','','queued',?,?)", (uid(), sid, tid, stamp))
                c.execute("UPDATE ef_sessions SET title=CASE WHEN title='新会话' THEN ? ELSE title END,updated_at=? WHERE id=?", (req["question"][:60], stamp, sid))
        stop = threading.Event()
        with self.lock: self.stops[tid] = stop
        self.pool.submit(self.work, tid, req, stop)
        return self.get(tid)

    def work(self, tid, req, stop):
        try:
            with s.conn() as c:
                c.execute("BEGIN IMMEDIATE")
                if not c.execute("UPDATE ef_tasks SET status='running',stage='开始查询',updated_at=? WHERE id=? AND status='queued'", (s.now(), tid)).rowcount: return
                c.execute("UPDATE ef_messages SET status='running' WHERE task_id=? AND role='assistant'", (tid,))
            def progress(label):
                trace=label if isinstance(label,dict) else None
                stage=trace.get('stage','正在查询') if trace else label
                with s.conn() as c:
                    c.execute('BEGIN IMMEDIATE')
                    changed=c.execute("UPDATE ef_tasks SET stage=?,updated_at=? WHERE id=? AND status='running'", (stage, s.now(), tid)).rowcount
                    if changed and trace:
                        c.execute('INSERT OR REPLACE INTO ef_task_traces VALUES (?,?)',(tid,encode({k:trace[k] for k in ('events','agent') if k in trace})))
            with s.query_cancellation(stop):
                extra = {"document_context":req["document_context"]} if req.get("document_context") else {}
                result = self.executor(req["question"], req.get("previous_context"), req.get("confirmed"), cancelled=stop, progress=progress, persist=False, **extra)
            if req.get("parent_run_id"): result["parent_run_id"] = req["parent_run_id"]
            if req.get("preserved_origins"):
                result["query_spec"]["origins"].update(req["preserved_origins"])
                result["conditions"] = engine.conditions(result["query_spec"])
            result["session_id"] = req.get("session_id")
            self.finish(tid, "completed", result=result)
        except InterruptedError:
            self.finish(tid, "cancelled")
        except Exception as exc:
            self.finish(tid, "failed", error=str(exc)[:500])
        finally:
            with self.lock: self.stops.pop(tid, None)

    def finish(self, tid, status, result=None, error=None):
        stamp = s.now()
        with s.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            task = c.execute("SELECT * FROM ef_tasks WHERE id=?", (tid,)).fetchone()
            if not task or task["status"] not in ACTIVE: return
            if task["status"] == "cancelling": status = "cancelled"
            if status != "completed": result = None
            terminalize_trace(c, tid, status, stamp, error)
            text = ((result.get("answer") or result.get("clarification", {}).get("title") or result.get("headline", "")) if result else "查询已取消" if status == "cancelled" else error or "查询未完成")
            c.execute("UPDATE ef_tasks SET status=?,stage=?,error=?,result_json=?,updated_at=? WHERE id=?", (status, {"completed": "查询完成", "cancelled": "已取消", "failed": "查询失败"}[status], error if status == "failed" else None, encode(result) if result else None, stamp, tid))
            c.execute("UPDATE ef_messages SET status=?,text=?,result_json=? WHERE task_id=? AND role='assistant'", (status, text, encode(result) if result else None, tid))
            if result:
                c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", (result["run_id"], result["question"], result["status"], stamp, encode(result)))
                mid = c.execute("SELECT id FROM ef_messages WHERE task_id=? AND role='assistant'", (tid,)).fetchone()
                if mid: c.execute("UPDATE ef_sessions SET selected_message_id=?,updated_at=? WHERE id=?", (mid[0], stamp, task["session_id"]))

    def cancel(self, tid):
        with s.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM ef_tasks WHERE id=?", (tid,)).fetchone()
            if not row: raise HTTPException(404, "查询不存在")
            if row[0] == "queued":
                c.execute("UPDATE ef_tasks SET status='cancelled',stage='已取消',updated_at=? WHERE id=?", (s.now(), tid))
                c.execute("UPDATE ef_messages SET status='cancelled',text='查询已取消' WHERE task_id=? AND role='assistant'", (tid,))
            elif row[0] == "running":
                c.execute("UPDATE ef_tasks SET status='cancelling',stage='正在取消',updated_at=? WHERE id=?", (s.now(), tid))
                c.execute("UPDATE ef_messages SET status='cancelling' WHERE task_id=? AND role='assistant'", (tid,))
        with self.lock:
            if tid in self.stops: self.stops[tid].set()
        return self.get(tid)

    def retry(self, tid, request_id):
        with s.conn() as c: row = c.execute("SELECT * FROM ef_tasks WHERE id=?", (tid,)).fetchone()
        if not row: raise HTTPException(404, "查询不存在")
        if row["status"] not in ("failed", "cancelled"): raise HTTPException(409, "只能重试失败或取消的查询")
        req = json.loads(row["request_json"])
        req["request_id"] = request_id
        return self.submit(req, attempt_of=tid)


manager = TaskManager()


class RetryRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)


@router.post("/tasks/{tid}/retry")
def retry_task(tid: str, req: RetryRequest): return manager.retry(tid, req.request_id)
