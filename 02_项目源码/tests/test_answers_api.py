"""Answer API contract and lifecycle tests. All model outputs are explicit fakes."""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app

SECRET = "fake-secret-only-for-api-tests"
FINAL = {"completed", "needs_clarification", "insufficient_evidence", "failed", "cancelled"}


class FakeGatewayFactory:
    """A controllable fake gateway; never opens a network connection."""
    def __init__(self):
        self.calls = []
        self.instances = []
        self.lock = threading.Lock()
        self.block_on = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.plan_table = None
        self.semantic_support = True

    def __call__(self, config, cancelled):
        factory = self

        class Gateway:
            def __init__(self):
                self.models = {}
                self.usage = []

            def __call__(self, messages, purpose):
                payload = json.loads(messages[-1]["content"])
                with factory.lock:
                    factory.calls.append((purpose, payload))
                if factory.block_on == purpose:
                    factory.entered.set()
                    if not factory.release.wait(timeout=8):
                        raise AssertionError("Test did not release the blocked fake gateway")
                    # Deliberately return a late response after cancellation: the service
                    # must still refuse to publish it even if a transport ignores cancel.
                self.models[purpose] = config["models"][purpose] + "-reported"
                self.usage.append({"purpose": purpose, "total_tokens": 10})
                if 'allowed_fields' in payload:
                    return {'status':'unavailable'}  # Explicit optional context fixture.
                if purpose == "table":
                    table = payload["table"]["table"]
                    return {"status": "ready", "plan": {"table": factory.plan_table or table,
                            "select": ["item", "amount"], "limit": 10}}
                if purpose == "document":
                    chunk = payload["chunks"][0]
                    return {"status": "completed", "claims": [{"text": "地区生产总值为1312.9亿元。",
                            "citations": [{"chunk_id": chunk["chunk_id"], "quote": "地区生产总值1312.9亿元"}]}]}
                if "claims" in payload:
                    return {"claims": [{"id": claim["id"], "supported": factory.semantic_support,
                                        "reason": "Explicit test verifier"} for claim in payload["claims"]],
                            "answer_complete": factory.semantic_support,
                            "coverage": [{"question_id": part['id'], "status": "answered" if factory.semantic_support else "missing_evidence",
                                          "claim_ids": [c['id'] for c in payload['claims']] if factory.semantic_support else []}
                                         for part in payload.get('question_parts', [])]}
                return {"supported": factory.semantic_support, "reason": "Explicit test verifier",
                        "table_relations": [{"id": identifier, "supported": factory.semantic_support}
                                            for identifier in payload.get('table_relation_claim_ids', [])]}

        gateway = Gateway()
        factory.instances.append(gateway)
        return gateway


def configure(root):
    root.mkdir(parents=True, exist_ok=True)
    config = {"provider": "Fake gateway for tests", "endpoint": "http://127.0.0.1:1/v1",
              "api_key": SECRET, "models": {"table": "table-model", "document": "doc-model", "verify": "verify-model"},
              "available_models": ["table-model", "doc-model", "verify-model", "alternate-model"]}
    (root / "model.json").write_text(json.dumps(config), encoding="utf-8")
    return config


@pytest.fixture
def app_env(tmp_path):
    root = tmp_path / "runtime"
    configure(root)
    gateway = FakeGatewayFactory()
    app = create_app(data_dir=root, samples_dir=tmp_path / "samples", enable_workers=False,
                     answer_gateway_factory=gateway)
    with TestClient(app) as client:
        try:
            yield client, gateway, root
        finally:
            gateway.release.set()


def project(client, name="API fixture"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, pid, name="facts.csv", content=b"item,amount\nA,0.10\nB,0.20\n"):
    response = client.post(f"/api/projects/{pid}/import", files=[("files", (name, content))])
    assert response.status_code == 200, response.text
    for job in response.json()["jobs"]:
        client.app.state.service.process_job(job["id"])
    sid = response.json()["sources"][0]["id"]
    detail = client.get(f"/api/sources/{sid}").json()
    assert detail["source"]["usable"], detail
    return detail


def request_body(detail, **updates):
    body = {"question": "列出项目和金额", "source_id": detail["source"]["id"], "mode": "table",
            "table_name": detail["tables"][0]["name"], "request_id": str(uuid.uuid4())}
    body.update(updates)
    return body


def submit(client, pid, body):
    response = client.post(f"/api/projects/{pid}/answers", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def wait_terminal(client, pid, answer_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/projects/{pid}/answers/{answer_id}")
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] in FINAL:
            return record
        time.sleep(.01)
    pytest.fail(f"Answer {answer_id} never reached a terminal state: {record}")


def source_update(client, sid, **values):
    assert set(values) <= {"parser_version", "sha256", "data_path", "status"}
    with client.app.state.service.store.transaction() as connection:
        connection.execute("UPDATE sources SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                           [*values.values(), sid])


def test_table_answer_has_real_result_versioned_evidence_and_reported_models(app_env):
    client, gateway, _ = app_env
    pid = project(client); source = upload(client, pid)
    original = client.get(f"/api/sources/{source['source']['id']}/original").content
    record = wait_terminal(client, pid, submit(client, pid, request_body(source))["id"])
    assert record["status"] == "completed", record
    assert record["result"]["rows"] == [["A", "0.10"], ["B", "0.20"]]
    assert record["result"]["columns"] == ["item", "amount"]
    assert [purpose for purpose, _ in gateway.calls] == ["table", "verify", "document"]
    assert record["models"] == {"table": "table-model-reported", "verify": "verify-model-reported", "document":"doc-model-reported"}
    assert record['result']['context_interpretation']['status']=='unavailable'
    evidence = record["result"]["evidence"][0]
    assert evidence["row_numbers"] == [1, 2]
    assert evidence["rows"] == record["result"]["rows"]
    assert evidence["sha256"] == source["source"]["sha256"] and evidence["stale"] is False
    assert evidence["parser_version"] == source["source"]["parser_version"]
    assert record["result"]["verification"]["status"] == "passed"
    assert "SELECT" in record["result"]["query"]["sql"]
    assert record['result']['trace']==record['result']['query']['trace']
    assert isinstance(record['result']['query']['parameters'],(list,dict))
    assert client.get(f"/api/sources/{source['source']['id']}/original").content == original
    assert client.get(f"/api/projects/{pid}/answers").json()[0]["id"] == record["id"]
    assert client.get(f"/api/projects/{pid}/drafts").json() == [], "Answer execution must not manufacture a draft"


def test_document_answer_preserves_exact_quote_location_and_single_source(app_env):
    client, gateway, _ = app_env
    pid = project(client)
    source = upload(client, pid, "facts.txt", "2024年地区生产总值1312.9亿元，比上年增长6.5%。".encode())
    body = {"question": "地区生产总值是多少？", "source_id": source["source"]["id"],
            "mode": "document", "request_id": str(uuid.uuid4())}
    record = wait_terminal(client, pid, submit(client, pid, body)["id"])
    assert record["status"] == "completed", record
    assert [purpose for purpose, _ in gateway.calls] == ["document", "verify", "document"]
    evidence = record["result"]["evidence"][0]
    page = next(page for page in source["pages"] if page["number"] == evidence["page"])
    assert page["text"][evidence["start"]:evidence["end"]] == evidence["quote"] == "地区生产总值1312.9亿元"
    assert evidence["source_id"] == source["source"]["id"] and evidence["kind"] == "text"
    assert "逻辑" in evidence["page_label"]
    assert 'query' not in record['result']
    assert record['result']['trace'][-1]['stage']=='回答完成'
    assert client.app.state.answers.store.get(pid,record['id'])['result']['trace']==record['result']['trace']


def test_same_request_is_idempotent_during_and_after_execution(app_env):
    client, gateway, _ = app_env
    gateway.block_on = "table"
    pid = project(client); source = upload(client, pid); body = request_body(source)
    first = submit(client, pid, body)
    assert gateway.entered.wait(2)
    repeated = submit(client, pid, body)
    assert repeated["id"] == first["id"]
    assert len(gateway.calls) == 1 and len(gateway.instances) == 1
    gateway.release.set()
    done = wait_terminal(client, pid, first["id"])
    again = submit(client, pid, body)
    assert done["status"] == "completed" and again["id"] == done["id"]
    assert len(gateway.calls) == 3 and len(gateway.instances) == 1
    assert len(client.get(f"/api/projects/{pid}/answers").json()) == 1


def test_reused_request_id_with_changed_payload_is_conflict(app_env):
    client, gateway, _ = app_env
    pid = project(client); source = upload(client, pid); body = request_body(source)
    first = wait_terminal(client, pid, submit(client, pid, body)["id"])
    response = client.post(f"/api/projects/{pid}/answers", json={**body, "question": "另一个问题"})
    assert response.status_code == 409, response.text
    assert len(gateway.calls) == 3
    assert client.get(f"/api/projects/{pid}/answers/{first['id']}").json()["question"] == body["question"]


def test_project_scope_applies_to_create_get_list_and_cancel(app_env):
    client, gateway, _ = app_env
    gateway.block_on = "table"
    first, other = project(client), project(client, "Other project")
    source = upload(client, first); body = request_body(source)
    response = client.post(f"/api/projects/{other}/answers", json=body)
    assert response.status_code == 404 and not gateway.calls
    record = submit(client, first, body)
    assert gateway.entered.wait(2)
    assert client.get(f"/api/projects/{other}/answers/{record['id']}").status_code == 404
    assert client.post(f"/api/projects/{other}/answers/{record['id']}/cancel").status_code == 404
    assert client.get(f"/api/projects/{other}/answers").json() == []
    gateway.release.set()
    assert wait_terminal(client, first, record["id"])["status"] == "completed", "Wrong-project cancel must not signal the real job"


def test_explicit_table_is_required_and_model_cannot_substitute_another(app_env, tmp_path):
    client, gateway, _ = app_env
    database = tmp_path / "two.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE selected (item TEXT, amount TEXT)")
        connection.execute("INSERT INTO selected VALUES('allowed','1.00')")
        connection.execute("CREATE TABLE unrelated (item TEXT, amount TEXT)")
        connection.execute("INSERT INTO unrelated VALUES('private-other-table','999.00')")
    pid = project(client); source = upload(client, pid, "two.sqlite", database.read_bytes())
    body = request_body(source, table_name="selected")
    for table in (None, "missing", "selected; DROP TABLE selected"):
        invalid = client.post(f"/api/projects/{pid}/answers", json={**body, "table_name": table})
        assert invalid.status_code in (409, 422), invalid.text
    assert not gateway.calls
    gateway.plan_table = "unrelated"
    result = wait_terminal(client, pid, submit(client, pid, body)["id"])
    assert result["status"] in ("failed", "needs_clarification"), result
    assert "private-other-table" not in json.dumps(result["result"])
    assert not result["result"] or not result["result"].get("rows")
    assert [purpose for purpose, _ in gateway.calls] == ["table"]


def test_cancel_discards_late_gateway_response_and_preserves_terminal_state(app_env):
    client, gateway, _ = app_env
    gateway.block_on = "verify"
    pid = project(client); source = upload(client, pid)
    record = submit(client, pid, request_body(source))
    assert gateway.entered.wait(2)
    response = client.post(f"/api/projects/{pid}/answers/{record['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    gateway.release.set()
    # A subsequent queue barrier observes that the late response has finished.
    client.app.state.answers.pool.submit(lambda: None).result(timeout=2)
    client.app.state.answers.close()
    record = client.get(f"/api/projects/{pid}/answers/{record['id']}").json()
    assert record["status"] == "cancelled" and record["result"] is None
    assert client.get(f"/api/projects/{pid}/answers").json()[0]["result"] is None


@pytest.mark.parametrize("change", ["archive", "parser_version", "sha256", "data_path", "original_bytes"])
def test_source_change_during_answer_prevents_publication(app_env, change):
    client, gateway, root = app_env
    gateway.block_on = "verify"
    pid = project(client); source = upload(client, pid); sid = source["source"]["id"]
    record = submit(client, pid, request_body(source))
    assert gateway.entered.wait(2)
    if change == "archive":
        assert client.post(f"/api/sources/{sid}/archive").status_code == 200
    elif change == "original_bytes":
        original = client.app.state.service.store.get("sources", sid)["original_path"]
        # Deliberately defeat the read-only fixture permission to exercise the
        # independent digest guard against external disk replacement.
        Path(original).chmod(0o600)
        Path(original).write_bytes(b"unexpected replacement")
    else:
        value = {"parser_version": "new-parser-version", "sha256": "b" * 64, "data_path": str(root / "other.sqlite")}[change]
        source_update(client, sid, **{change: value})
    gateway.release.set()
    done = wait_terminal(client, pid, record["id"])
    assert done["status"] == "failed" and done["result"] is None, done
    assert "变化" in done["error"]


@pytest.mark.parametrize("change", ["archive", "parser_version", "sha256"])
def test_finished_answer_is_immutable_and_marks_stale_evidence(app_env, change):
    client, _, _ = app_env
    pid = project(client); source = upload(client, pid); sid = source["source"]["id"]
    before = wait_terminal(client, pid, submit(client, pid, request_body(source))["id"])
    assert before["status"] == "completed"
    if change == "archive":
        client.post(f"/api/sources/{sid}/archive")
    else:
        source_update(client, sid, **{change: "new-parser" if change == "parser_version" else "c" * 64})
    after = client.get(f"/api/projects/{pid}/answers/{before['id']}").json()
    assert after["status"] == "completed"
    assert after["result"]["rows"] == before["result"]["rows"]
    assert after["source_sha256"] == before["source_sha256"] and after["parser_version"] == before["parser_version"]
    assert all(evidence["stale"] for evidence in after["result"]["evidence"])
    assert after["result"]["evidence"][0]["sha256"] == before["result"]["evidence"][0]["sha256"]


def test_restart_fails_inflight_records_without_replaying_models(tmp_path):
    root = tmp_path / "runtime"; configure(root)
    gateway = FakeGatewayFactory()
    app = create_app(data_dir=root, samples_dir=tmp_path / "samples", enable_workers=False, answer_gateway_factory=gateway)
    with TestClient(app) as client:
        pid = project(client); source = upload(client, pid)
        completed = wait_terminal(client, pid, submit(client, pid, request_body(source))["id"])
        assert completed["status"] == "completed"
    crashed = copy.deepcopy(completed)
    crashed.update(id=uuid.uuid4().hex, request_id=str(uuid.uuid4()), status="running", stage="正在调用模型", result=None)
    with sqlite3.connect(root / "workspace.sqlite") as connection:
        connection.execute("INSERT INTO answers(id,project_id,request_id,body) VALUES(?,?,?,?)",
                           (crashed["id"], pid, crashed["request_id"], json.dumps(crashed)))
    calls_before = len(gateway.calls)
    restarted = create_app(data_dir=root, samples_dir=tmp_path / "samples", enable_workers=False, answer_gateway_factory=gateway)
    with TestClient(restarted) as client:
        recovered = client.get(f"/api/projects/{pid}/answers/{crashed['id']}").json()
        assert recovered["status"] == "failed" and recovered["result"] is None
        assert "中断" in recovered["error"] and "未自动" in recovered["error"]
        assert len(gateway.calls) == calls_before
        preserved = client.get(f"/api/projects/{pid}/answers/{completed['id']}").json()
        assert preserved["result"] == completed["result"]


def test_public_model_configuration_hides_secret_and_validates_candidates(app_env):
    client, gateway, root = app_env
    response = client.get("/api/model")
    assert response.status_code == 200 and response.json()["configured"] is True
    assert SECRET not in response.text and "api_key" not in response.json()
    assert response.json()["available_models"] == ["table-model", "doc-model", "verify-model", "alternate-model"]
    original = json.loads((root / "model.json").read_text())
    invalid = client.patch("/api/model", json={"models": {"table": "unregistered", "document": "doc-model", "verify": "verify-model"}})
    assert invalid.status_code == 422 and SECRET not in invalid.text
    assert json.loads((root / "model.json").read_text()) == original
    valid = client.patch("/api/model", json={"models": {"table": "alternate-model", "document": "doc-model", "verify": "verify-model"}})
    assert valid.status_code == 200 and valid.json()["models"]["table"] == "alternate-model"
    assert SECRET not in valid.text
    assert json.loads((root / "model.json").read_text())["api_key"] == SECRET
    pid = project(client); source = upload(client, pid)
    answer = wait_terminal(client, pid, submit(client, pid, request_body(source))["id"])
    assert answer["models"]["table"] == "alternate-model-reported"
    assert SECRET not in json.dumps(answer)
    assert [purpose for purpose, _ in gateway.calls] == ["table", "verify", "document"]


def test_invalid_inputs_never_reach_gateway(app_env):
    client, gateway, _ = app_env
    pid = project(client); source = upload(client, pid); body = request_body(source)
    for changes in ({"question": "  "}, {"request_id": "not-a-uuid"}, {"mode": "all"}, {"source_ids": []}, {"sql": "DROP TABLE x"}):
        response = client.post(f"/api/projects/{pid}/answers", json={**body, **changes})
        assert response.status_code == 422, response.text
    assert not gateway.calls


@pytest.mark.parametrize("mode,expected_status", [("table", "failed"), ("document", "insufficient_evidence")])
def test_semantic_verification_failure_is_not_a_completed_answer(app_env, mode, expected_status):
    client, gateway, _ = app_env
    gateway.semantic_support = False
    pid = project(client)
    if mode == "document":
        source = upload(client, pid, "facts.txt", "地区生产总值1312.9亿元。".encode())
        body = {"question": "地区生产总值是多少", "source_id": source["source"]["id"], "mode": "document", "request_id": str(uuid.uuid4())}
    else:
        source = upload(client, pid)
        body = request_body(source)
    result = wait_terminal(client, pid, submit(client, pid, body)["id"])
    assert result["status"] == expected_status, result
    assert not result["result"]["claims"]
    assert not result["result"].get("rows")
    assert result["result"]["verification"]["status"] == "limited"
    assert 'clarification' not in result['result']
    if mode == 'table':
        # Rejection withholds the conclusion, not the already executed audit.
        audit = result['result']['query']
        assert 'SELECT' in audit['sql'] and isinstance(audit['parameters'], (list, dict))
        assert audit['executed_result'] == {'columns': ['item', 'amount'],
                                           'rows': [['A', '0.10'], ['B', '0.20']]}
        evidence = result['result']['evidence']
        assert len(evidence) == 1 and evidence[0]['source_id'] == source['source']['id']
        assert evidence[0]['rows'] == audit['executed_result']['rows']
        assert evidence[0]['row_numbers'] == [1, 2]
        assert [purpose for purpose, _ in gateway.calls] == ['table', 'verify']
    else:
        assert not result['result']['evidence'] and 'query' not in result['result']


def test_cancel_cannot_overwrite_a_completed_answer(app_env):
    client, _, _ = app_env
    pid = project(client); source = upload(client, pid)
    completed = wait_terminal(client, pid, submit(client, pid, request_body(source))["id"])
    cancelled = client.post(f"/api/projects/{pid}/answers/{completed['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "completed"
    assert cancelled.json()["result"] == completed["result"]


@pytest.mark.parametrize('legacy_query', [
    {'trace':[{'stage':'回答完成','elapsed_ms':5}]},
    {'inspections':[{'sql':'SELECT 1','error':'旧探查失败'}], 'repairs':[]},
    {'sql':'SELECT item FROM facts'},
    {'sql':'SELECT item FROM facts','parameters':None},
])
def test_old_query_shapes_are_safe_in_public_api_without_rewriting_saved_record(app_env,legacy_query):
    client,gateway,_=app_env
    pid=project(client);source=upload(client,pid)
    completed=wait_terminal(client,pid,submit(client,pid,request_body(source))['id'])
    saved=client.app.state.answers.store.get(pid,completed['id'])
    saved['result'].pop('trace',None)
    saved['result']['query']=copy.deepcopy(legacy_query)
    with client.app.state.service.store.transaction() as connection:
        connection.execute('UPDATE answers SET body=? WHERE id=?',(json.dumps(saved),saved['id']))
    calls_before=len(gateway.calls)
    public=client.get(f'/api/projects/{pid}/answers/{saved["id"]}').json()['result']
    if legacy_query.get('sql'):
        assert public['query']['parameters']==[]
        assert public['query']['parameters_available'] is False
        assert any('未保存有效的绑定参数' in note for note in public['notes'])
    else:
        assert 'query' not in public
        if 'trace' in legacy_query:assert public['trace']==legacy_query['trace']
        else:assert public['query_diagnostics']==legacy_query
    assert client.app.state.answers.store.get(pid,saved['id'])==saved
    assert len(gateway.calls)==calls_before


def test_public_projection_does_not_mutate_input_record_or_shared_evidence(app_env):
    client,_,_=app_env
    pid=project(client);source=upload(client,pid)
    completed=wait_terminal(client,pid,submit(client,pid,request_body(source))['id'])
    original=client.app.state.answers.store.get(pid,completed['id'])
    original.pop('scope_stale',None)
    original['result'].pop('trace',None)
    original['result']['query']={'trace':[{'stage':'历史阶段','elapsed_ms':5}]}
    original['result']['evidence'][0].pop('stale',None)
    before=copy.deepcopy(original)
    public=client.app.state.answers.public(original)
    assert original==before
    assert public['scope_stale'] is False and public['result']['evidence'][0]['stale'] is False
    assert public['result']['trace']==before['result']['query']['trace']
    assert 'query' not in public['result']
    public['result']['evidence'][0]['rows'][0][0]='changed public view'
    assert original==before


def test_result_normalization_returns_independent_nested_collections():
    from backend.answer_service import normalize_result_contract
    original={'evidence':[{'rows':[['original']]}],
              'query':{'sql':'SELECT ?','parameters':['original'],'trace':[{'stage':'done','elapsed_ms':1}]}}
    before=copy.deepcopy(original)
    normalized=normalize_result_contract(original)
    normalized['query']['parameters'][0]='changed'
    normalized['evidence'][0]['rows'][0][0]='changed'
    normalized['trace'][0]['stage']='changed'
    assert original==before
