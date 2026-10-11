"""One fresh run against the public project. References never enter model input."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
DATASET=ROOT/'03_测试验收/测试样本/公开统计新验证集-20261011.json'
FIRST=ROOT/'03_测试验收/验收证据/公开统计新验证首测-20261011.json'
EXPECTED_SHA='2513698355fd3beb2918c413aae7b8d80c3be6544fcea48d3aee450fa6abe714'
BASE='http://127.0.0.1:8765'

def now(): return datetime.now(timezone.utc).isoformat()
def api(path, payload=None):
    request=urllib.request.Request(BASE+path,
        data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=630) as response: return json.load(response)

def percentile(values, p):
    if not values: return None
    values=sorted(values); index=(len(values)-1)*p; low=int(index)
    return round(values[low]+(values[min(low+1,len(values)-1)]-values[low])*(index-low),2)

def equal(a,b,tolerance):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,abs_tol=tolerance,rel_tol=1e-8)
    return a==b

def strict_rows(result, reference):
    expected=reference.get('expected_rows')
    if not reference.get('sql'): return None
    actual=[list(row.values()) for row in result.get('rows',[])]
    tolerance=reference.get('numeric_tolerance',1e-7)
    same=lambda a,b: len(a)==len(b) and all(equal(x,y,tolerance) for x,y in zip(a,b))
    if len(actual)!=len(expected): return False
    if reference.get('order_matters',False): return all(same(a,b) for a,b in zip(actual,expected))
    remaining=list(actual)
    for expected_row in expected:
        match=next((i for i,row in enumerate(remaining) if same(row,expected_row)),None)
        if match is None: return False
        remaining.pop(match)
    return True

def execute(output,regression=False,case_ids=None):
    raw=DATASET.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=EXPECTED_SHA: raise ValueError('题集与冻结摘要不一致')
    dataset=json.loads(raw)
    selected=dataset['cases']
    if case_ids:
        if not regression:raise ValueError('首测必须执行完整题集；题目筛选只用于已见题回归')
        unknown=set(case_ids)-{case['id'] for case in selected}
        if unknown:raise ValueError('筛选包含未知题目：'+','.join(sorted(unknown)))
        selected=[case for case in selected if case['id'] in set(case_ids)]
    if not regression and (FIRST.exists() or Path(output).resolve() != FIRST.resolve()):
        raise ValueError('首测已经封存；再次执行必须指定--regression并使用新的输出路径')
    for source in dataset['source_snapshot']['original_pdfs']:
        if hashlib.sha256((ROOT/source['path']).read_bytes()).hexdigest()!=source['sha256']:
            raise ValueError('官方原件偏离冻结版本')
    indicator=dataset['source_snapshot']
    if hashlib.sha256((ROOT/indicator['indicators_path']).read_bytes()).hexdigest()!=indicator['indicators_sha256']:
        raise ValueError('指标偏离冻结版本')
    health=api('/api/health'); profile=api('/api/projects/public-statistics/query-context')
    if health.get('agent_mode')!='model' or not profile.get('database_connection_id'):
        raise ValueError('真实模型或公开项目未就绪')
    output=Path(output).resolve(); output.parent.mkdir(parents=True,exist_ok=True)
    configured=[c for c in profile.get('connections',[]) if c['id']==profile.get('connection_id')]
    model=profile.get('model') or (configured[0].get('model') if len(configured)==1 else None)
    report={'dataset_id':dataset['dataset_id'],'dataset_sha256':EXPECTED_SHA,'run_kind':'regression' if regression else 'first_exposure',
        'frozen_at':dataset['frozen_at'],'started_at':now(),'phase':'running','model':model,
        'source_sha256':{n:hashlib.sha256((ROOT/'01_程序代码/backend'/n).read_bytes()).hexdigest() for n in ['agent.py','connections.py','database_query.py','sql_guard.py','rag_grounding.py','rag_retrieval.py','public_statistics.py','query_routing.py']},
        'project_id':'public-statistics','workers':2,'human_review':'pending',
        'selected_case_ids':[case['id'] for case in selected],
        'no_post_retries':True,'reference_answers_sent_to_model':False,
        'boundary':'SQL严格行匹配与引用定位是自动诊断，不能替代答全、口径和引用语义的真人复核。',
        'requests':{},'results':[]}
    # Exclusive creation prevents silently replaying any paid request.
    with output.open('x',encoding='utf-8') as f: json.dump(report,f,ensure_ascii=False,indent=2)
    lock=threading.Lock()
    def write():
        temp=output.with_suffix(output.suffix+'.pending')
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temp.replace(output)
    def run(case):
        payload={'question':case['question'],'project_id':dataset['project_id'],'scope_override':case['scope']}
        with lock:
            report['requests'][case['id']]={'started_at':now(),'status':'sent_once','payload_fields':list(payload)}
            write()
        started=time.perf_counter(); result={}; error=None; http_status=200
        try: result=api('/api/query',payload)
        except urllib.error.HTTPError as exc:
            http_status=exc.code
            try: result=json.load(exc)
            except Exception: result={}
            error=result.get('detail') or f'HTTP {exc.code}'
        except Exception as exc: error=f'{type(exc).__name__}: {exc}'
        elapsed=round((time.perf_counter()-started)*1000,2)
        citations=[]
        for evidence in result.get('evidence',[]):
            if evidence.get('type')!='document':continue
            resolved={}; location_error=None
            try: resolved=api('/api/evidence/resolve',{'locator':evidence.get('locator',{}),'quote':evidence.get('snippet','')})
            except Exception as exc: location_error=str(exc)
            citations.append({'evidence_id':evidence['id'],'source':evidence.get('source'),
                'located':resolved.get('status')=='located','locator':resolved.get('locator'),
                'error':location_error,'semantic_support':'human_pending'})
        expected=case['reference']['status']
        state=(result.get('status')=='needs_clarification' if expected=='needs_clarification' else
               result.get('answer_status')=='supported' if expected=='supported' else
               result.get('answer_status') in {'refused','insufficient_evidence','partial'})
        row={'case_id':case['id'],'category':case['category'],'scope':case['scope'],'question':case['question'],
            'http_status':http_status,'error':error,'duration_ms':elapsed,
            'model_calls':result.get('agent',{}).get('calls',0),'usage':result.get('agent',{}).get('usage',{}),
            'nonempty_answer':bool(str(result.get('answer','')).strip()),'expected_status':expected,
            'expected_state_match':bool(state and not error),'strict_sql_rows_match':strict_rows(result,case['reference']) if not error else False if case['reference'].get('sql') else None,
            'citation_location_checks':citations,'result':result,'human_review':'pending'}
        with lock:
            report['results'].append(row); report['requests'][case['id']]['status']='failed' if error else 'returned'
            write()
        print(json.dumps({k:row[k] for k in ['case_id','http_status','duration_ms','model_calls','nonempty_answer','expected_state_match','strict_sql_rows_match']},ensure_ascii=False),flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run,c) for c in selected]): future.result()
    rows=report['results']; rows.sort(key=lambda row:row['case_id'])
    checks=[r['strict_sql_rows_match'] for r in rows if r['strict_sql_rows_match'] is not None]
    citations=[c for r in rows for c in r['citation_location_checks']]
    report['metrics']={'executed_questions':len(rows),'technical_failures':sum(bool(r['error']) for r in rows),
        'nonempty_answers':sum(r['nonempty_answer'] for r in rows),'expected_state_matches':sum(r['expected_state_match'] for r in rows),
        'strict_sql_rows':{'matches':sum(checks),'denominator':len(checks)},
        'citation_locations':{'located':sum(c['located'] for c in citations),'denominator':len(citations)},
        'latency_ms':{'p50':percentile([r['duration_ms'] for r in rows],.5),'p95':percentile([r['duration_ms'] for r in rows],.95)},
        'model_calls':sum(r['model_calls'] for r in rows),'semantic_accuracy':'human_pending'}
    report.update(phase='completed',finished_at=now());write()
    print(json.dumps(report['metrics'],ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output');parser.add_argument('--regression',action='store_true')
    parser.add_argument('--case',dest='case_ids',action='append',help='已见题回归的题目ID，可重复指定');args=parser.parse_args()
    if args.execute:
        if not args.output:parser.error('--execute需要指定新的输出路径')
        execute(args.output,args.regression,args.case_ids)
    else:
        raw=DATASET.read_bytes();print(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(),'frozen_hash_matches':hashlib.sha256(raw).hexdigest()==EXPECTED_SHA,'model_calls':0},ensure_ascii=False))
