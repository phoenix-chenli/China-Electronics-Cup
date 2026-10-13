"""Real cross-source diagnostics with an explicit output contract.

Default sends no model request. References never enter a query payload.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import time
import httpx

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'03_测试验收/测试样本/明确输出合同跨源验证集-20261013.json'
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def rows_match(result,expected):
    actual=[list(row.values()) for row in result.get('rows',[])]
    def equal(a,b):
        if type(a) in (int,float) and type(b) in (int,float):return math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-7)
        return a==b
    return len(actual)==len(expected) and all(len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b)) for a,b in zip(actual,expected))

def execute(output,ids=None,dataset=DATA):
    if sha(dataset)!=dataset.with_suffix('.sha256').read_text().split()[0]:raise ValueError('题集冻结摘要不符')
    if output.exists():raise FileExistsError('报告已存在，未重复调用模型')
    data=json.loads(dataset.read_text())
    if sha(ROOT/'02_数据资料/公开统计分析/指标核验.json')!=data['indicators_sha256']:raise ValueError('原始指标资料偏离参考')
    selected=[case for case in data['cases'] if not ids or case['id'] in ids]
    if ids and set(ids)-{case['id'] for case in data['cases']}:raise ValueError('未知题号')
    report={'phase':'running','started_at':now(),'dataset_sha256':sha(dataset),'workers':1,
        'reference_answers_sent_to_model':False,'human_review':'pending','results':[],
        'source_sha256':{name:sha(ROOT/'01_程序代码/backend'/name) for name in
                         ['agent.py','connections.py','database_query.py','database_answer.py','sql_guard.py','rag_grounding.py','rag_retrieval.py']},
        'boundary':data['boundary']}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    def write():output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    with httpx.Client(base_url='http://127.0.0.1:8765',timeout=630,trust_env=False) as client:
        health=client.get('/api/health').json();loaded=health.get('backend_source_at_startup',{})
        if not loaded or any(sha(ROOT/'01_程序代码/backend'/name)!=digest for name,digest in loaded.items()):
            raise ValueError('当前服务未加载最新源码，未发送模型请求；请先重启当前目录服务')
        report['runtime_source_attestation']={'instance':health['instance'],'matching_source_files':len(loaded)}
        config=client.get('/api/agent/config').json()
        if config.get('mode')!='model' or not config.get('available'):raise ValueError('主模型未就绪')
        model=next(x for x in client.get('/api/connections').json()['items'] if x['id']==config['connection_id'])
        report['requested_model']=model['model'];report['endpoint']=model['endpoint'];write()
        consecutive_limits=0
        for case in selected:
            hashes={doc['document_id']:doc['sha256'] for doc in case['documents']}
            for did,expected in hashes.items():
                view=client.get('/api/documents/'+did+'/view');view.raise_for_status()
                if view.json()['document']['sha256']!=expected:raise ValueError('原件版本不符')
            request={'project_id':data['project_id'],'scope_override':'hybrid','question':case['question'],
                     'rag_scope':{'scope_type':'corpus' if len(hashes)>1 else 'document','document_ids':list(hashes)}}
            started=time.perf_counter();error=None;result={};status=None
            try:
                response=client.post('/api/query',json=request);status=response.status_code;result=response.json()
                if status!=200:error=result.get('detail','本机请求失败')
            except (httpx.HTTPError,ValueError) as exc:error=type(exc).__name__
            cited={eid for claim in result.get('claims',[]) for eid in claim.get('evidence_ids',[])}
            checks=[]
            for evidence in result.get('evidence',[]):
                if evidence['id'] not in cited or evidence.get('type')!='document':continue
                location=client.post('/api/evidence/resolve',json={'locator':evidence['locator'],'quote':evidence['snippet']})
                body=location.json();loc=evidence['locator'];did=loc.get('document_id')
                checks.append({'evidence_id':evidence['id'],'document_id':did,'page':loc.get('page'),
                    'located':location.status_code==200 and body.get('status')=='located',
                    'sha256_matches':hashes.get(did)==loc.get('sha256')})
            source_ok=all(any(x['located'] and x['sha256_matches'] and x['document_id']==did and x['page'] in pages
                             for x in checks) for did,pages in case['expected_source_pages'].items())
            text=result.get('answer','');terms={word:word in text for word in case['required_terms']}
            sql_ok=rows_match(result,case['reference_rows'])
            passed=not error and result.get('answer_status')=='supported' and sql_ok and source_ok and all(terms.values())
            row={'id':case['id'],'category':case['category'],'question':case['question'],'http_status':status,'error':error,
                'duration_ms':round((time.perf_counter()-started)*1000,2),'strict_sql_rows_match':sql_ok,
                'actual_answer_citations_located':source_ok,'terms':terms,'necessary_checks_passed':passed,
                'reference_sql':case['reference_sql'],'reference_rows':case['reference_rows'],'citations':checks,'actual':result}
            report['results'].append(row);write();print(json.dumps({k:row[k] for k in
                ['id','necessary_checks_passed','strict_sql_rows_match','actual_answer_citations_located','error']},ensure_ascii=False),flush=True)
            consecutive_limits=consecutive_limits+1 if status==429 or 'HTTP 429' in str(error) else 0
            if consecutive_limits>=2:
                report['stopped_reason']='连续限流；余题未提交，已有POST未重放';break
        report.update(phase='completed' if len(report['results'])==len(selected) else 'stopped',finished_at=now())
        report['summary']={'cases':len(report['results']),'necessary_checks_passed':sum(x['necessary_checks_passed'] for x in report['results']),
            'strict_sql_matches':sum(x['strict_sql_rows_match'] for x in report['results']),
            'actual_answer_sources_located':sum(x['actual_answer_citations_located'] for x in report['results']),
            'technical_failures':sum(bool(x['error']) for x in report['results']),'independent_semantic_accuracy':'pending'}
        write();print(json.dumps(report['summary'],ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--case',action='append')
    parser.add_argument('--dataset',type=Path,default=DATA)
    parser.add_argument('--output',type=Path,default=ROOT/'03_测试验收/验收证据/明确输出合同跨源TokenRhythm首测-20261013.json')
    args=parser.parse_args()
    if args.execute:execute(args.output.resolve(),args.case,args.dataset.resolve())
    else:print(json.dumps({'dataset_sha256':sha(DATA),'cases':len(json.loads(DATA.read_text())['cases']),'model_calls':0}))
