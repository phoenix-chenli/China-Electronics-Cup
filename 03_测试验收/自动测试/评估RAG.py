"""Validate the frozen public corpus, or explicitly evaluate a configured API.

No network or application data access occurs without --execute. This runner
reports retrieval coverage and server review states; neither proves correctness.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / '03_测试验收/测试样本/RAG冻结评估集.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(dataset_path=DEFAULT_DATASET):
    path = Path(dataset_path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != '1.0' or not data.get('synthetic'):
        raise ValueError('评估清单格式不正确，必须使用公开合成资料')
    source_root = (ROOT / data['source_directory']).resolve()
    if source_root != (ROOT / '02_数据资料/示例数据/docs').resolve():
        raise ValueError('评估资料必须来自指定公开示例目录')
    texts = {}
    for document in data['documents']:
        name = document['file']
        if Path(name).name != name or name in texts:
            raise ValueError('文档名重复或超出指定目录')
        source = source_root / name
        if source.is_symlink() or digest(source) != document['sha256']:
            raise ValueError('冻结资料已改变：' + name)
        text = source.read_text(encoding='utf-8')
        if '公开合成 RAG 测试资料' not in text:
            raise ValueError('资料未标明合成来源：' + name)
        texts[name] = text
    cases = data['cases']; ids = set(); questions = set()
    for case in cases:
        if case['id'] in ids or case['question'] in questions:
            raise ValueError('用例编号或问题重复')
        ids.add(case['id']); questions.add(case['question'])
        if case['expected_status'] not in {'supported', 'refused', 'partial'}:
            raise ValueError('用例预期状态不正确：' + case['id'])
        required = set(case['required_sources'])
        if not required <= texts.keys():
            raise ValueError('用例引用未知文档：' + case['id'])
        observed = set()
        for requirement in case['requirements']:
            if not requirement['text'] or not requirement['evidence']:
                raise ValueError('必要事实或来源缺失：' + case['id'])
            for evidence in requirement['evidence']:
                source, quote = evidence['source'], evidence['quote']
                if source not in texts or not quote or quote not in texts[source]:
                    raise ValueError('预期证据不在冻结原文中：' + case['id'])
                observed.add(source)
        if required != observed:
            raise ValueError('必要来源清单与证据不一致：' + case['id'])
        if case['expected_status'] in {'refused', 'partial'} and not case['missing_requirements'] and not case['conflicts']:
            raise ValueError('拒答/部分支持缺少明确缺口：' + case['id'])
        if case['expected_status'] == 'supported' and not case['requirements']:
            raise ValueError('可回答问题没有必要事实：' + case['id'])
    counts = Counter(c['expected_status'] for c in cases)
    if dict(counts) != data['counts']:
        raise ValueError('清单统计与实际用例不一致')
    return data, texts, {'dataset_id': data['dataset_id'], 'dataset_sha256': digest(path),
                         'documents': len(texts), 'cases': len(cases),
                         'expected_statuses': dict(counts), 'validation': 'passed',
                         'generated_quality': 'not_evaluated', 'human_review': 'pending'}


class Client:
    def __init__(self, base, timeout=210):
        parsed = urlparse(base)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != 8765 or parsed.path not in {'', '/'} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('仅支持 http://127.0.0.1:8765 的本地主服务')
        self.base = base.rstrip('/'); self.timeout = timeout

    def request(self, path, body=None, content_type='application/json'):
        payload = body
        if body is not None and not isinstance(body, bytes):
            payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        req = Request(self.base + path, data=payload,
                      headers={'Content-Type': content_type} if body is not None else {})
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try: detail = json.loads(exc.read()).get('detail', str(exc.code))
            except (ValueError, UnicodeError): detail = str(exc.code)
            raise ValueError(f'本机 API {exc.code}：{detail}') from None
        except URLError as exc:
            raise ValueError('本机服务未连接：' + str(exc.reason)) from None

    def import_public(self, filename, text):
        boundary = 'EvidenceFlowEval' + uuid.uuid4().hex
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: text/markdown; charset=utf-8\r\n\r\n')
        body = head.encode('utf-8') + text.encode('utf-8') + f'\r\n--{boundary}--\r\n'.encode()
        return self.request('/api/import', body, 'multipart/form-data; boundary=' + boundary)


def prepare_documents(client, data, texts, import_missing):
    existing = client.request('/api/documents')
    selected = {}
    for document in data['documents']:
        name = document['file']; expected = document['sha256']
        matching = [d for d in existing if d.get('name') == name and d.get('sha256') == expected]
        if not matching:
            if not import_missing:
                raise ValueError('未找到固定版本，请先导入公开资料，或显式指定 --import-missing：' + name)
            response = client.import_public(name, texts[name])
            candidate = response.get('document', {})
            if not response.get('ok') or candidate.get('sha256') != expected:
                raise ValueError('公开资料导入未通过固定版本核对：' + name)
            matching = [candidate]
        # Multiple copies with identical digest are equivalent but the scope
        # still records a specific document ID, never an arbitrary updated file.
        selected[name] = matching[0]['id']
    return selected


def normalized(text):
    return ''.join(str(text).split())


def inspect_result(case, result, duration_ms):
    sources = result.get('evidence', [])
    found = {e.get('source') for e in sources}
    required = set(case['required_sources'])
    source_hits = len(found & required)
    span_checks = []
    for requirement in case['requirements']:
        for evidence in requirement['evidence']:
            located = any(e.get('source') == evidence['source'] and normalized(evidence['quote']) in normalized(e.get('snippet', '')) for e in sources)
            span_checks.append({'requirement_id': requirement['id'], 'source': evidence['source'],
                                'quote': evidence['quote'], 'exact_text_in_pack': located})
    reviews = [{'claim_id': claim.get('id'), 'text': claim.get('text'),
                'evidence_ids': claim.get('evidence_ids', []),
                'semantic_check': claim.get('semantic_check'), 'human_support': 'pending'}
               for claim in result.get('claims', [])]
    retrieval = result.get('rag', {}).get('retrieval', [])
    records = retrieval if isinstance(retrieval, list) else [retrieval]
    return {'case_id': case['id'], 'question': case['question'], 'run_id': result.get('run_id'),
            'duration_ms': duration_ms, 'expected_status': case['expected_status'],
            'returned_status': result.get('answer_status'),
            'status_label_matches_expected': result.get('answer_status') == case['expected_status'],
            'source_recall_at_evidence_pack': source_hits / len(required) if required else None,
            'source_hits': source_hits, 'required_source_count': len(required),
            'necessary_span_checks': span_checks,
            'necessary_span_exact_recall': sum(s['exact_text_in_pack'] for s in span_checks) / len(span_checks) if span_checks else None,
            'returned_answer': result.get('answer'), 'missing_requirements': result.get('missing_requirements', []),
            'conflicts': result.get('conflicts', []), 'refusal_reason': result.get('refusal_reason'),
            'retrieval': records, 'claim_reviews': reviews,
            'execution_events': result.get('events', []),
            'human_answer_quality': 'pending', 'human_citation_precision': 'pending',
            'human_citation_completeness': 'pending', 'human_refusal_quality': 'pending',
            'boundary': '状态匹配与精确文本召回只作诊断，模型支持检查不是真实正确率。'}


def execute(args, data, texts, summary):
    if not args.output:
        raise ValueError('--execute 必须指定 --output；不把真实运行结果默认写入交接目录')
    client = Client(args.base_url)
    config = client.request('/api/agent/config')
    if config.get('query_scope') != 'knowledge' or config.get('mode') == 'local' or not config.get('available'):
        raise ValueError('请先在主问答设置中配置实际可用的生成模型并选择“知识库问答”；本 runner 不修改配置')
    providers = client.request('/api/rag/status').get('providers', {})
    selected = prepare_documents(client, data, texts, args.import_missing)
    cases = data['cases']
    if args.case_ids:
        wanted = set(args.case_ids.split(',')); cases = [c for c in cases if c['id'] in wanted]
        if wanted != {c['id'] for c in cases}: raise ValueError('指定了不存在的用例 ID')
    if args.limit: cases = cases[:args.limit]
    rows = []
    for case in cases:
        started = time.perf_counter()
        try:
            # Always include all twenty fixed public sources: hard negatives
            # and contradictions must remain eligible for retrieval.
            result = client.request('/api/query', {'question': case['question'],
                'inherit_context': False, 'rag_scope': {'scope_type': 'corpus', 'document_ids': list(selected.values())}})
            row = inspect_result(case, result, round((time.perf_counter() - started) * 1000, 2))
        except ValueError as exc:
            row = {'case_id': case['id'], 'question': case['question'], 'execution_error': str(exc),
                   'duration_ms': round((time.perf_counter() - started) * 1000, 2),
                   'human_answer_quality': 'pending'}
        rows.append(row)
        print(json.dumps({'case_id': case['id'], 'returned_status': row.get('returned_status'),
                          'execution_error': row.get('execution_error')}, ensure_ascii=False), flush=True)
        report = {**summary, 'generated_quality': 'human_review_pending',
                  'executed_at': datetime.now(timezone.utc).isoformat(), 'selected_documents': selected,
                  'provider_snapshot': providers, 'completed_cases': len(rows), 'requested_cases': len(cases),
                  'results': rows, 'metrics': {
                    'execution_errors': sum('execution_error' in r for r in rows),
                    'source_micro_recall_at_evidence_pack': sum(r.get('source_hits', 0) for r in rows) / sum(r.get('required_source_count', 0) for r in rows) if sum(r.get('required_source_count', 0) for r in rows) else None,
                    'status_label_matches': sum(r.get('status_label_matches_expected', False) for r in rows)},
                  'metric_boundary': '运行失败、拒答及模型自检均独立记录；自动状态/召回指标不能宣布答案正确率。'}
        output = Path(args.output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', default=str(DEFAULT_DATASET))
    parser.add_argument('--execute', action='store_true', help='实际调用已配置的生成模型，可能发送公开测试资料并产生模型费用')
    parser.add_argument('--import-missing', action='store_true', help='仅在 --execute 时受控导入缺失的公开合成资料')
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--output', help='实际运行报告绝对路径，建议指定工作区外的本机目录')
    parser.add_argument('--case-ids', help='逗号分隔的冻结用例编号')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    if args.limit < 0: parser.error('--limit 不能小于零')
    data, texts, summary = validate(args.dataset)
    if args.execute: execute(args, data, texts, summary)
    else: print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
