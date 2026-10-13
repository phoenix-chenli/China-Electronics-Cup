"""Frozen Chinook reference validation and explicitly requested model evaluation.

Reference SQL/answers never enter model input. Paid POSTs are submitted once;
first-run evidence cannot be overwritten or presented as an unseen regression.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import httpx

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / '03_测试验收/测试样本/chinook验证集-20261012.json'
FIRST = ROOT / '03_测试验收/验收证据/ChinookTokenRhythm首测-20261012.json'


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()


def equal(a, b, tolerance):
    if type(a) in (int, float) and type(b) in (int, float):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, abs_tol=tolerance, rel_tol=1e-9)
    return type(a) is type(b) and a == b


def rows_match(actual, case):
    expected = case['expected_rows']; tolerance = case.get('numeric_tolerance', 1e-6)
    same = lambda a, b: len(a) == len(b) and all(equal(x, y, tolerance) for x, y in zip(a, b))
    if len(actual) != len(expected): return False
    if case.get('order_matters', True): return all(same(a, b) for a, b in zip(actual, expected))
    remaining = list(actual)
    for row in expected:
        found = next((i for i, value in enumerate(remaining) if same(value, row)), None)
        if found is None: return False
        remaining.pop(found)
    return True


def validate():
    checksum = DATASET.with_suffix(DATASET.suffix + '.sha256').read_text().split()[0]
    raw = DATASET.read_bytes()
    # Git normalizes the teammate's Windows CRLF file to LF. Accept exactly
    # those two byte encodings; changed content still fails before any request.
    windows_digest = hashlib.sha256(raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')).hexdigest()
    if digest(DATASET) != checksum and windows_digest != checksum:
        raise ValueError('Chinook 题集偏离冻结摘要')
    data = json.loads(DATASET.read_text())
    database = ROOT / data['source']['file']
    if digest(database) != data['source']['sha256']: raise ValueError('Chinook 原库偏离冻结摘要')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        for case in data['cases']:
            cursor = db.execute(case['reference_sql'])
            if [x[0] for x in cursor.description] != case['columns'] or not rows_match([list(x) for x in cursor], case):
                raise ValueError('参考 SQL/冻结值不一致：' + case['id'])
    return data, checksum


def execute(output, regression=False, case_ids=None):
    data, checksum = validate(); output = Path(output).resolve()
    if output.exists(): raise FileExistsError('结果文件已存在，未发起模型调用')
    if not regression and (output != FIRST.resolve() or FIRST.exists()):
        raise ValueError('首测只能完整执行一次并封存；已见题必须标记回归')
    if regression and (not FIRST.exists() or output == FIRST.resolve()):
        raise ValueError('回归需已有首测，并使用新的输出路径')
    selected = data['cases']
    if case_ids:
        if not regression: raise ValueError('首测必须执行完整题集')
        wanted = set(case_ids)
        if wanted - {x['id'] for x in selected}: raise ValueError('筛选包含未知案例')
        selected = [x for x in selected if x['id'] in wanted]
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=630, trust_env=False) as client:
        config = client.get('/api/agent/config').json()
        options = client.get('/api/connections').json()['items']
        model = next((x for x in options if x['id'] == config.get('connection_id')), None)
        if config.get('mode') != 'model' or not config.get('available') or not model:
            raise ValueError('真实主模型尚未就绪')
        report = {'dataset_id':data['dataset_id'], 'dataset_sha256':digest(DATASET),
                  'declared_windows_dataset_sha256':checksum, 'newline_normalization':'Git CRLF/LF only',
                  'run_kind':'regression' if regression else 'first_exposure', 'phase':'running',
                  'started_at':now(), 'requested_model':model['model'], 'endpoint':model['endpoint'],
                  'selected_case_ids':[x['id'] for x in selected], 'project_id':data['project_id'],
                  'workers':1, 'no_post_retries':True, 'reference_answers_sent_to_model':False,
                  'human_review':'pending', 'source_sha256':{n:digest(ROOT / '01_程序代码/backend' / n) for n in
                    ('agent.py','connections.py','database_query.py','database_answer.py','sql_guard.py','rag_grounding.py')},
                  'requests':{}, 'results':[],
                  'boundary':'严格查询结果匹配是自动诊断；自然语言语义、答全及官方计分仍需独立复核。题目由本项目自行编制。'}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as handle: json.dump(report, handle, ensure_ascii=False, indent=2)
        def write():
            pending = output.with_suffix(output.suffix + '.pending')
            pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            pending.replace(output)
        for case in selected:
            payload = {'question':case['question'], 'project_id':data['project_id'], 'scope_override':'database'}
            report['requests'][case['id']] = {'status':'sent_once', 'started_at':now(), 'payload_fields':list(payload)}
            write(); started = time.perf_counter(); result = {}; error = None; status = None
            try:
                response = client.post('/api/query', json=payload); status = response.status_code
                result = response.json()
                if status != 200: error = result.get('detail', '本机服务拒绝请求')
            except (httpx.HTTPError, ValueError) as exc: error = type(exc).__name__
            query = result.get('database_query', {})
            executed = bool(query.get('sql'))
            matched = not error and executed and rows_match([list(x.values()) for x in result.get('rows', [])], case)
            item = {'id':case['id'], 'category':case['category'], 'question':case['question'],
                    'http_status':status, 'error':error, 'duration_ms':round((time.perf_counter()-started)*1000,2),
                    'query_executed':executed, 'strict_sql_rows_match':matched,
                    'nonempty_answer':bool(str(result.get('answer') or result.get('headline') or '').strip()),
                    'answer_status':result.get('answer_status'), 'actual':result,
                    'reference_sql':case['reference_sql'], 'expected_rows':case['expected_rows']}
            report['results'].append(item); report['requests'][case['id']]['status']='finished'; write()
            print(json.dumps({k:item[k] for k in ('id','strict_sql_rows_match','answer_status','duration_ms','error')}, ensure_ascii=False), flush=True)
        results = report['results']; durations = sorted(x['duration_ms'] for x in results)
        report.update(phase='completed', finished_at=now(), database_original_unchanged=digest(ROOT / data['source']['file']) == data['source']['sha256'])
        report['summary'] = {'cases':len(results), 'strict_sql_rows_match':sum(x['strict_sql_rows_match'] for x in results),
                             'queries_executed':sum(x['query_executed'] for x in results),
                             'nonempty_answers':sum(x['nonempty_answer'] for x in results),
                             'http_failures':sum(x['http_status'] != 200 for x in results),
                             'calls':sum(x['actual'].get('agent',{}).get('calls',0) for x in results),
                             'p50_ms':durations[len(durations)//2] if durations else None,
                             'semantic_accuracy':'not_independently_reviewed'}
        write(); print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
        return report


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, default=FIRST); parser.add_argument('--regression', action='store_true')
    parser.add_argument('--case', action='append'); args = parser.parse_args()
    if args.execute: execute(args.output, args.regression, args.case)
    else:
        data, checksum = validate(); print(json.dumps({'reference_validation':'passed','cases':len(data['cases']),'dataset_sha256':checksum,'model_calls':0},ensure_ascii=False))


if __name__ == '__main__': main()
