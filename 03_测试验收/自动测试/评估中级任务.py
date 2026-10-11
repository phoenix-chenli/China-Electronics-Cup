"""Run frozen synthetic schema-linking, grain and clarification diagnostics.

Default validates only. Execution reads a model connection from stdin; secrets
never belong in the dataset, arguments, report or production database.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[2]
DATASET=ROOT/'03_测试验收/测试样本/中级任务58表验证集-20261011.json'

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path=None):
    path=Path(path or DATASET).resolve();raw=path.read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    if path.with_suffix('.sha256').exists():
        expected=path.with_suffix('.sha256').read_text().split()[0]
    elif path==DATASET.resolve():
        expected='ae259ddeead5b11e4472d2df375d891281c833d646d267b124d044ea6377e044'
    else:raise ValueError('评估集缺少冻结摘要')
    if sha!=expected:raise ValueError('评估集偏离冻结摘要')
    data=json.loads(raw)
    if not data.get('synthetic'):raise ValueError('隔离评估仅接受合成公开夹具')
    if data.get('seed_sha256') and hashlib.sha256(data['seed_sql'].encode()).hexdigest()!=data['seed_sha256']:
        raise ValueError('数据库夹具偏离冻结摘要')
    ids=[x['id'] for x in data['cases']]
    if len(ids)!=len(set(ids)):raise ValueError('重复案例编号')
    with sqlite3.connect(':memory:') as db:
        db.executescript(data['seed_sql'])
        if db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]!=data['table_count']:
            raise ValueError('表数量与夹具不一致')
        for case in data['cases']:
            for turn in case.get('turns',[case]):
                if not turn.get('reference_sql'):continue
                if not turn['reference_sql'].lstrip().upper().startswith(('SELECT','WITH')):
                    raise ValueError('参考查询必须只读')
                cursor=db.execute(turn['reference_sql'])
                if [list(row) for row in cursor]!=turn['expected_rows']:
                    raise ValueError('独立SQL与冻结结果不一致：'+case['id'])
                if turn.get('expected_columns') and [x[0] for x in cursor.description]!=turn['expected_columns']:
                    raise ValueError('参考输出列不一致：'+case['id'])
    return data,sha


def check_result(result,case,error=None):
    expected=case.get('expected_status','supported')
    state=(result.get('status')=='needs_clarification' if expected=='needs_clarification' else
           result.get('answer_status')=='supported') and not error
    rows=None
    if case.get('reference_sql'):
        actual=[list(row.values()) for row in result.get('rows',[])];wanted=case['expected_rows']
        def same(a,b):
            if type(a) in (int,float) and type(b) in (int,float):
                return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-7)
            return type(a)==type(b) and a==b
        if case.get('expected_columns'):
            rows=(result.get('columns')==case['expected_columns'] and len(actual)==len(wanted)
                  and all(len(a)==len(b) and all(same(x,y) for x,y in zip(a,b)) for a,b in zip(actual,wanted)))
        else:rows=sorted(actual,key=str)==sorted(wanted,key=str)
        rows=bool(rows and not error)
    return {'expected_state_match':bool(state),'strict_sql_rows_match':rows,
            'nonempty_answer':bool(str(result.get('answer') or result.get('headline') or '').strip())}


def execute(out,data,*,dataset_sha=None,regression=True,case_ids=None):
    first=data.get('first_run_path')
    if first and not regression and (ROOT/first).exists():raise ValueError('首测已封存，请标记回归并使用新输出路径')
    if first and not regression and Path(out).resolve()!=(ROOT/first).resolve():
        raise ValueError('首测必须使用题集指定的封存路径')
    chosen=data['cases']
    if case_ids:
        if not regression:raise ValueError('首测必须完整执行；筛选仅用于回归')
        wanted=set(case_ids);chosen=[c for c in chosen if c['id'] in wanted]
        if wanted!={c['id'] for c in chosen}:raise ValueError('筛选包含未知案例')
    out=Path(out).resolve()
    report={'dataset_sha256':dataset_sha or digest(DATASET),'phase':'preparing','human_review':'pending',
            'run_kind':'regression' if regression else 'first_exposure',
            'dataset_id':data.get('dataset_id'),'frozen_at':data.get('frozen_at'),
            'selected_case_ids':[c['id'] for c in chosen],
            'reference_answers_sent_to_model':False,'no_post_retries':True,'results':[],
            'started_at':datetime.now(timezone.utc).isoformat(),'table_count':data['table_count'],
            'decoy_table_count':data['decoy_table_count'],'boundary':data['boundary']}
    with out.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    work=Path(tempfile.mkdtemp(prefix='evidenceflow-schema58-'));credential={}
    def write():
        tmp=out.with_suffix('.json.pending');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2));tmp.replace(out)
    try:
        credential=json.load(sys.stdin)
        if set(credential)!={'endpoint','model','secret'}:raise ValueError('连接字段不正确')
        os.environ['EVIDENCEFLOW_DATA_DIR']=str(work)
        os.environ['EVIDENCEFLOW_SKIP_PUBLIC_DEMO']='1'
        sys.path[:0]=[str(ROOT/'01_程序代码'),str(ROOT/'01_程序代码/启动与打包')]
        from runtime_bootstrap import activate
        activate()
        from backend import main,agent,connections as c,settings
        main.init_app();settings.privacy(settings.Privacy(local_only=False))
        model=c.create(c.Config(name='隔离58表评测模型',kind='text',timeout=120,**credential));credential.clear()
        if not c.test(model['id'])['last_test']['ok']:raise ValueError('模型连接未通过')
        c.enabled(model['id'],c.Enabled(enabled=True))
        dbpath=work/'synthetic.sqlite'
        with sqlite3.connect(dbpath) as db:db.executescript(data['seed_sql'])
        database=c.create(c.Config(name='隔离58表合成库',kind='sqlite',path=str(dbpath)))
        if not c.test(database['id'])['last_test']['ok']:raise ValueError('数据库连接未通过')
        c.enabled(database['id'],c.Enabled(enabled=True))
        agent.configure(agent.Configuration(mode='model',connection_id=model['id'],query_scope='database',database_connection_id=database['id']))
        before=digest(dbpath)
        report.update(phase='running',model=model['model'],connection_probe_calls=1,
            evaluator_sha256=digest(Path(__file__)),
            source_sha256={name:digest(ROOT/'01_程序代码/backend'/name) for name in ['agent.py','connections.py','database_query.py','sql_guard.py']})
        write()
        for case in chosen:
            previous={};turns=case.get('turns',[case])
            for index,turn in enumerate(turns):
                cid=case['id'] if len(turns)==1 else case['id']+'-turn-'+str(index+1)
                # Persist before the POST; observations never replay a sent request.
                report.setdefault('requests',{})[cid]={'status':'sent_once','question':turn['question']};write()
                start=time.perf_counter();error=None
                try:result=agent.execute(turn['question'],previous,persist=False)
                except agent.AgentFailure as exc:result=exc.result;error=str(exc)
                schema=[e for e in result.get('events',[]) if e['tool']=='database.schema' and e['status']=='completed']
                selected=sorted({t['name'] for e in schema for t in e.get('outputs',{}).get('tables',[])})
                decoys=set(data.get('decoy_tables',[]))
                excluded=not any(t.split('.')[-1] in decoys for t in selected) if decoys else not any('_' in t for t in selected)
                item={'case_id':cid,'question':turn['question'],'error':error,**check_result(result,turn,error),
                      'duration_ms':round((time.perf_counter()-start)*1000,2),'expected_status':turn.get('expected_status','supported'),
                      'model_calls':result.get('agent',{}).get('calls',0),'required_tables':turn['required_tables'],
                      'selected_tables':selected,'excluded_decoys':excluded,
                      'schema_stages':len(schema),'result':result,'human_review':'pending'}
                report['results'].append(item);report['requests'][cid]['status']='failed' if error else 'returned';write()
                previous=result.get('query_spec',{}) if not error else {}
                print(json.dumps({k:v for k,v in item.items() if k not in ['result','question']},ensure_ascii=False),flush=True)
        rows=report['results'];checks=[r['strict_sql_rows_match'] for r in rows if r['strict_sql_rows_match'] is not None]
        report.update(phase='completed',finished_at=datetime.now(timezone.utc).isoformat(),database_unchanged=digest(dbpath)==before,
            metrics={'executed_turns':len(rows),'technical_failures':sum(bool(r['error']) for r in rows),
                     'nonempty_answers':sum(r['nonempty_answer'] for r in rows),
                     'expected_state_matches':sum(r['expected_state_match'] for r in rows),
                     'strict_sql_rows':{'matches':sum(checks),'denominator':len(checks)},
                     'semantic_accuracy':'human_pending'})
        write()
    except BaseException as exc:
        report.update(phase='failed',exception_type=type(exc).__name__);write();raise
    finally:
        credential.clear()
        dbpath=work/'app.db'
        if dbpath.exists():
            with sqlite3.connect(dbpath) as db:
                db.execute('PRAGMA secure_delete=ON')
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='ef_connections'").fetchone():db.execute('DELETE FROM ef_connections WHERE secret IS NOT NULL')
                db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        (work/'.private/connections.key').unlink(missing_ok=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true');parser.add_argument('--output')
    parser.add_argument('--dataset',type=Path,default=DATASET);parser.add_argument('--regression',action='store_true')
    parser.add_argument('--case',action='append',dest='case_ids');args=parser.parse_args()
    data,sha=load(args.dataset)
    if args.execute:
        if not args.output:parser.error('--execute需指定新的输出路径')
        execute(args.output,data,dataset_sha=sha,regression=args.regression or not data.get('first_run_path'),case_ids=args.case_ids)
    else:
        print(json.dumps({'dataset_sha256':sha,'table_count':data['table_count'],'cases':len(data['cases']),'references_match':True,'model_calls':0},ensure_ascii=False))
