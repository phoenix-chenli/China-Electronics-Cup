"""Read-only exports of saved answer snapshots; never execute source SQL or models."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .answer_store import FINAL
from .source_snapshot import snapshot
from .store import unpack


STATUS = {"completed": "已完成", "needs_clarification": "需要澄清", "insufficient_evidence": "依据不足",
          "failed": "处理失败", "cancelled": "已取消"}
CSV_POLICY = ("仅导出已保存的实际返回行，不重跑查询。空值使用\\N，文本中的反斜杠加倍；"
              "公式前缀、前导单引号、数字字符串、浮点数及超过15位的整数前加单引号保护。"
              "读取时先去掉该保护单引号，再还原文本反斜杠；数据类型及精确原值以JSON记录为准。")
BOUNDARY = ("这是已保存回答的记录快照，不是原件打包，也不是重新执行后的结果。"
            "失败、取消、依据不足和待澄清记录不构成成功结论；截断表只代表已返回行。"
            "导出时仅检查当前资料登记状态与版本，未重新计算原件文件摘要。")
_SECRET_KEYS = {"apikey", "authorization", "password", "secret", "accesstoken", "refreshtoken",
                "cookie", "setcookie", "headers", "requestheaders", "modelconfig", "config", "endpoint"}
_LOCAL_PATH = re.compile(r"(?<![\w:])(?:file://[^\s\"'<>]+|(?:/Users/|/home/|/private/|/tmp/|/var/|/Volumes/|/etc/|/opt/|/usr/|/root/|/Applications/|/Library/)[^\s\"'<>]+|[A-Za-z]:[\\/][^\s\"'<>]+)")
_CREDENTIAL = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[\"']?([^\s,;\"'}]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+")
_URL_AUTH = re.compile(r"(https?://)[^\s/@]+@", re.I)
_NUMERIC_STRING = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


class ExportError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


@dataclass(frozen=True)
class ExportFile:
    content: bytes
    media_type: str
    headers: dict[str, str]


def _sanitize(value, redactions, location="record"):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            child = f"{location}.{key}"
            if normalized == "headerpath" and isinstance(item, list):
                # A table header hierarchy is source evidence, not a filesystem
                # location. Its elements still pass ordinary recursive path
                # and credential redaction; scalar header_path stays private.
                result[key] = _sanitize(item, redactions, child)
            elif normalized in _SECRET_KEYS or normalized.endswith("path") or normalized.endswith("directory"):
                result[key] = "[已隐去本机路径或连接凭据]"
                redactions.append({"field": child, "reason": "本机路径或连接配置"})
            else:
                result[key] = _sanitize(item, redactions, child)
        return result
    if isinstance(value, list):
        return [_sanitize(item, redactions, f"{location}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, str):
        clean = _LOCAL_PATH.sub("[本机路径已隐去]", value)
        clean = _CREDENTIAL.sub(r"\1[凭据已隐去]", clean)
        clean = _BEARER.sub("Bearer [凭据已隐去]", clean)
        clean = _URL_AUTH.sub(r"\1[凭据已隐去]@", clean)
        if clean != value:
            redactions.append({"field": location, "reason": "文本中的路径或凭据模式"})
        return clean
    return value


def _freshness(connection, project_id, record):
    recorded = record.get("sources") or [{"id": record.get("source_id"), "sha256": record.get("source_sha256"),
                                         "parser_version": record.get("parser_version")}]
    states = []
    for old in recorded:
        row = connection.execute("SELECT * FROM sources WHERE id=? AND project_id=?", (old.get("id"), project_id)).fetchone()
        source = unpack(row)
        reasons = []
        if source is None:
            reasons.append("资料已移除或不属于当前项目")
        else:
            if source.get("archived"):
                reasons.append("资料已归档")
            if source.get("status") not in {"ready", "attention"}:
                reasons.append("资料当前不可用于回答")
            if old.get("sha256") != source.get("sha256"):
                reasons.append("原件摘要已变化")
            if old.get("parser_version") != source.get("parser_version"):
                reasons.append("解析版本已变化")
            if old.get("revision") and old["revision"] != snapshot(source)["revision"]:
                reasons.append("解析内容或元数据已变化")
            if old.get('semantic_revision'):
                try:
                    from .semantic_store import read_state
                    if read_state(connection,source)['semantic_revision']!=old['semantic_revision']:
                        reasons.append('业务定义版本已变化')
                except sqlite3.Error:
                    reasons.append('业务定义版本暂不可核对')
        states.append({"source_id": old.get("id"), "stale": bool(reasons), "reasons": reasons,
                       "content_revision_checked": bool(old.get("revision")) and source is not None,
                       "semantic_revision_checked": bool(old.get('semantic_revision')) and source is not None})
    return {"scope_stale": any(s["stale"] for s in states), "sources": states,
            "legacy_revision_notice": "旧记录未保存内容修订摘要的来源仅核对文件SHA与解析版本" if any(not s["content_revision_checked"] for s in states) else None}


def read_snapshot(db_path: Path, project_id: str, answer_id: str, scope="answer") -> dict:
    """A single SQLite read transaction; no startup recovery or write-capable Store calls."""
    if scope not in {"answer", "conversation"}:
        raise ExportError(422, "导出范围只能是 answer 或 conversation")
    connection = None
    try:
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise ExportError(409, "保存记录暂时不可读取，请稍后再试") from exc
    try:
        project = connection.execute("SELECT id,name FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ExportError(404, "项目不存在")
        chain, seen, metadata, events = [], set(), {}, {}
        has_events = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_events'").fetchone())
        current = answer_id
        while current:
            if current in seen or len(chain) >= 1000:
                raise ExportError(409, "会话超过1000轮或历史存在循环，无法导出完整会话")
            seen.add(current)
            row = connection.execute("SELECT body FROM answers WHERE id=? AND project_id=?", (current, project_id)).fetchone()
            if not row:
                raise ExportError(404 if not chain else 409, "回答不存在或不属于当前项目" if not chain else "会话上文缺失，无法导出完整会话")
            record = json.loads(row["body"])
            if record.get("status") not in FINAL:
                raise ExportError(409, "回答尚未结束，请处理完成或取消后再导出")
            chain.append(record)
            metadata[current] = {"stored_body_sha256": hashlib.sha256(row["body"].encode("utf-8")).hexdigest(),
                                 "freshness": _freshness(connection, project_id, record)}
            if has_events:
                events[current] = [json.loads(row[0]) for row in connection.execute(
                    'SELECT body FROM answer_events WHERE answer_id=? AND project_id=? ORDER BY sequence', (current,project_id))]
            current = record.get("parent_answer_id") if scope == "conversation" else None
        redactions = []
        payload = {"schema_version": "zhiheng.answer-export.v1", "exported_at": datetime.now(timezone.utc).isoformat(),
                   "scope": scope, "selected_answer_id": answer_id, "project": dict(project),
                   "boundary": BOUNDARY, "snapshot_hash_definition": "stored_body_sha256 是数据库 body 原始UTF-8字节的SHA-256，脱敏后内容与该摘要不同",
                   "csv_policy": CSV_POLICY, "answers": list(reversed(chain)), "record_metadata": metadata,
                   "execution_events": events}
        payload = _sanitize(payload, redactions)
        payload["redactions"] = redactions
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ExportError(409, "保存记录包含无法可靠表示的数值或结构，未生成导出文件") from exc
        return payload
    except (sqlite3.Error, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ExportError(409, "保存记录不完整或暂时不可读取，未生成导出文件") from exc
    finally:
        connection.close()


def _md(value) -> str:
    # Source/user text must remain text in Markdown viewers: no HTML, links, or images.
    escaped = html.escape(str(value), quote=True)
    escaped = re.sub(r"([\\`*_{}\[\]()#+\-.!|>~=])", r"\\\1", escaped)
    return re.sub(r"(?i)\b(https?|ftp|mailto):", r"\1&#58;", escaped)


def _fence(value, language="json") -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}\n"


def _table(columns, rows):
    if len(columns) > 12:
        return _fence({"columns": columns, "rows": rows})
    cell = lambda x: _md("NULL" if x is None else x).replace("\r\n", " ↵ ").replace("\n", " ↵ ").replace("\r", " ↵ ")
    lines = ["| " + " | ".join(cell(c) for c in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def _result_boundary(answer, result):
    query = result.get("query") or {}
    count = len(result.get("rows", []))
    if answer.get("mode") == "table":
        limit = query.get("limit", (query.get("plan") or {}).get("limit"))
        before = query.get("result_rows_before_limit")
        return (f"查询上限：{limit if limit is not None else '未记录'}；已返回：{count} 行；"
                f"限制前结果：{str(before) + ' 行' if before is not None else '未记录'}。"
                "此上限可能对应用户要求的 TopN，不据此判定答复不完整。")
    return f"保存行数：{count}；截断：{'是，只包含实际返回部分' if query.get('truncated') else '否（以原记录为准）'}。"


def render_markdown(payload):
    out = ["# 知衡 · 回答记录\n", "导出时间：" + _md(payload["exported_at"]),
           "范围：" + ("当前回答及其祖先会话" if payload["scope"] == "conversation" else "当前回答"),
           "项目：" + _md(payload["project"]["name"]), _md(BOUNDARY)]
    for answer in payload["answers"]:
        state = payload["record_metadata"][answer["id"]]
        fresh = state["freshness"]
        status = answer.get("status")
        out += [f"## 第 {_md(answer.get('turn_index', 1))} 轮 · {_md(STATUS.get(status, status))}",
                "问题：" + _md(answer.get("question", "")), "回答编号：" + _md(answer["id"]),
                "创建时间：" + _md(answer.get("created_at", "未记录")) + "；结束/更新时间：" + _md(answer.get("updated_at", "未记录")),
                "记录耗时（毫秒）：" + _md(answer.get("elapsed_ms", "未记录")),
                "当前版本核对：" + ("**资料已过期或不可用。以下仅为历史记录，不能当作当前结果。**" if fresh["scope_stale"] else "登记版本与记录匹配；未重新运行问答。")]
        if fresh.get("legacy_revision_notice"):
            out.append(_md(fresh["legacy_revision_notice"]))
        if status != "completed":
            out.append("**本轮未形成已完成的答案；下列内容保留其澄清、依据不足、失败或取消状态。**")
        if answer.get("error"):
            out.append("处理信息：" + _md(answer["error"]))
        result = answer.get("result") or {}
        out += ["### 已保存答复", _md(result.get("text") or "本轮没有保存答复正文。")]
        for claim in result.get("claims", []):
            out.append("- " + _md(claim.get("id", "")) + "：" + _md(claim.get("text", "")) + "；引用：" + _md("、".join(claim.get("evidence_ids", []))))
            if claim.get("table_bindings"):
                out += ["", "表格关系绑定（按原格或已执行查询范围核对）：", _fence(claim["table_bindings"])]
            if claim.get("chart_bindings"):
                out += ["", "图表依据（原页标签、位置及独立视觉关系复核）：", _fence(claim["chart_bindings"])]
        if isinstance(result.get("columns"), list) and result["columns"] and isinstance(result.get("rows"), list):
            out += ["### 已返回结果表", _result_boundary(answer, result) + "空表不等于数值零。",
                    _table(result["columns"], result["rows"])]
        query = result.get("query") if isinstance(result.get("query"), dict) else {}
        trace = result.get("trace") if isinstance(result.get("trace"), list) else query.get("trace")
        if trace:
            out += ["### 执行摘要（已记录阶段与累计耗时）", _fence(trace)]
        if payload.get('execution_events',{}).get(answer['id']):
            out += ["### 持久执行事件（实际状态变更）", _fence(payload['execution_events'][answer['id']])]
        query_record = {key: value for key, value in query.items() if key != "trace"}
        diagnostics = dict(result.get("query_diagnostics") or {})
        if isinstance(query.get("sql"), str) and query["sql"].strip():
            out += ["### 查询与参数（只保留当时执行记录）", _fence(query_record)]
            if not isinstance(query.get("parameters"), (list, dict)) or query.get("parameters_available") is False:
                out.append("原记录未保存有效绑定参数，不能据此复现原查询。")
        elif query_record:
            diagnostics.update(query_record)
        if diagnostics:
            out += ["### 查询诊断记录（不代表已完成查询）", _fence(diagnostics)]
        if result.get("completeness") or result.get("completion_repair"):
            out += ["### 问题覆盖与有限补检", _fence({
                "completeness": result.get("completeness"),
                "completion_repair": result.get("completion_repair"),
            })]
        if result.get("coverage_evidence"):
            out += ["### 覆盖检查引用的原文片段", _fence(result["coverage_evidence"])]
        if result.get("coverage_claims"):
            out += ["### 覆盖检查的事实快照（含未采纳尝试，以发布标记为准）", _fence(result["coverage_claims"])]
        if result.get("calculations"):
            out += ["### 公式、单位与参数绑定", _fence(result["calculations"])]
        if result.get("steps"):
            out += ["### 执行步骤与跨源绑定", _fence(result["steps"])]
        out.append("### 来源快照与引用")
        sources = answer.get("sources") or [{"id": answer.get("source_id"), "name": answer.get("source_name"), "sha256": answer.get("source_sha256"), "parser_version": answer.get("parser_version")}]
        out.append(_fence({"recorded_sources": sources, "current_version_check": fresh}))
        if result.get("evidence"):
            # JSON inside a content-sized fence preserves exact quote text, page/span, SHA, parser and SQL cells.
            out.append(_fence(result["evidence"]))
        else:
            out.append("本轮未保存可引用证据。")
        out += ["### 质量提示与复核", _fence({"source_warnings": answer.get("warnings", []), "notes": result.get("notes", []),
                                                "verification": result.get("verification"), "diagnostics": result.get("diagnostics")}),
                "### 模型与调用记录", "标识来自保存的路由配置或接口回报，不证明底层模型权重；同模型两次调用不等于两个独立模型。",
                _fence({"models": answer.get("models"), "usage": answer.get("usage"), "model_attempts": answer.get("model_attempts")}),
                "保存记录摘要（脱敏前 body）：" + _md(state["stored_body_sha256"])]
    out += ["## 导出边界与脱敏", _md(payload["snapshot_hash_definition"]), _md(CSV_POLICY),
            "本文件转义问题和资料中的 HTML/Markdown，不将其中链接变成可点击外链。完整记录字段请使用 JSON 导出。",
            _fence(payload["redactions"])]
    return ("\n\n".join(out) + "\n").encode("utf-8")


def _csv_cell(value):
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return ("'" if len(str(abs(value))) > 15 else "") + str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExportError(409, "结果含非有限数值，CSV无法可靠表示，请导出JSON审阅")
        return "'" + repr(value)
    if not isinstance(value, str):
        raise ExportError(409, "结果表含非标量单元格，请使用JSON导出")
    escaped = value.replace("\\", "\\\\")
    if value.startswith("'") or _NUMERIC_STRING.fullmatch(value) or re.match(r"^[\s\x00-\x1f\ufeff]*[=+@\-]", value) or value.startswith(("\t", "\r", "\n")):
        return "'" + escaped
    return escaped


def export_answer(db_path, project_id, answer_id, format="markdown", scope="answer") -> ExportFile:
    format = "markdown" if format == "md" else format
    if format not in {"markdown", "json", "csv"}:
        raise ExportError(422, "导出格式只能是 markdown、json 或 csv")
    if format == "csv" and scope != "answer":
        raise ExportError(409, "CSV仅支持本轮结果表；连续会话请使用Markdown或JSON")
    payload = read_snapshot(Path(db_path), project_id, answer_id, scope)
    stale = any(m["freshness"]["scope_stale"] for m in payload["record_metadata"].values())
    short_id = hashlib.sha256(str(answer_id).encode()).hexdigest()[:8]
    stem = "知衡_" + ("会话记录" if scope == "conversation" else "回答记录") + ("_历史资料已过期" if stale else "") + "_" + short_id
    headers = {"Cache-Control": "no-store", "X-Export-Snapshot": "saved-record-v1", "X-Export-Scope-Stale": str(stale).lower(),
               "X-Export-Redaction-Count": str(len(payload["redactions"]))}
    if format == "markdown":
        content, media, extension = render_markdown(payload), "text/markdown; charset=utf-8", "md"
    elif format == "json":
        content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        media, extension = "application/json", "json"
    else:
        answer = payload["answers"][0]
        result = answer.get("result") or {}
        if answer.get("status") != "completed":
            raise ExportError(409, "本轮未完成，不生成成功结果CSV；请导出Markdown或JSON审阅记录")
        if stale:
            raise ExportError(409, "资料版本已变化，CSV无法携带完整历史边界；请导出Markdown或JSON")
        columns, rows = result.get("columns"), result.get("rows")
        if not isinstance(columns, list) or not columns or not all(isinstance(c, str) for c in columns) or not isinstance(rows, list):
            raise ExportError(409, "本轮没有独立结果表；跨源查询步骤和公式请使用Markdown或JSON")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ExportError(409, "已保存表格结构不完整，未生成CSV")
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow([_csv_cell(c) for c in columns])
        writer.writerows([_csv_cell(v) for v in row] for row in rows)
        content, media, extension = buffer.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "csv"
        limit_applied = bool((result.get("query") or {}).get("truncated")) and answer.get("mode") == "table"
        truncated = bool((result.get("query") or {}).get("truncated")) and answer.get("mode") != "table"
        stem = "知衡_安全文本结果_" + ("已截断_" if truncated else "按查询上限_" if limit_applied else "") + f"返回{len(rows)}行_" + short_id
        headers.update({"X-Export-Row-Count": str(len(rows)), "X-Export-Truncated": str(truncated).lower(),
                        "X-Export-Limit-Applied": str(limit_applied).lower(),
                        "X-Export-CSV-Encoding": "zhiheng-safe-csv-v1", "X-Export-Warnings": quote(CSV_POLICY + _result_boundary(answer, result) + f"本记录隐去路径或凭据{len(payload['redactions'])}处，明细见JSON。", safe="")})
    headers["Content-Disposition"] = f'attachment; filename="zhiheng-{short_id}.{extension}"; filename*=UTF-8\'\'{quote(stem + "." + extension, safe="")}'
    return ExportFile(content, media, headers)
