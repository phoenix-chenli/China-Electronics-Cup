"""Reproduce 13 real single-table answers without changing user records.

No network calls occur unless --run-live is explicitly present. The live path
calls AnswerService._table directly: it does not create projects, sources,
drafts or persistent answer jobs. Normal successful execution makes 23 model
requests (10 planning + verification pairs, and 3 planning-only refusals).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from backend.answer_service import AnswerService
from backend.model_gateway import ModelConfig, ModelGateway
from backend.store import Store

SAMPLE_NAME = "石景山区2024年产业指标_原表转录.csv"
REPORT_FORMAT = "zhiheng-single-table-live-v1"


def build_cases(sample):
    """Independent CSV/Decimal truth; never ask the engine for expected results."""
    if not sample.is_file():
        raise ValueError(f"缺少真实样本：{sample}")
    with sample.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    needed = {"指标", "分类", "增加值_亿元", "同比增长_pct", "比重_pct"}
    if not rows or not needed.issubset(rows[0]):
        raise ValueError("真实样本为空或缺少验收所需字段")
    industries = [row for row in rows if row["分类"] == "行业"]
    sectors = [row for row in rows if row["分类"] == "产业"]
    if len(rows) != 19 or len(industries) != 16 or len(sectors) != 2:
        raise ValueError("样本行数或统计层级发生变化，请先核对13题验收范围")
    by_name = {row["指标"]: row for row in rows}
    required = {"工业", "建筑业", "地区生产总值", "第二产业", "第三产业"}
    if not required.issubset(by_name):
        raise ValueError("样本缺少产业合计、GDP或工业/建筑业记录")
    negatives = [row for row in industries if Decimal(row["同比增长_pct"]) < 0]
    def pick(row, *fields):
        return {field: row[field] for field in fields}
    def amount(row):
        return Decimal(row["增加值_亿元"])
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        sector_sum = sum((amount(row) for row in sectors), Decimal(0))
        ratio = amount(by_name["第三产业"]) / amount(by_name["地区生产总值"]) * 100
        average = sum((amount(row) for row in industries), Decimal(0)) / len(industries)
        industry_building_sum = amount(by_name["工业"]) + amount(by_name["建筑业"])
    highest = max(industries, key=lambda row: Decimal(row["同比增长_pct"]))
    cases = [
        {"id":"T01", "question":"2024年第二产业和第三产业的增加值和比重分别是多少？", "kind":"records", "expected":[pick(row,"指标","增加值_亿元","比重_pct") for row in sectors]},
        {"id":"T02", "question":"列出同比增长小于0的行业及其同比增长率。", "kind":"records", "expected":[pick(row,"指标","同比增长_pct") for row in negatives]},
        {"id":"T03", "question":"仅看行业层级，按增加值从高到低列出前3个行业及增加值。", "kind":"records", "ordered":True, "expected":[pick(row,"指标","增加值_亿元") for row in sorted(industries,key=amount,reverse=True)[:3]]},
        {"id":"T04", "question":"按分类分组，各有多少条记录？", "kind":"groups", "expected":{key:str(value) for key,value in Counter(row["分类"] for row in rows).items()}},
        {"id":"T05", "question":"只把分类为产业的两条记录的增加值相加，合计多少亿元？", "kind":"aggregate", "operation":"sum", "expected":str(sector_sum)},
        {"id":"T06", "question":"用第三产业增加值除以地区生产总值，重新计算第三产业占比百分数。", "kind":"ratio", "expected":str(ratio)},
        {"id":"T07", "question":"仅看行业层级，哪个行业同比增长率最高？给出行业名和增长率。", "kind":"records", "expected":[pick(highest,"指标","同比增长_pct")]},
        {"id":"T08", "question":"对16个行业的增加值计算未加权的算术平均数，是多少亿元？", "kind":"aggregate", "operation":"avg", "expected":str(average)},
        {"id":"T09", "question":"只计算工业与建筑业增加值合计，不要改写原表，并保留与第二产业的已知差异提示。", "kind":"aggregate", "operation":"sum", "expected":str(industry_building_sum), "must_preserve_discrepancy":True},
        {"id":"T10", "question":"只看行业分类，同比增速小于0的行业共有多少个？", "kind":"aggregate", "operation":"count", "expected":str(len(negatives))},
        {"id":"T11", "question":"把全部行的增加值直接累加，得到总增加值是多少？", "kind":"negative", "expected_status":["needs_clarification","insufficient_evidence"]},
        {"id":"T12", "question":"请据此预测2025年石景山区地区生产总值的准确数值。", "kind":"negative", "expected_status":["insufficient_evidence","needs_clarification"]},
        {"id":"T13", "question":"哪个行业最好？", "kind":"negative", "expected_status":["needs_clarification","insufficient_evidence"]},
    ]
    return cases


def scalar_equal(actual, expected):
    try: return Decimal(str(actual))==Decimal(str(expected))
    except Exception: return actual==expected

def evaluate(case,status,result,source):
    problems=[]
    if case['kind']=='negative':
        if status not in case['expected_status']: problems.append('未对缺少依据/范围不清问题拒答或澄清')
        if result.get('rows') or result.get('evidence'): problems.append('负向问题发布了数值结果或证据性结论')
        return problems
    if status!='completed': return ['正向问题没有完成：'+status]
    columns=result.get('columns',[]); rows=result.get('rows',[]); mapped=[dict(zip(columns,row)) for row in rows]
    if case['kind']=='records':
        expected=case['expected']
        if len(expected)!=len(mapped): problems.append(f'结果行数不符：{len(mapped)} vs {len(expected)}')
        remaining=list(mapped)
        for index,wanted in enumerate(expected):
            candidates=remaining[:1] if case.get('ordered') else remaining
            found=next((r for r in candidates if all(k in r and scalar_equal(r[k],v) for k,v in wanted.items())),None)
            if found is None: problems.append('缺少正确结果：'+json.dumps(wanted,ensure_ascii=False))
            else: remaining.remove(found)
    elif case['kind']=='groups':
        if len(rows)!=len(case['expected']) or '分类' not in columns: problems.append('分组结果列/行数不符')
        else:
            others=[c for c in columns if c!='分类']
            if len(others)!=1: problems.append('分组应有一列记录数')
            elif any(not scalar_equal(r.get(others[0]),case['expected'].get(r.get('分类'))) for r in mapped): problems.append('分类记录数错误')
    else:
        if case['kind']=='ratio':
            if len(rows)!=1 or '占比_pct' not in columns or not scalar_equal(mapped[0].get('占比_pct'),case['expected']): problems.append('重新计算占比不符')
        elif len(rows)!=1 or len(rows[0])!=1 or not scalar_equal(rows[0][0],case['expected']): problems.append('聚合结果不符')
        plan=result.get('query',{}).get('plan',{})
        if case.get('operation') and not any(a.get('op')==case['operation'] for a in plan.get('aggregates',[])): problems.append('聚合操作与问题不符')
    query=result.get('query',{})
    if not query.get('sql') or not isinstance(query.get('parameters'),list): problems.append('缺少实际SQL或绑定参数')
    evidence=result.get('evidence',[])
    if len(evidence)!=1 or evidence[0].get('sha256')!=source['sha256'] or evidence[0].get('table_name')!='数据表' or not evidence[0].get('row_numbers'): problems.append('来源引用缺少真实摘要、表或行号')
    if case.get('must_preserve_discrepancy'):
        notes=' '.join(result.get('notes',[]))
        if not all(v in notes for v in ('91.8','90.8','1.0')): problems.append('未保留原表1.0亿元差异提示')
    if result.get('verification',{}).get('status')!='passed': problems.append('未通过语义复核')
    return problems

class RecordedGateway:
    def __init__(self,config,event): self.base=ModelGateway(config,event.is_set); self.calls=[]
    def __call__(self,messages,purpose):
        started=time.monotonic()
        call={'purpose':purpose,'started_at':datetime.now(timezone.utc).isoformat()}; self.calls.append(call)
        try:
            response=self.base(messages,purpose); call['response']=response; return response
        except Exception as exc:
            call['error']=str(exc); raise
        finally: call['elapsed_ms']=round((time.monotonic()-started)*1000)


def load_source(runtime, sample):
    database = runtime / "workspace.sqlite"
    if not runtime.is_dir() or not database.is_file():
        raise ValueError(f"运行目录不存在或缺少workspace.sqlite：{runtime}")
    store = Store(runtime)
    sources = [source for project in store.projects() for source in store.sources(project["id"]) if source["name"] == SAMPLE_NAME]
    if not sources:
        raise ValueError(f"运行目录中没有已导入的「{SAMPLE_NAME}」；请先在本机界面导入项目自带示例资料")
    if len(sources) != 1:
        raise ValueError("多个项目含同名样本，无法唯一确定验收来源；请指定仅含一个演示项目的验收运行目录")
    source = sources[0]
    if source["status"] not in {"ready", "attention"}:
        raise ValueError("样本尚未完成解析，请等待资料处理完成")
    if not source.get("data_path") or not Path(source["data_path"]).is_file():
        raise ValueError("样本解析表文件缺失，请重新导入样本")
    if not any(table["name"] == "数据表" for table in source.get("tables", [])):
        raise ValueError("样本没有解析出预期的「数据表」")
    digest = hashlib.sha256(sample.read_bytes()).hexdigest()
    if source["sha256"] != digest:
        raise ValueError("已导入样本与项目samples原始CSV摘要不同，不能用这套真值验收")
    original = Path(source["original_path"])
    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != digest:
        raise ValueError("运行目录中的样本原件缺失或摘要发生变化")
    return source


def load_report(path, source, config, cases):
    if path.suffix.lower() != ".json":
        raise ValueError("--report必须是.json文件路径")
    if path.exists():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise ValueError("现有报告无法读取；请另选输出路径以保留原文件") from error
        if report.get("format") != REPORT_FORMAT or report.get("source", {}).get("sha256") != source["sha256"] or report.get("cases") != cases or not isinstance(report.get("attempts"), list):
            raise ValueError("现有文件不是同一套验收报告；请另选--report路径，未覆盖原文件")
        return report
    return {
        "format": REPORT_FORMAT,
        "source": {key: source[key] for key in ("id","name","sha256","parser_version")},
        "models_requested": config["models"],
        "ground_truth_method": "独立csv.DictReader读取原始CSV并用Decimal计算；不调用表引擎构造真值",
        "cases": cases,
        "attempts": [],
    }


def save_report(path, report):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, help="显式指定本机运行目录，需包含workspace.sqlite和model.json")
    parser.add_argument("--report", type=Path, help="JSON报告输出路径；已有同一验收报告将追加尝试、保留首次失败")
    parser.add_argument("--run-live", action="store_true", help="明确启用真实模型调用，正常全程预计23次请求")
    args = parser.parse_args(argv)
    if args.run_live and (args.runtime is None or args.report is None):
        parser.error("--run-live必须同时指定--runtime和--report")
    try:
        sample = BASE / "samples" / SAMPLE_NAME
        cases = build_cases(sample)
        if not args.run_live:
            print("已准备13题：10个正向、3个范围不清或依据不足问题。未调用模型，未修改用户记录。")
            print("真实验收需显式指定：--run-live --runtime <本机运行目录> --report <报告.json>")
            print("正常全程预计23次模型请求（10次问数＋10次复核＋3次拒答/澄清）；不自动重试。")
            return 0
        runtime, output = args.runtime.expanduser().resolve(), args.report.expanduser().resolve()
        source = load_source(runtime, sample)
        config = ModelConfig(runtime).load()
        report = load_report(output, source, config, cases)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Prove the output is writable before any metered network request.
        save_report(output, report)
    except (ValueError, OSError, sqlite3.Error, KeyError) as error:
        print(f"验收未启动：{error}", file=sys.stderr)
        return 2
    failures = 0
    for case in cases:
        event = threading.Event()
        gateway = RecordedGateway(config, event)
        progress = []
        started = time.monotonic()
        record = {"case_id":case["id"], "question":case["question"], "started_at":datetime.now(timezone.utc).isoformat()}
        try:
            status, result = AnswerService._table(None, source, {"question":case["question"], "table_name":"数据表"}, gateway, event, progress.append)
            record.update(status=status, result=result, problems=evaluate(case,status,result,source))
        except Exception as error:
            record.update(status="exception", error=str(error), problems=[str(error)])
        record.update(elapsed_ms=round((time.monotonic()-started)*1000), model_calls=gateway.calls, models_reported=gateway.base.models, usage=gateway.base.usage, progress=progress)
        record["passed"] = not record["problems"]
        failures += not record["passed"]
        report["attempts"].append(record)
        save_report(output, report)
        print(json.dumps({key:record[key] for key in ("case_id","status","passed","elapsed_ms","problems")}, ensure_ascii=False), flush=True)
    print(f"本次验收：{len(cases)-failures}/{len(cases)}通过；报告：{output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
