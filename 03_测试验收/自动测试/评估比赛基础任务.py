"""Frozen single-table / single-document checks; reference answers stay local."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / '03_测试验收/测试样本/比赛基础任务新验证集-20261011.json'
FIRST = ROOT / '03_测试验收/验收证据/比赛基础任务DeepSeek首测-20261011.json'
EXPECTED_SHA = 'c83872ee813d4e82382747f3c44101a7c0e97077569fa1a43de83d1040ff9a0e'
spec = importlib.util.spec_from_file_location('public_evaluation_helpers', Path(__file__).with_name('评估公开统计.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load():
    if digest(DATASET) != EXPECTED_SHA:
        raise ValueError('基础题集与冻结摘要不一致')
    data = json.loads(DATASET.read_text())
    if not data['frozen_before_model_execution']:
        raise ValueError('题集未在模型执行前冻结')
    for source in data['source_snapshot']['original_pdfs']:
        if digest(ROOT / source['path']) != source['sha256']:
            raise ValueError('官方 PDF 偏离冻结版本')
    indicator = data['source_snapshot']
    if digest(ROOT / indicator['indicators_path']) != indicator['indicators_sha256']:
        raise ValueError('公开指标偏离冻结版本')
    database = ROOT / data['database']['path']
    if digest(database) != data['database']['sha256']:
        raise ValueError('只读数据库偏离冻结版本')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        for case in data['cases']:
            reference = case['reference']
            if reference.get('sql'):
                actual = [list(row) for row in db.execute(reference['sql'], reference.get('params', []))]
                if actual != reference['expected_rows']:
                    raise ValueError('独立 SQL 参考值发生变化：' + case['id'])
    return data


def numeric_coverage(result, expected_values):
    """Diagnostic only: values in supported answer text do not prove semantics."""
    text = '\n'.join(str(claim.get('text', '')) for claim in result.get('claims', [])
                     if claim.get('status') in {'supported', 'verified'})
    numbers = re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])', text)
    from decimal import Decimal
    present = {Decimal(number) for number in numbers}
    return [{'value': value, 'present': Decimal(value) in present} for value in expected_values]


def basic_contract(case, result, error):
    if error:
        return {'single_source_match': False, 'strict_sql_rows_match': False if case['scope'] == 'database' else None,
                'supported_answer_numeric_coverage': [], 'basic_execution_contract_match': False}
    supported = result.get('answer_status') == 'supported'
    if case['scope'] == 'database':
        tables = result.get('database_query', {}).get('validation', {}).get('tables', [])
        single = tables == ['main.public_observations']
        rows = helpers.strict_rows(result, case['reference'])
        expected_columns = case['reference'].get('expected_columns')
        columns = result.get('columns') == expected_columns if expected_columns is not None else None
        return {'single_source_match': single, 'executed_tables': tables, 'strict_sql_rows_match': rows,
                'explicit_columns_match': columns,
                'supported_answer_numeric_coverage': [], 'basic_execution_contract_match': bool(single and rows and supported and columns is not False)}
    target = case['document']
    scope = result.get('query_spec', {}).get('rag_scope', {})
    docs = scope.get('documents', [])
    citations = [e for e in result.get('evidence', []) if e.get('type') == 'document']
    single = (scope.get('scope_type') == 'document' and len(docs) == 1
              and docs[0].get('document_id') == target['document_id'] and docs[0].get('sha256') == target['sha256']
              and bool(citations) and all(e.get('locator', {}).get('document_id') == target['document_id']
                                         and e.get('locator', {}).get('sha256') == target['sha256'] for e in citations))
    coverage = numeric_coverage(result, case['reference']['expected_values'])
    return {'single_source_match': single, 'strict_sql_rows_match': None,
            'supported_answer_numeric_coverage': coverage,
            'basic_execution_contract_match': bool(single and supported and all(c['present'] for c in coverage))}


def execute(output, regression=False, case_ids=None):
    data = load()
    output = Path(output).resolve()
    if not regression and (output != FIRST.resolve() or FIRST.exists()):
        raise ValueError('首测仅允许一次并写入固定首测路径；已见题使用 --regression 和新的结果文件')
    if regression and not FIRST.exists():
        raise ValueError('尚未封存完整首测，不能标为回归')
    if regression and output == FIRST.resolve():
        raise ValueError('不能覆盖首测')
    if output.exists():
        raise FileExistsError('结果文件已存在，未执行任何模型请求')
    cases = data['cases']
    if case_ids:
        if not regression:
            raise ValueError('首测必须包含完整题集')
        if set(case_ids) - {c['id'] for c in cases}:
            raise ValueError('未知题目 ID')
        cases = [c for c in cases if c['id'] in set(case_ids)]
    health = helpers.api('/api/health')
    profile = helpers.api('/api/projects/public-statistics/query-context')
    selected = next((c for c in profile['connections'] if c['id'] == profile['connection_id']), {})
    if health.get('agent_mode') != 'model' or selected.get('model') != 'deepseek-v4.1-flash' or not profile['available']:
        raise ValueError('当前 DeepSeek 或公开统计项目未就绪')
    # Single-document runs are checked against the actual indexed file, not its title alone.
    for document in data['documents']:
        view = helpers.api('/api/documents/' + document['document_id'] + '/view')
        if view['document']['sha256'] != document['sha256']:
            raise ValueError('索引原件与冻结文档不一致')
    report = {'dataset_id': data['dataset_id'], 'dataset_sha256': EXPECTED_SHA,
              'run_kind': 'regression' if regression else 'first_exposure', 'phase': 'running',
              'started_at': helpers.now(), 'frozen_at': data['frozen_at'], 'model': selected['model'],
              'source_sha256': {n: digest(ROOT / '01_程序代码/backend' / n) for n in
                                ('agent.py', 'connections.py', 'database_query.py', 'sql_guard.py', 'rag_grounding.py', 'query_routing.py')},
              'project_id': data['project_id'], 'workers': 2, 'no_post_retries': True,
              'reference_answers_sent_to_model': False, 'human_review': 'pending',
              'selected_case_ids': [c['id'] for c in cases], 'requests': {}, 'results': [],
              'boundary': '执行与数值覆盖为自动诊断；单文档回答的语义正确、答全及引用支持仍需独立真人核验，不兑换官方得分。'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    lock = threading.Lock()

    def write():
        temp = output.with_suffix(output.suffix + '.pending')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(output)

    def run(case):
        payload = {'question': case['question'], 'project_id': data['project_id'], 'scope_override': case['scope']}
        if case['scope'] == 'knowledge':
            payload['rag_scope'] = {'scope_type': 'document', 'document_ids': [case['document']['document_id']]}
        with lock:
            report['requests'][case['id']] = {'started_at': helpers.now(), 'status': 'sent_once', 'payload': payload}
            write()
        started = time.perf_counter()
        result, error, status = {}, None, 200
        try:
            result = helpers.api('/api/query', payload)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                result = json.load(exc)
            except Exception:
                result = {}
            error = result.get('detail') or 'HTTP ' + str(exc.code)
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        citations = []
        for evidence in result.get('evidence', []):
            if evidence.get('type') != 'document':
                continue
            resolved, location_error = {}, None
            try:
                resolved = helpers.api('/api/evidence/resolve', {'locator': evidence.get('locator', {}), 'quote': evidence.get('snippet', '')})
            except Exception as exc:
                location_error = str(exc)
            citations.append({'evidence_id': evidence['id'], 'located': resolved.get('status') == 'located',
                              'locator': resolved.get('locator'), 'error': location_error, 'semantic_support': 'human_pending'})
        row = {'case_id': case['id'], 'category': case['category'], 'scope': case['scope'], 'question': case['question'],
               'http_status': status, 'error': error, 'duration_ms': elapsed,
               'nonempty_answer': bool(str(result.get('answer', '')).strip()),
               'model_calls': result.get('agent', {}).get('calls', 0), 'answer_status': result.get('answer_status'),
               **basic_contract(case, result, error), 'citation_location_checks': citations,
               'human_review': 'pending', 'result': result}
        with lock:
            report['results'].append(row)
            report['requests'][case['id']]['status'] = 'failed' if error else 'returned'
            write()
        print(json.dumps({k: row[k] for k in ('case_id', 'http_status', 'duration_ms', 'single_source_match',
                                             'strict_sql_rows_match', 'basic_execution_contract_match')}, ensure_ascii=False), flush=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, c) for c in cases]):
            future.result()
    report['results'].sort(key=lambda r: r['case_id'])
    rows = report['results']
    citations = [c for r in rows for c in r['citation_location_checks']]
    report['metrics'] = {'executed_questions': len(rows), 'technical_failures': sum(bool(r['error']) for r in rows),
                         'nonempty_answers': sum(r['nonempty_answer'] for r in rows),
                         'single_source_matches': sum(r['single_source_match'] for r in rows),
                         'basic_execution_contracts': {scope: {'matches': sum(r['basic_execution_contract_match'] for r in rows if r['scope'] == scope),
                                                             'denominator': sum(r['scope'] == scope for r in rows)} for scope in ('database', 'knowledge')},
                         'citation_locations': {'located': sum(c['located'] for c in citations), 'denominator': len(citations)},
                         'latency_ms': {'p50': helpers.percentile([r['duration_ms'] for r in rows], .5), 'p95': helpers.percentile([r['duration_ms'] for r in rows], .95)},
                         'database_unchanged': digest(ROOT / data['database']['path']) == data['database']['sha256'],
                         'semantic_accuracy': 'human_pending'}
    report.update(phase='completed', finished_at=helpers.now())
    write()
    print(json.dumps(report['metrics'], ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--regression', action='store_true')
    parser.add_argument('--output')
    parser.add_argument('--case', dest='case_ids', action='append')
    args = parser.parse_args()
    if args.execute:
        if not args.output:
            parser.error('--execute 需要新结果路径')
        execute(args.output, args.regression, args.case_ids)
    else:
        data = load()
        print(json.dumps({'dataset_sha256': EXPECTED_SHA, 'cases': len(data['cases']), 'model_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
