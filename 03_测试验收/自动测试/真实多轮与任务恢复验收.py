"""Five real readonly turns, idempotency, cancellation and optional owned restart.

Default only inspects the frozen dataset. --execute spends model tokens.
--restart-service explicitly exercises the workspace's own launcher.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[2]
DATA=ROOT/'03_测试验收/测试样本/Chinook连续五轮验证集-20261013.json'

def now():return datetime.now(timezone.utc).isoformat()
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def same_rows(actual,expected):
    rows=[list(x.values()) for x in actual.get('rows',[])]
    def equal(a,b):
        if isinstance(a,(int,float)) and isinstance(b,(int,float)):
            return math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-7)
        return a==b
    return len(rows)==len(expected) and all(len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b)) for a,b in zip(rows,expected))

def execute(output,restart=False):
    if sha(DATA)!=DATA.with_suffix('.sha256').read_text().split()[0]:raise ValueError('题集冻结摘要不符')
    if output.exists():raise FileExistsError('报告已存在，未重放请求')
    data=json.loads(DATA.read_text());report={'started_at':now(),'phase':'running','dataset_sha256':sha(DATA),
        'reference_answers_sent_to_model':False,'human_review':'pending',
        'turns':[],'boundary':data['boundary'],'upstream_posts_automatically_replayed':False}
    if sha(ROOT/'02_数据资料/公开基准/Chinook/Chinook_Sqlite.sqlite')!=data['source_sha256']:
        raise ValueError('原始数据库摘要与冻结参考不同')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def write():output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    with httpx.Client(base_url='http://127.0.0.1:8765',timeout=45,trust_env=False) as client:
        def get(path):r=client.get(path);r.raise_for_status();return r.json()
        def post(path,payload):r=client.post(path,json=payload);r.raise_for_status();return r.json()
        config=get('/api/agent/config')
        model=next(x for x in get('/api/connections')['items'] if x['id']==config.get('connection_id'))
        if config.get('mode')!='model' or not config.get('available'):raise ValueError('主模型未就绪')
        report['requested_model']=model['model'];report['endpoint']=model['endpoint']
        def wait(tid,seconds=240):
            until=time.monotonic()+seconds
            while time.monotonic()<until:
                task=get('/api/tasks/'+tid)
                if task['status'] not in {'queued','running','cancelling'}:return task
                time.sleep(.2)
            raise TimeoutError('任务未在验收时限内结束；不会自动重新提交')
        session=post('/api/projects/'+data['project_id']+'/sessions',{})
        sid=session['id'];report['session_id']=sid
        title='TokenRhythm · 条件继承与恢复验收' if '回归' in output.name else 'TokenRhythm · 连续五轮与恢复验收'
        client.patch('/api/sessions/'+sid,json={'title':title}).raise_for_status()
        parent=None
        for case in data['cases']:
            payload={'session_id':sid,'question':case['question'],'scope_override':'database',
                     'request_id':str(uuid.uuid4()),'parent_message_id':parent,'inherit_context':True}
            start=time.perf_counter();task=post('/api/tasks',payload);task=wait(task['id'])
            result=task.get('result') or {};session=get('/api/sessions/'+sid);parent=session.get('selected_message_id')
            match=task['status']=='completed' and result.get('answer_status')=='supported' and same_rows(result,case['reference_rows'])
            report['turns'].append({'id':case['id'],'question':case['question'],'task_id':task['id'],
                'duration_ms':round((time.perf_counter()-start)*1000,2),'status':task['status'],
                'strict_rows_match':match,'reference_rows':case['reference_rows'],'actual':result})
            if case['id']=='turn-1':
                repeated=post('/api/tasks',payload)
                report['idempotency']={'same_request_id_same_task':repeated['id']==task['id'],'task_id':task['id']}
            write();print(json.dumps({'turn':case['id'],'strict_rows_match':match,'status':task['status']},ensure_ascii=False),flush=True)
        selected_before=parent;case=data['cases'][3]
        task=post('/api/tasks',{'session_id':sid,'question':case['question'],'scope_override':'database',
                  'request_id':str(uuid.uuid4()),'parent_message_id':parent,'inherit_context':True})
        until=time.monotonic()+30;seen=None
        while time.monotonic()<until:
            seen=get('/api/tasks/'+task['id'])
            if any(e.get('tool')=='model.decide' and e.get('status')=='completed' for e in seen.get('events',[])):break
            if seen['status'] not in {'queued','running'}:break
            time.sleep(.15)
        if seen['status'] in {'queued','running'}:
            start=time.perf_counter();post('/api/tasks/'+task['id']+'/cancel',{});cancelled=wait(task['id'],30)
            report['cancel']={'status':cancelled['status'],'duration_ms':round((time.perf_counter()-start)*1000,2),
                'real_model_step_completed_before_cancel':any(e.get('tool')=='model.decide' and e.get('status')=='completed' for e in seen.get('events',[])),
                'late_result_published':cancelled.get('result') is not None,'task_id':task['id'],
                'previous_selection_retained':get('/api/sessions/'+sid).get('selected_message_id')==selected_before}
        else:report['cancel']={'status':'completed_before_cancel','task_id':task['id']}
        write()
        if restart:
            before=get('/api/health')['instance'];launcher=ROOT/'01_程序代码/启动与打包/launcher.py'
            env={**os.environ,'EVIDENCEFLOW_NO_BROWSER':'1'}
            for command in ['stop','start']:
                subprocess.run([sys.executable,'-B','-X','utf8',str(launcher),command],cwd=ROOT,env=env,check=True)
            after=get('/api/health')['instance'];restored=get('/api/sessions/'+sid)
            report['restart']={'workspace_same':before['workspace']==after['workspace'],
                'instance_changed':before['id']!=after['id'],'selected_result_retained':restored.get('selected_message_id')==selected_before,
                'completed_turns_retained':sum(m['role']=='assistant' and m.get('result') is not None for m in restored['messages'])>=5,
                'cancelled_task_restored':get('/api/tasks/'+task['id'])['status']=='cancelled'}
        if report.get('cancel',{}).get('status')=='cancelled':
            # This is the explicit recovery step, a new task with a fresh ID.
            retry=post('/api/tasks/'+task['id']+'/retry',{'request_id':str(uuid.uuid4())});retry=wait(retry['id'])
            report['explicit_retry']={'status':retry['status'],'new_task_id':retry['id'],'attempt_of':retry.get('attempt_of'),
                'strict_rows_match':same_rows(retry.get('result') or {},case['reference_rows']),'actual':retry.get('result')}
        report.update(phase='completed',finished_at=now(),summary={'turns':5,
            'strict_rows_matches':sum(x['strict_rows_match'] for x in report['turns']),
            'semantic_accuracy':'human_pending','cancel':report.get('cancel',{}).get('status')})
        write();print(json.dumps(report['summary'],ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--restart-service',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'03_测试验收/验收证据/TokenRhythm连续五轮取消与恢复-20261013.json')
    args=parser.parse_args()
    if args.execute:execute(args.output.resolve(),args.restart_service)
    else:print(json.dumps({'dataset_sha256':sha(DATA),'turns':5,'model_calls':0}))
