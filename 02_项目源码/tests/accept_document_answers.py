#!/usr/bin/env python3
"""Run the 14 real-source document acceptance cases only with --run-live.

Run from the project root, for example:
  .venv/bin/python tests/accept_document_answers.py --runtime .runtime \
      --report work/document-acceptance-new.json --run-live

This reads an existing workspace without changing projects, sources, or answers.
The report contains model answers and exact citations, never API credentials.
Passing the automatic checks is not a substitute for reviewing answer meaning.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The expected facts were read from these official sample files, not generated
# from the answer under evaluation. One document is selected per question.
SAMPLE_NAMES = {
    "draft": "人形机器人产业方案2024_征求意见稿.docx",
    "note": "人形机器人产业方案2024_起草说明.docx",
    "pdf": "石景山区2024年统计公报.pdf",
    "image": "石景山区统计公报_第2页原表.png",
    "csv": "石景山区2024年产业指标_原表转录.csv",
}
CASES = [
    ("D01", "draft", "产业基地以哪些地方为核心？", "completed", ["首钢冬奥广场", "特钢园"], "方案拟建设，不是已建成。"),
    ("D02", "draft", "具身数据创新平台计划使用哪些技术，建设什么数据集？", "completed", ["动捕", "遥操作", "具身智能动作数据集"], "任务5提出的技术与数据集。"),
    ("D03", "draft", "感控一体研发验证平台提供哪些服务？", "completed", ["小试验证", "中试熟化", "工程开发", "工艺改进", "软件升级"], "任务6中的五类服务。"),
    ("D04", "draft", "医疗服务应用场景由谁牵头，围绕哪些医疗服务需求？", "completed", ["卫生健康委", "康复", "配送"], "任务13，不混入养老或金融的牵头单位。"),
    ("D05", "draft", "养老领域场景应用由谁牵头，满足哪些养老需求？", "completed", ["民政局", "陪伴", "护理"], "任务15。"),
    ("D06", "draft", "人形机器人产业基金计划坚持什么投资方向？", "completed", ["投早", "投小", "投硬科技"], "任务19，是计划而非已设立成果。"),
    ("D07", "note", "实施方案分为哪两部分，重点任务包括多少方面和多少项任务？", "completed", ["重点任务", "保障措施", "5", "21"], "两部分；重点任务5个方面、21项。"),
    ("D08", "note", "这份起草说明的署名单位和日期是什么？", "completed", ["中关村科技园区石景山园管理委员会", "北京市石景山区科学技术委员会", "2024", "8", "28"], "署名两单位；2024年8月28日。"),
    ("D09", "pdf", "2024年石景山区地区生产总值和同比增速是多少？", "completed", ["1312.9", "6.5"], "1312.9亿元；按不变价格同比增长6.5%；初步核算。"),
    ("D10", "pdf", "2024年末石景山区常住人口是多少，常住外来人口占比是多少？", "completed", ["56.1", "27.5"], "56.1万人；常住外来人口占27.5%。"),
    ("N01", "draft", "人形机器人产业基金目前实际已经投资了多少亿元？", "insufficient_evidence", [], "只有设立计划，没有实际投资金额。"),
    ("N02", "pdf", "2025年石景山区第三产业增加值是多少亿元？", "insufficient_evidence", [], "2024年度公报不能支持2025年的结果。"),
    ("N03", "image", "这张图片中第二产业增加值是多少？", "insufficient_evidence", [], "图片尚无OCR文字，不可借用同源PDF作答；此边界不调用模型。"),
    ("N04", "csv", "请以单文档文字问答方式解释这份CSV为什么有口径差异。", "insufficient_evidence", [], "CSV没有文档正文，表格查询由单表引擎负责；此边界不调用模型。"),
]


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", required=True, type=Path, help="已有运行目录，内含workspace.sqlite与model.json")
    parser.add_argument("--report", required=True, type=Path, help="新的JSON报告路径；不会覆盖已有报告")
    parser.add_argument("--project-id", help="多个项目导入了同名样本时，明确选择项目")
    parser.add_argument("--run-live", action="store_true", help="明确允许本次实际模型调用及相应额度消耗")
    args = parser.parse_args(argv)
    if not args.run_live:
        parser.error("未指定 --run-live；没有读取模型配置，也没有发送模型请求")
    return parser, args


def _read_sources(runtime, project_id):
    database = runtime / "workspace.sqlite"
    if not database.is_file():
        raise ValueError("指定运行目录缺少 workspace.sqlite")
    manifest = json.loads((PROJECT_ROOT / "samples" / "manifest.json").read_text())
    expected_hashes = {item["name"]: item["sha256"] for item in manifest}
    sources = {}
    # URI mode=ro prevents even accidental schema or workspace-record writes.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        for key, name in SAMPLE_NAMES.items():
            query = "SELECT * FROM sources WHERE name=? AND archived=0 AND status IN ('ready','attention')"
            values = [name]
            if project_id:
                query += " AND project_id=?"
                values.append(project_id)
            rows = connection.execute(query, values).fetchall()
            if not rows:
                raise ValueError(f"缺少已解析样本：{name}；请先通过应用导入该真实样本")
            if len(rows) != 1:
                raise ValueError(f"存在多份同名样本：{name}；请使用 --project-id 明确选择项目")
            source = dict(rows[0])
            if source["sha256"] != expected_hashes.get(name):
                raise ValueError(f"样本内容与官方样本清单不一致：{name}")
            for field in ("pages", "metadata", "warnings", "tables"):
                source[field] = json.loads(source[field])
            sources[key] = source
    return sources


def _write_report(path, report):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(path)


def main(argv=None):
    parser, args = _arguments(argv)
    runtime, output = args.runtime.expanduser().resolve(), args.report.expanduser().resolve()
    if output.suffix.lower() != ".json":
        parser.error("--report 必须是新的 .json 文件")
    if output.exists():
        parser.error("报告已存在，未覆盖；请指定新路径以保留首次结果")
    try:
        sources = _read_sources(runtime, args.project_id)
        # Import/load only after explicit live opt-in and sample validation.
        from backend.model_gateway import ModelConfig, ModelGateway
        from backend.document_engine import answer_document
        config = ModelConfig(runtime).load()
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "single-document real-source acceptance; read-only workspace; no automatic POST retries",
              "configured_models": config["models"], "cases": [],
              "review_note": "自动检查只核对状态、预期词语与原文锚点，需逐题审阅事实、单位、否定和计划/已完成边界。"}
    for case_id, key, question, status, terms, explanation in CASES:
        source, started, stages = sources[key], time.monotonic(), []
        gateway, raw = ModelGateway(config), []
        def model(messages, purpose):
            value = gateway(messages, purpose)
            raw.append({"purpose": purpose, "response": value})
            return value
        case = {"id": case_id, "source_id": source["id"], "source_name": source["name"],
                "sha256": source["sha256"], "parser_version": source["parser_version"],
                "question": question, "expected": {"status": status, "terms": terms, "explanation": explanation}}
        try:
            result = answer_document(source, question, model, lambda: False, stages.append)
            pages = {p["number"]: p["text"] for p in source["pages"]}
            anchors = all(e["source_id"] == source["id"] and e["sha256"] == source["sha256"]
                          and e["page"] in pages and pages[e["page"]][e["start"]:e["end"]] == e["quote"]
                          for e in result["evidence"])
            present = {term: term in result["text"] for term in terms}
            case.update(actual=result, anchor_check=anchors, term_presence=present, status_match=result["status"] == status,
                        automatic_checks_passed=anchors and all(present.values()) and result["status"] == status)
        except Exception as error:
            # Gateway and engine exceptions are sanitized; never include config,
            # requests, headers, or a traceback in the user-facing report.
            case.update(error={"type": type(error).__name__, "message": str(error)}, automatic_checks_passed=False)
        case.update(elapsed_seconds=round(time.monotonic() - started, 3), reported_models=gateway.models,
                    usage=gateway.usage, stages=stages, model_responses=raw)
        report["cases"].append(case)
        _write_report(output, report)
        print(f"{case_id}: {case.get('actual', {}).get('status', 'error')} ({case['elapsed_seconds']:.2f}s)", flush=True)
    usage = [entry for case in report["cases"] for entry in case["usage"]]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {"cases": len(CASES), "positive_cases": 10, "negative_cases": 4,
                         "automatic_checks_passed": sum(c["automatic_checks_passed"] for c in report["cases"]),
                         "recorded_model_responses": len(usage),
                         "reported_tokens": {k: sum(u.get(k, 0) for u in usage) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                         "case_elapsed_seconds": round(sum(c["elapsed_seconds"] for c in report["cases"]), 3)}
    _write_report(output, report)
    print(f"报告：{output}")
    return 0 if all(c["automatic_checks_passed"] for c in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
