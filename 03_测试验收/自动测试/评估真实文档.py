"""Real original PDF/Word regression. References stay outside model input.

Automatic checks measure necessary fact coverage and source location, not an
independent human judgment of semantic correctness.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import httpx

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / '03_测试验收/测试样本/真实Word与PDFTokenRhythm基线-20261012.json'
FIRST = ROOT / '03_测试验收/验收证据/真实Word与PDFTokenRhythm基线首测-20261012.json'

def now(): return datetime.now(timezone.utc).isoformat()
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def normalize(text): return re.sub(r'\s+', '', str(text))
def case_documents(case): return case.get('documents') or [case['document']]


def numeric_coverage(text, required):
    tokens = re.findall(r'(?<![\d.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])', str(text))
    numbers = [float(x.replace(',', '')) for x in tokens]
    return {str(value):any(math.isclose(float(value), x, abs_tol=1e-7, rel_tol=1e-9) for x in numbers) for value in required}


def execute(output, regression=False, case_ids=None):
    if digest(DATASET) != DATASET.with_suffix('.sha256').read_text().split()[0]: raise ValueError('文档题集偏离冻结摘要')
    data = json.loads(DATASET.read_text()); output = Path(output).resolve()
    if output.exists(): raise FileExistsError('结果文件已存在，未发起模型调用')
    if not regression and (FIRST.exists() or output != FIRST.resolve()): raise ValueError('基线只能封存一次；再次执行必须标记回归')
    if regression and (not FIRST.exists() or output == FIRST.resolve()): raise ValueError('回归必须保留原基线并使用新路径')
    selected = data['cases']
    if case_ids:
        if not regression: raise ValueError('基线须执行完整题集')
        wanted = set(case_ids)
        if wanted - {x['id'] for x in selected}: raise ValueError('筛选包含未知案例')
        selected = [x for x in selected if x['id'] in wanted]
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=630, trust_env=False) as client:
        def get(path):
            r = client.get(path); r.raise_for_status(); return r.json()
        config = get('/api/agent/config')
        model = next((x for x in get('/api/connections')['items'] if x['id'] == config.get('connection_id')), None)
        if not model or config.get('mode') != 'model' or not config.get('available'): raise ValueError('真实主模型尚未就绪')
        originals = {}
        for case in selected:
            for doc in case_documents(case):
                did = doc['document_id']
                if did not in originals: originals[did]=get('/api/documents/' + did + '/view')
                if originals[did]['document']['sha256'] != doc['sha256']: raise ValueError('原件版本与冻结题集不同')
                for quote in case['reference'].get('reference_quotes', []):
                    texts = [get(f'/api/documents/{did}/pages/{page}')['text'] for page in case['reference']['expected_pages']]
                    if not any(normalize(quote) in normalize(text) for text in texts): raise ValueError('参考摘录不在原件')
        report = {'dataset_id':data['dataset_id'], 'dataset_sha256':digest(DATASET), 'phase':'running',
                  'run_kind':'regression' if regression else 'provider_baseline_mixed_prior_exposure',
                  'started_at':now(), 'requested_model':model['model'], 'endpoint':model['endpoint'],
                  'workers':1, 'no_post_retries':True, 'reference_answers_sent_to_model':False, 'human_review':'pending',
                  'source_sha256':{n:digest(ROOT / '01_程序代码/backend' / n) for n in
                    ('agent.py','connections.py','rag_grounding.py','rag_retrieval.py','rag_index.py','documents.py')},
                  'selected_case_ids':[x['id'] for x in selected], 'requests':{}, 'results':[],
                  'boundary':'必要数值/词项覆盖、回答状态和原文定位为自动诊断；不能将模型自评或词项命中当作独立语义正确率。PDF题为换服务回归，Word新题也由同一项目出题。'}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x') as handle: json.dump(report, handle, ensure_ascii=False, indent=2)
        def write():
            pending = output.with_suffix(output.suffix + '.pending')
            pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n'); pending.replace(output)
        for case in selected:
            docs=case_documents(case); hashes={doc['document_id']:doc['sha256'] for doc in docs}
            payload = {'question':case['question'], 'project_id':data['project_id'], 'scope_override':'knowledge',
                       'rag_scope':{'scope_type':'document' if len(docs)==1 else 'corpus','document_ids':list(hashes)}}
            report['requests'][case['id']]={'status':'sent_once','started_at':now(),'payload_fields':list(payload)}
            write(); started=time.perf_counter(); result={}; error=None; status=None
            try:
                response=client.post('/api/query',json=payload);status=response.status_code;result=response.json()
                if status!=200:error=result.get('detail','本机服务拒绝请求')
            except (httpx.HTTPError, ValueError) as exc:error=type(exc).__name__
            text=str(result.get('answer') or ''); ref=case['reference']; cleaned=normalize(text)
            values=numeric_coverage(text,ref.get('expected_values',[]))
            terms={t:normalize(t) in cleaned for t in ref.get('required_terms',[])}
            alternatives={ '|'.join(group):any(normalize(t) in cleaned for t in group) for group in ref.get('required_alternatives',[])}
            citations=[]
            for evidence in result.get('evidence',[]):
                if evidence.get('type')!='document':continue
                location={};location_error=None
                try:
                    r=client.post('/api/evidence/resolve',json={'locator':evidence.get('locator',{}),'quote':evidence.get('snippet','')})
                    r.raise_for_status();location=r.json()
                except (httpx.HTTPError,ValueError) as exc:location_error=type(exc).__name__
                locator=evidence.get('locator',{})
                citations.append({'evidence_id':evidence['id'],'document_id':locator.get('document_id'),
                                  'page':locator.get('page'),'located':location.get('status')=='located',
                                  'sha256_matches':locator.get('sha256')==hashes.get(locator.get('document_id')),
                                  'error':location_error})
            cited_ids={eid for claim in result.get('claims',[]) for eid in claim.get('evidence_ids',[])}
            for citation in citations:citation['used_in_answer']=citation['evidence_id'] in cited_ids
            expected_pages=ref.get('expected_source_pages') or {doc['document_id']:ref['expected_pages'] for doc in docs}
            source_hit=all(any(x['located'] and x['sha256_matches'] and x['document_id']==did and
                          x['used_in_answer'] and
                          (not pages or x['page'] in pages) for x in citations) for did,pages in expected_pages.items())
            coverage=all(values.values()) and all(terms.values()) and all(alternatives.values())
            matched=not error and result.get('answer_status')==ref['status'] and coverage and source_hit
            item={'id':case['id'],'question':case['question'],'category':case['category'],'http_status':status,'error':error,
                  'duration_ms':round((time.perf_counter()-started)*1000,2),'necessary_checks_passed':matched,
                  'reference_values':values,'reference_terms':terms,'reference_alternatives':alternatives,
                  'citation_check':'actual_answer_citations_only',
                  'expected_source_located':source_hit,'citations':citations,'actual':result}
            report['results'].append(item);report['requests'][case['id']]['status']='finished';write()
            print(json.dumps({'id':case['id'],'necessary_checks_passed':matched,'answer_status':result.get('answer_status'),
                              'duration_ms':item['duration_ms'],'error':error},ensure_ascii=False),flush=True)
        report.update(phase='completed',finished_at=now())
        report['summary']={'cases':len(report['results']),'necessary_checks_passed':sum(x['necessary_checks_passed'] for x in report['results']),
                           'expected_source_located':sum(x['expected_source_located'] for x in report['results']),
                           'http_failures':sum(x['http_status']!=200 for x in report['results']),
                           'nonempty_answers':sum(bool(x['actual'].get('answer')) for x in report['results']),
                           'calls':sum(x['actual'].get('agent',{}).get('calls',0) for x in report['results']),
                           'independent_semantic_accuracy':'pending'}
        write(); print(json.dumps(report['summary'],ensure_ascii=False),flush=True)
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',type=Path,default=FIRST);parser.add_argument('--regression',action='store_true');parser.add_argument('--case',action='append')
    args=parser.parse_args()
    if args.execute:execute(args.output,args.regression,args.case)
    else:print(json.dumps({'dataset_sha256':digest(DATASET),'cases':len(json.loads(DATASET.read_text())['cases']),'model_calls':0},ensure_ascii=False))
