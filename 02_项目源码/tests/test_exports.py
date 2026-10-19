"""Snapshot export tests; all records/model interactions are local test fixtures."""
from __future__ import annotations

import copy
import csv
import io
import json
import sqlite3
from urllib.parse import unquote

import pytest

from backend.export_service import ExportError, export_answer, read_snapshot
from test_answers_api import app_env, project, upload, submit, request_body, wait_terminal


@pytest.fixture
def saved(app_env):
    client, gateway, root = app_env
    pid = project(client, "中文项目")
    source = upload(client, pid)
    answer = wait_terminal(client, pid, submit(client, pid, request_body(source))["id"])
    assert answer["status"] == "completed"
    return client, gateway, root / "workspace.sqlite", pid, answer


def save(db, answer):
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT OR REPLACE INTO answers(id,project_id,request_id,body) VALUES (?,?,?,?)",
                           (answer["id"], answer["project_id"], answer["request_id"], json.dumps(answer, ensure_ascii=False)))


def get_body(db, aid):
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT body FROM answers WHERE id=?", (aid,)).fetchone()[0]


def exported(saved, format="json", scope="answer", aid=None):
    _, _, db, pid, answer = saved
    return export_answer(db, pid, aid or answer["id"], format, scope)


def test_api_exports_saved_snapshot_without_query_model_or_record_mutation(saved):
    client, gateway, db, pid, answer = saved
    before, calls = get_body(db, answer["id"]), len(gateway.calls)
    for format in ["markdown", "md", "json", "csv"]:
        response = client.get(f"/api/projects/{pid}/answers/{answer['id']}/export", params={"format": format})
        assert response.status_code == 200, response.text
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert "知衡" in unquote(response.headers["content-disposition"])
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert get_body(db, answer["id"]) == before
    assert len(gateway.calls) == calls
    data = json.loads(exported(saved).content)
    assert data["answers"][0]["result"]["rows"] == [["A", "0.10"], ["B", "0.20"]]
    assert data["answers"][0]["sources"] == answer["sources"]
    assert "不是原件打包" in data["boundary"]
    assert data["record_metadata"][answer["id"]]["freshness"]["scope_stale"] is False


@pytest.mark.parametrize('legacy', [False, True])
def test_non_sql_trace_and_diagnostics_survive_markdown_without_inventing_query(saved, legacy):
    client, gateway, db, pid, answer = saved
    trace = [{'stage': '显式测试阶段', 'elapsed_ms': 17}]
    diagnostics = {'inspections': [{'error': '测试探查未完成'}], 'repairs': ['测试修复记录']}
    answer['mode'] = 'document'
    answer['result'] = {'text': '受控历史记录', 'claims': [], 'evidence': [], 'notes': [],
                        'verification': {'status': 'limited', 'checks': []}}
    if legacy:
        answer['result']['query'] = {'trace': trace, **diagnostics}
    else:
        answer['result'].update(trace=trace, query_diagnostics=diagnostics)
    save(db, answer)
    before, calls = get_body(db, answer['id']), len(gateway.calls)
    public = client.get(f"/api/projects/{pid}/answers/{answer['id']}").json()
    assert 'query' not in public['result']
    text = exported(saved, 'markdown').content.decode()
    assert text.count('显式测试阶段') == 1
    assert '测试探查未完成' in text and '测试修复记录' in text
    assert '### 查询与参数' not in text
    assert '### 查询诊断记录' in text
    exported_json = json.loads(exported(saved).content)['answers'][0]['result']
    assert ('query' in exported_json) is legacy  # Raw saved shape remains a snapshot.
    assert get_body(db, answer['id']) == before
    assert len(gateway.calls) == calls


def test_sql_trace_is_not_duplicated_and_missing_parameters_are_unknown(saved):
    _, _, db, _, answer = saved
    trace = [{'stage': '只出现一次的测试阶段', 'elapsed_ms': 18}]
    answer['result']['trace'] = trace
    answer['result']['query']['trace'] = trace
    answer['result']['query'].pop('parameters', None)
    save(db, answer)
    text = exported(saved, 'markdown').content.decode()
    assert text.count('只出现一次的测试阶段') == 1
    assert '原记录未保存有效绑定参数' in text
    assert '### 查询与参数' in text


def test_readonly_database_connection_and_missing_db_does_not_create(tmp_path, saved):
    _, _, db, pid, answer = saved
    # A concurrent RESERVED write lock still permits the export's read-only transaction.
    connection = sqlite3.connect(db)
    connection.execute("BEGIN IMMEDIATE")
    try:
        assert read_snapshot(db, pid, answer["id"])["answers"][0]["id"] == answer["id"]
    finally:
        connection.rollback(); connection.close()
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ExportError, match="不可读取"):
        read_snapshot(missing, pid, answer["id"])
    assert not missing.exists()


def test_completion_repair_and_coverage_evidence_survive_both_exports(saved):
    _, gateway, db, _, answer = saved
    answer['result'].update(
        completeness={'status': 'assessed', 'complete': False, 'coverage': [
            {'question_id': 'Q2', 'status': 'evidence_available', 'evidence_ids': ['p8:20-40']}]},
        completion_repair={'attempted': True, 'resolved': False, 'reason': 'old_claims_not_preserved'},
        coverage_claims=[{'id': 'C2', 'text': '二稿中未采纳的受控事实',
                          'publication_status': 'not_adopted', 'published_claim_ids': []}],
        coverage_evidence=[{'id': 'p8:20-40', 'source_id': 'test-source', 'page': 8,
                            'start': 20, 'end': 40, 'quote': '受控测试：遗漏部分对应原文'}],
    )
    save(db, answer)
    before, calls = get_body(db, answer['id']), len(gateway.calls)
    text = exported(saved, 'markdown').content.decode()
    assert '### 问题覆盖与有限补检' in text
    assert '### 覆盖检查引用的原文片段' in text
    assert 'old_claims_not_preserved' in text and '受控测试：遗漏部分对应原文' in text
    assert '含未采纳尝试，以发布标记为准' in text and 'not_adopted' in text
    data = json.loads(exported(saved).content)['answers'][0]['result']
    for field in ['completeness', 'completion_repair', 'coverage_evidence', 'coverage_claims']:
        assert data[field] == answer['result'][field]
    assert get_body(db, answer['id']) == before and len(gateway.calls) == calls


@pytest.mark.parametrize("status", ["needs_clarification", "insufficient_evidence", "failed", "cancelled"])
def test_non_success_records_keep_status_and_no_success_csv(saved, status):
    _, _, db, _, answer = saved
    answer.update(status=status, result=None, error="未发布结果")
    save(db, answer)
    data = json.loads(exported(saved).content)
    assert data["answers"][0]["status"] == status
    assert data["answers"][0]["result"] is None
    assert "本轮未形成已完成的答案" in exported(saved, "markdown").content.decode()
    with pytest.raises(ExportError) as error:
        exported(saved, "csv")
    assert error.value.status_code == 409


@pytest.mark.parametrize("status", ["queued", "running"])
def test_in_progress_is_409_for_all_formats(saved, status):
    client, _, db, pid, answer = saved
    answer["status"] = status; save(db, answer)
    for format in ["markdown", "json", "csv"]:
        response = client.get(f"/api/projects/{pid}/answers/{answer['id']}/export?format={format}")
        assert response.status_code == 409
        assert "尚未结束" in response.json()["detail"]


def test_conversation_one_branch_and_broken_history(saved):
    _, _, db, _, answer = saved
    parent = answer["id"]
    for aid, parent_id, turn in [("child", parent, 2), ("sibling", parent, 2), ("grandchild", "child", 3)]:
        record = copy.deepcopy(answer)
        record.update(id=aid, request_id=aid, parent_answer_id=parent_id, turn_index=turn)
        save(db, record)
    data = json.loads(exported(saved, scope="conversation", aid="grandchild").content)
    assert [a["id"] for a in data["answers"]] == [parent, "child", "grandchild"]
    with pytest.raises(ExportError, match="CSV仅支持本轮"):
        exported(saved, "csv", "conversation")
    record["parent_answer_id"] = "missing"; save(db, record)
    with pytest.raises(ExportError, match="上文缺失"):
        exported(saved, scope="conversation", aid="grandchild")
    record["parent_answer_id"] = "grandchild"; save(db, record)
    with pytest.raises(ExportError, match="循环"):
        exported(saved, scope="conversation", aid="grandchild")


def test_cross_project_and_unknown_format(saved):
    client, _, _, pid, answer = saved
    other = project(client, "其他项目")
    response = client.get(f"/api/projects/{other}/answers/{answer['id']}/export?format=json")
    assert response.status_code == 404
    assert client.get(f"/api/projects/{pid}/answers/{answer['id']}/export?format=pdf").status_code == 422
    assert client.get(f"/api/projects/{pid}/answers/{answer['id']}/export?scope=all").status_code == 422


def test_stale_metadata_detected_without_rewriting_historical_evidence(saved):
    _, _, db, _, answer = saved
    old = get_body(db, answer["id"])
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE sources SET metadata=? WHERE id=?", (json.dumps({"revision": "updated"}), answer["source_id"]))
    file = exported(saved)
    data = json.loads(file.content)
    assert data["record_metadata"][answer["id"]]["freshness"]["scope_stale"] is True
    assert data["answers"][0]["result"]["evidence"][0]["stale"] is False  # Historical field retained.
    assert get_body(db, answer["id"]) == old
    assert "历史资料已过期" in unquote(file.headers["Content-Disposition"])
    assert "仅为历史记录" in exported(saved, "markdown").content.decode()
    with pytest.raises(ExportError, match="资料版本已变化"):
        exported(saved, "csv")


def test_legacy_snapshot_no_revision_notice(saved):
    _, _, db, _, answer = saved
    answer.pop("sources", None); save(db, answer)
    data = json.loads(exported(saved).content)
    check = data["record_metadata"][answer["id"]]["freshness"]
    assert check["scope_stale"] is False
    assert check["legacy_revision_notice"]


def test_credentials_paths_redacted_with_explicit_manifest(saved):
    _, _, db, _, answer = saved
    answer["result"]["diagnostics"] = {"api_key": "TOP_SECRET", "original_path": "/Users/test/private/fact.csv",
        "error": "打开 /private/tmp/private-file 失败; api_key=INLINE_SECRET Bearer BEARER_SECRET https://alice:pass@example.org/v1",
        "model_config": {"secret": "NESTED_SECRET"}}
    answer["usage"] = [{"total_tokens": 123, "prompt_tokens": 100}]
    save(db, answer)
    for format in ["json", "markdown"]:
        text = exported(saved, format).content.decode()
        for secret in ["TOP_SECRET", "INLINE_SECRET", "BEARER_SECRET", "NESTED_SECRET", "/Users/test", "/private/tmp", "alice:pass"]:
            assert secret not in text
        assert "total_tokens" in text and "123" in text
    assert json.loads(exported(saved).content)["redactions"]


def test_saved_table_header_hierarchy_survives_json_and_markdown_while_nested_paths_remain_private(saved):
    _, gateway, db, pid, answer = saved
    hierarchy=[{'text':'2024年','row':1,'col':2,'evidence':{'page':2,'bbox':[.2,.1,.6,.2]}},
               {'text':'金额（万元）','row':2,'col':2,'evidence':{'page':2,'bbox':[.2,.2,.4,.3]}}]
    binding={'table_name':'统计表','cell_id':'R3C2','value':'10','header_path':copy.deepcopy(hierarchy)}
    answer['result']['claims']=[{'id':'C1','text':'2024年金额为10万元。','evidence_ids':['E1'],
                                'table_bindings':[binding]}]
    answer['result']['evidence'][0]['table_context']={'tables':[{'table_name':'统计表','columns':[
        {'column':2,'header_path':copy.deepcopy(hierarchy)}]}]}
    answer['result']['diagnostics']={
        'header_path':['/Users/test/private/header.txt',
                       {'text':'安全列标题','source_path':'/private/tmp/raw.csv','api_key':'ARRAY_SECRET'},
                       {'text':'文件 /tmp/hidden.tsv; password=INLINE_ARRAY_SECRET'}],
        'legacy':{'header_path':'/Users/test/private/scalar.txt'},
        'source_path':'/Users/test/private/source.pdf','filepath':'/private/tmp/file.csv',
        'authorization':'Bearer AUTH_SECRET'}
    save(db,answer)
    before,calls=get_body(db,answer['id']),len(gateway.calls)
    snapshot=read_snapshot(db,pid,answer['id'])
    result=snapshot['answers'][0]['result']
    assert result['claims'][0]['table_bindings'][0]['header_path']==hierarchy
    assert result['evidence'][0]['table_context']['tables'][0]['columns'][0]['header_path']==hierarchy
    assert isinstance(result['diagnostics']['header_path'],list)
    assert result['diagnostics']['header_path'][1]['text']=='安全列标题'
    assert result['diagnostics']['legacy']['header_path']=='[已隐去本机路径或连接凭据]'
    for format in ['json','markdown']:
        text=exported(saved,format).content.decode()
        for label in ['2024年','金额（万元）','"header_path": [','"row": 2','安全列标题']:
            assert label in text
        for private in ['/Users/test','/private/tmp','/tmp/hidden','ARRAY_SECRET','INLINE_ARRAY_SECRET','AUTH_SECRET']:
            assert private not in text
    fields={entry['field'] for entry in snapshot['redactions']}
    assert any(field.endswith('diagnostics.header_path[0]') for field in fields)
    assert any(field.endswith('diagnostics.header_path[1].source_path') for field in fields)
    assert not any(field.endswith('table_bindings[0].header_path') for field in fields)
    assert get_body(db,answer['id'])==before and len(gateway.calls)==calls


def test_markdown_untrusted_text_and_fences_are_safe(saved):
    _, _, db, _, answer = saved
    attack = '<img src=x onerror="alert(1)"> [click](https://evil.example) ![load](https://evil.example/image)'
    answer["question"] = attack
    answer["result"]["text"] = attack
    answer["result"]["evidence"][0]["quote"] = "```\n" + attack + "\n````"
    answer["result"]["query"]["sql"] = "SELECT '````' AS value"
    save(db, answer)
    text = exported(saved, "markdown").content.decode()
    assert "&lt;img" in text and "\\[click\\]\\(" in text and "https&#58;" in text
    assert "`````json\n" in text  # Longer than the untrusted 4-backtick run.
    assert json.loads(exported(saved).content)["answers"][0]["question"] == attack


def test_csv_preserves_strings_null_quotes_newlines_zero_large_integer_and_decimal(saved):
    _, _, db, _, answer = saved
    values = [None, "", "中文\n第二行,\"引号\"", 0, 9007199254740993123456789, "0.00000000000000000000100", "000123", "\\N", "'literal", "=SUM(1,2)", " \t@cmd", "-1.25", -1.25, "\rformula", 0.12345678901234566, "\x00=1+2"]
    answer["result"].update(columns=["字段" + str(i) for i in range(len(values))], rows=[values])
    save(db, answer)
    file = exported(saved, "csv")
    assert file.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(file.content.decode("utf-8-sig"), newline="")))
    assert rows[1] == ["\\N", "", "中文\n第二行,\"引号\"", "0", "'9007199254740993123456789", "'0.00000000000000000000100", "'000123", "\\\\N", "''literal", "'=SUM(1,2)", "' \t@cmd", "'-1.25", "'-1.25", "'\rformula", "'0.12345678901234566", "'\x00=1+2"]
    assert json.loads(exported(saved).content)["answers"][0]["result"]["rows"][0] == values
    assert file.headers["X-Export-Row-Count"] == "1"
    assert "反斜杠" in unquote(file.headers["X-Export-Warnings"])


def test_csv_real_empty_header_no_fake_zero_and_no_unavailable_table(saved):
    _, _, db, _, answer = saved
    answer["result"]["rows"] = []; save(db, answer)
    file = exported(saved, "csv")
    assert list(csv.reader(io.StringIO(file.content.decode("utf-8-sig")))) == [["item", "amount"]]
    assert file.headers["X-Export-Row-Count"] == "0"
    answer["result"].pop("columns"); answer["result"].pop("rows"); save(db, answer)
    with pytest.raises(ExportError, match="没有独立结果表"):
        exported(saved, "csv")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_historical_value_is_explicit_409_in_every_format(saved, value):
    client, _, db, pid, answer = saved
    answer["result"]["rows"][0][1] = value
    save(db, answer)
    for format in ["markdown", "json", "csv"]:
        response = client.get(f"/api/projects/{pid}/answers/{answer['id']}/export?format={format}")
        assert response.status_code == 409
        assert "无法可靠表示的数值或结构" in response.json()["detail"]
        assert "content-disposition" not in response.headers


def test_top_n_limit_is_not_safety_truncation_but_database_truncation_is(saved):
    _, _, db, _, answer = saved
    query = answer["result"]["query"]
    query.update(truncated=True, returned_rows=2, result_rows_before_limit=16, limit=2)
    save(db, answer)
    file = exported(saved, "csv")
    assert file.headers["X-Export-Truncated"] == "false"
    assert file.headers["X-Export-Limit-Applied"] == "true"
    text = exported(saved, "markdown").content.decode()
    assert "查询上限：2；已返回：2 行；限制前结果：16 行" in text
    answer["mode"] = "database"; save(db, answer)
    file = exported(saved, "csv")
    assert file.headers["X-Export-Truncated"] == "true"
    assert "已截断" in unquote(file.headers["Content-Disposition"])


def test_multisource_formula_bindings_units_and_steps_remain_snapshot(saved):
    _, _, db, _, answer = saved
    answer["mode"] = "multi_source"
    answer["result"] = {"text": "差额为 1.00 元。", "claims": [{"id": "C1", "text": "差额为 1.00 元。", "evidence_ids": ["E1"], "calculation_ids": ["K1"]}],
        "calculations": [{"id": "K1", "expression": "a-b", "value": "1", "unit": "元", "display_value": "1.00", "parameters": [{"name": "a", "value": "2", "unit": "元", "evidence_ids": ["E1"], "binding": {"row": 0, "column": "amount"}}]}],
        "steps": [{"id": "S1", "kind": "sql", "query": {"sql": "SELECT amount FROM facts WHERE id=?", "parameters": [1], "columns": ["amount"], "rows": [["2"]], "truncated": False}, "evidence_ids": ["E1"]}],
        "evidence": [{"id": "E1", "source_id": answer["source_id"], "sha256": answer["source_sha256"], "parser_version": answer["parser_version"], "page": 4, "start": 3, "end": 8, "quote": "amount=2"}], "verification": {"status": "passed"}}
    save(db, answer)
    data = json.loads(exported(saved).content)
    assert data["answers"][0]["result"] == answer["result"]
    text = exported(saved, "markdown").content.decode()
    for expected in ["a-b", "1.00", "元", '"row": 0', '"page": 4', '"parameters": [', "SELECT amount"]:
        assert expected in text
    with pytest.raises(ExportError, match="没有独立结果表"):
        exported(saved, "csv")
