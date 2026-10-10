"""Frozen, isolated quality diagnostics. --execute reads credentials only from stdin.

No model calls or personal runtime access occur in validation mode. Development
and holdout results are separate; exact SQL/text checks never count as human review.
"""
from __future__ import annotations
import argparse,hashlib,importlib.util,json,math,os,shutil,signal,sqlite3,sys,tempfile,time
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
DEFAULT=ROOT/'03_测试验收/测试样本/优化评估集-20261010.json'


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path):
    path=Path(path).resolve();raw=path.read_bytes();expected=path.with_suffix('.sha256').read_text().split()[0]
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('评估集已偏离冻结摘要')
    data=json.loads(raw);texts={};ids=[]
    if not data.get('synthetic') or not data.get('frozen_before_model_execution'):raise ValueError('只能执行已冻结的公开合成评估集')
    for source in data['documents']:
        if source.get('format')=='scan':
            p=(ROOT/source['fixture_path']).resolve()
            if not p.is_relative_to(ROOT/'03_测试验收/输入样本') or digest(p)!=source['sha256']:raise ValueError('扫描夹具位置或摘要不符')
            texts[source['file']]=source['reference_text']
        else:
            texts[source['file']]=source['text']
            if hashlib.sha256(source['text'].encode()).hexdigest()!=source['sha256']:raise ValueError('原文摘要不符')
    sql=data['database']['schema_and_seed_sql']
    if hashlib.sha256(sql.encode()).hexdigest()!=data['database']['sha256']:raise ValueError('数据库夹具摘要不符')
    with sqlite3.connect(':memory:') as db:
        db.executescript(sql)
        for case in data['cases']:
            ids.append(case['id'])
            if case['split'] not in {'development','holdout'}:raise ValueError('分集不正确')
            for item in case.get('turns',[case]):
                ref=item['reference']
                if ref.get('sql'):
                    if not ref['sql'].lstrip().upper().startswith(('SELECT','WITH')):raise ValueError('参考查询必须只读')
                    rows=[list(x) for x in db.execute(ref['sql']).fetchall()]
                    if rows!=ref['expected_rows']:raise ValueError('独立SQL与冻结结果不一致：'+case['id'])
                for requirement in ref.get('requirements',[]):
                    if requirement['quote'] not in texts.get(requirement['source'],''):raise ValueError('参考引用不在原件中')
    if len(ids)!=len(set(ids)):raise ValueError('重复用例ID')
    return data,expected,texts


def equal(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,rel_tol=1e-9,abs_tol=.001)
    return a==b

def rows_match(actual,expected):
    actual=[list(x.values()) for x in actual]
    return len(actual)==len(expected) and all(len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b)) for a,b in zip(actual,expected))

def percentile(values,p):
    if not values:return None
    v=sorted(values);pos=(len(v)-1)*p;i=int(pos);f=pos-i
    return round(v[i]+f*(v[min(i+1,len(v)-1)]-v[i]),2)


def metrics(rows):
    sql=[r for r in rows if r.get('sql_rows_match') is not None]
    answer=[r for r in rows if r.get('reference_atom_checks')]
    citations=[x for r in rows for x in r.get('citation_location_checks',[])]
    refusal=[r for r in rows if r['expected_status'] in {'refused','needs_clarification_or_refusal'}]
    supported=[r for r in rows if r['expected_status']=='supported']
    return {'executed_questions':len(rows),'execution_errors':sum(r['execution_status']=='failed' for r in rows),
      'sql_rows':{'matches':sum(r['sql_rows_match'] for r in sql),'denominator':len(sql)},
      'reference_atoms':{'present':sum(x['present_in_answer'] for r in answer for x in r['reference_atom_checks']),
                         'denominator':sum(len(r['reference_atom_checks']) for r in answer)},
      'citations':{'located':sum(x['located'] for x in citations),'denominator':len(citations),'semantic_support':'human_pending'},
      'expected_refusal':{'matching':sum(r['expected_state_match'] for r in refusal),'denominator':len(refusal)},
      'supported_cases_refused_or_clarified':{'count':sum(r.get('answer_status')=='refused' or r.get('status')=='needs_clarification' for r in supported),'denominator':len(supported)},
      'expected_state_matches':sum(r.get('expected_state_match',False) for r in rows),
      'latency_ms':{'p50':percentile([r['duration_ms'] for r in rows],.5),'p95':percentile([r['duration_ms'] for r in rows],.95),'includes_errors':True},
      'model_calls':sum(r['model_calls'] for r in rows),'model_tokens':sum(r['usage'].get('total_tokens',0) for r in rows),
      'boundary':'结果匹配、精确文字与定位是自动诊断；引用语义、业务口径、答全及拒答合理性仍保留人工审核，不提供总准确率。'}


def execute(args,data,sha,texts):
    if not args.output:raise ValueError('--execute 必须指定 --output')
    chosen=[x for x in data['cases'] if x['split']==args.split]
    if args.case_ids:
        wanted=set(args.case_ids.split(','));chosen=[x for x in chosen if x['id'] in wanted]
        if wanted!={x['id'] for x in chosen}:raise ValueError('指定编号不属于当前分集')
    if args.limit:chosen=chosen[:args.limit]
    out=Path(args.output).resolve();out.parent.mkdir(parents=True,exist_ok=True)
    os.umask(0o077)
    # Reserve the checkpoint before reading a secret or making a paid request.
    # A later invocation must use a different output instead of replaying a run.
    with out.open('x') as checkpoint:
        json.dump({'dataset_sha256':sha,'split':args.split,'phase':'preparing'},checkpoint)
    credential={};work=None
    previous_signal=signal.getsignal(signal.SIGTERM)
    def terminate(signum,frame):raise KeyboardInterrupt('评估进程收到停止信号')
    signal.signal(signal.SIGTERM,terminate)
    try:
        parsed=json.load(sys.stdin)
        if not isinstance(parsed,dict):raise ValueError('评估连接字段不正确')
        credential=parsed
        if set(credential)!={'endpoint','model','secret'} or not all(isinstance(v,str) and v for v in credential.values()):
            raise ValueError('评估连接字段不正确')
        work=Path(tempfile.mkdtemp(prefix='evidenceflow-quality-'))
        return _execute(args,data,sha,texts,credential,work,chosen,out)
    except BaseException as exc:
        report=json.loads(out.read_text())
        report.update(phase='cancelled' if isinstance(exc,KeyboardInterrupt) else 'failed',
                      exception_type=type(exc).__name__,finished_at=datetime.now(timezone.utc).isoformat())
        temporary=out.with_suffix(out.suffix+'.pending')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');temporary.replace(out)
        raise
    finally:
        signal.signal(signal.SIGTERM,previous_signal)
        credential.clear()
        if work is not None:
            # This database belongs exclusively to the isolated public run.
            # Also cover setup, OCR and cancellation failures before the loop.
            try:
                app_db=work/'app.db'
                if app_db.is_file():
                    with sqlite3.connect(app_db) as db:
                        db.execute('PRAGMA secure_delete=ON')
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ef_connections'").fetchone():
                            db.execute('DELETE FROM ef_connections WHERE secret IS NOT NULL')
                        db.commit()
                        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            finally:
                (work/'.private/connections.key').unlink(missing_ok=True)


def _execute(args,data,sha,texts,credential,work,chosen,out):
    os.environ['EVIDENCEFLOW_DATA_DIR']=str(work)
    os.environ['EVIDENCEFLOW_RAG_MODEL_DIR']=str(ROOT/'02_数据资料/运行数据/models')
    sys.path.insert(0,str(ROOT/'01_程序代码'))
    from backend import main,agent,connections as c,settings,store as s,documents,document_tasks,rag_providers,rag_index
    main.init_app()
    settings.privacy(settings.Privacy(local_only=False))
    model=c.create(c.Config(name='公开评估文本模型',kind='text',endpoint=credential['endpoint'],model=credential['model'],timeout=120,secret=credential['secret']))
    credential.clear()
    tested=c.test(model['id'])
    if not tested['last_test']['ok']:raise ValueError('真实模型探针失败：'+tested['last_test']['detail'])
    c.enabled(model['id'],c.Enabled(enabled=True))
    dbpath=work/'检修公开评估.sqlite'
    with sqlite3.connect(dbpath) as db:db.executescript(data['database']['schema_and_seed_sql'])
    baseline_db_hash=digest(dbpath)
    database=c.create(c.Config(name='公开合成检修库',kind='sqlite',path=str(dbpath)))
    if not c.test(database['id'])['last_test']['ok']:raise ValueError('只读数据库探针失败')
    c.enabled(database['id'],c.Enabled(enabled=True))
    selected={};processing=[]
    for source in data['documents']:
        path=s.UPLOADS/source['file']
        if source.get('format')=='scan':shutil.copy2(ROOT/source['fixture_path'],path)
        else:path.write_text(source['text'])
        doc=s.index_document(path,name=source['file']);selected[source['file']]=doc['id']
        if source.get('format')=='scan':
            task=document_tasks.manager.submit(document_tasks.ParseRequest(document_id=doc['id'],version=doc['sha256'],pages=[1],mode='ocr'))
            while task['status'] in {'queued','running','cancelling'}:
                time.sleep(.2);task=document_tasks.manager.get(task['id'])
            processing.append({'source':source['file'],'task':task})
            if task['status']!='completed':raise ValueError('公开扫描资料未完成解析')
    index_job=rag_index.submit_index(list(selected.values()),synchronous=True)
    index_status=rag_index.status()
    if index_job['status']!='completed' or any(x['vector_status']!='ready' for x in index_status['indexes'] if x['document_id'] in selected.values()):
        raise ValueError('公开评估资料的本机向量索引尚未就绪')
    spec=importlib.util.spec_from_file_location('rag_eval_helpers',Path(__file__).with_name('评估RAG.py'))
    legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
    report={'dataset_id':data['dataset_id'],'dataset_sha256':sha,'split':args.split,'started_at':datetime.now(timezone.utc).isoformat(),
      'model':model['model'],'endpoint':model['endpoint'],'isolated_public_data':True,'data_directory':str(work),
      'phase':'running','providers':rag_providers.capabilities(),'scan_processing':processing,
      'index_preparation':{'job':index_job,'status':index_status,'comparison_note':'原开发基线为关键词＋本机重排；本轮加入显式本机向量索引准备。'},
      'source_sha256':{name:digest(ROOT/name) for name in ['01_程序代码/backend/agent.py','01_程序代码/backend/connections.py','01_程序代码/backend/rag_grounding.py','01_程序代码/backend/rag_index.py','01_程序代码/backend/rag_retrieval.py','01_程序代码/backend/database_query.py','01_程序代码/backend/sql_guard.py','01_程序代码/backend/formula_source.py','01_程序代码/backend/documents.py','01_程序代码/backend/document_quality.py']},
      'results':[],'human_review':'pending','limits':{'model_calls':args.max_model_calls,'model_tokens':args.max_tokens},
      'connection_probe_calls':1,'budget_note':'统计不含一次连接探针；每题开始前预留最多 12 次调用。token 是已报告用量的停止阈值，最后一题可能越过阈值。'}
    report['requested_groups']=len(chosen)
    def write():
        report['metrics']=metrics(report['results']);report['database_unchanged']=digest(dbpath)==baseline_db_hash
        temporary=out.with_suffix(out.suffix+'.pending')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        temporary.replace(out)
    def run_case(case,item,turn,previous):
        if report.get('metrics',{}).get('model_calls',0)+12>args.max_model_calls or report.get('metrics',{}).get('model_tokens',0)>=args.max_tokens:
            report['stopped_reason']='budget';return None
        scope=case['scope']
        agent.configure(agent.Configuration(mode='model',connection_id=model['id'],query_scope=scope,database_connection_id=database['id'] if scope in {'database','hybrid'} else None))
        context=agent.query_context(previous,requested_scope={'scope_type':'corpus','document_ids':list(selected.values())} if scope in {'knowledge','hybrid'} else None)
        started=time.perf_counter();error=None
        try:result=agent.execute(item['question'],previous=previous,persist=False,database_context=context)
        except agent.AgentFailure as exc:result=exc.result;error=str(exc)
        except Exception as exc:result={};error=str(exc)
        ref=item['reference'];status=result.get('answer_status');claims=result.get('claims',[])
        answer=result.get('answer','');state_match=status==ref['status']
        if ref['status']=='needs_clarification_or_refusal':state_match=not claims and (status=='refused' or result.get('status')=='needs_clarification')
        atoms=[{'source':r['source'],'atom':atom,'present_in_answer':legacy.normalized(atom) in legacy.normalized(answer)} for r in ref.get('requirements',[]) for atom in r.get('answer_atoms',[])]
        locations=[]
        for evidence in result.get('evidence',[]):
            if evidence.get('type')=='document':
                try:resolved=documents.resolve(evidence['locator'],evidence.get('snippet',''));ok=resolved['status']=='located'
                except Exception:ok=False
                locations.append({'evidence_id':evidence['id'],'source':evidence.get('source'),'located':ok})
        adapted={'id':case['id'],'question':item['question'],'expected_status':ref['status'],'required_sources':list(dict.fromkeys(r['source'] for r in ref.get('requirements',[]))),
                 'requirements':[{'id':str(i+1),'evidence':[{'source':r['source'],'quote':r['quote']}]} for i,r in enumerate(ref.get('requirements',[]))]}
        diagnostic=legacy.inspect_result(adapted,result,round((time.perf_counter()-started)*1000,2)) if scope in {'knowledge','hybrid'} else None
        row={'case_id':case['id'],'turn':turn,'category':case['category'],'question':item['question'],'expected_status':ref['status'],
             'status':result.get('status'),'answer_status':status,'execution_status':'failed' if error else 'returned','error':error,
             'duration_ms':round((time.perf_counter()-started)*1000,2),'model_calls':result.get('agent',{}).get('calls',0),'usage':result.get('agent',{}).get('usage',{}),
             'expected_state_match':bool(state_match and not error),'sql_rows_match':bool(not error and result.get('database_query') and rows_match(result.get('rows',[]),ref['expected_rows'])) if ref.get('sql') else None,
             'reference_atom_checks':atoms,'citation_location_checks':locations,'retrieval_diagnostic':diagnostic,'reference':ref,'result':result,'human_review':'pending'}
        report['results'].append(row);write()
        print(json.dumps({k:row[k] for k in ['case_id','turn','execution_status','error','duration_ms','sql_rows_match','expected_state_match']},ensure_ascii=False),flush=True)
        return result.get('query_spec') if not error else None
    write()
    for case in chosen:
        previous=None
        for turn,item in enumerate(case.get('turns',[case]),1):
            previous=run_case(case,item,turn,previous)
            if report.get('stopped_reason'):break
        if report.get('stopped_reason'):break
    report['phase']='budget_stopped' if report.get('stopped_reason') else 'completed'
    report['finished_at']=datetime.now(timezone.utc).isoformat();write()
    print(json.dumps({'output':str(out),'metrics':report['metrics'],'stopped_reason':report.get('stopped_reason')},ensure_ascii=False),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',default=str(DEFAULT));parser.add_argument('--execute',action='store_true')
    parser.add_argument('--split',choices=['development','holdout'],default='development')
    parser.add_argument('--output');parser.add_argument('--limit',type=int,default=0);parser.add_argument('--case-ids')
    parser.add_argument('--max-model-calls',type=int,default=240);parser.add_argument('--max-tokens',type=int,default=900000)
    args=parser.parse_args()
    if args.limit<0 or args.max_model_calls<1 or args.max_tokens<1:parser.error('预算和数量不正确')
    data,sha,texts=load(args.dataset)
    if args.execute:execute(args,data,sha,texts)
    else:print(json.dumps({'dataset':data['dataset_id'],'sha256':sha,'groups':len(data['cases']),'questions':sum(len(c.get('turns',[c])) for c in data['cases']),'splits':dict(Counter(c['split'] for c in data['cases'])),'validation':'passed','model_calls':0},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
