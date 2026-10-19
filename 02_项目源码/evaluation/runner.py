"""python -m evaluation.runner --help. Live, replay and fault evidence stay distinct."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
import os
import platform
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .core import load_suite, digest, code_digest, input_payload, assess, summarize

ROOT=Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def wait_answer_cleanup(answers, answer_id, timeout=30):
    """Cancellation becomes terminal before the worker finishes usage audit."""
    deadline = time.monotonic() + timeout
    while True:
        with answers.lock:
            if answer_id not in answers.events:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(.05, max(0, deadline - time.monotonic())))


def following_case_cooldown(record):
    """Delay a different, not-yet-submitted case; never resubmit this record."""
    delays = []
    for attempt in (record or {}).get('model_attempts', []):
        if not isinstance(attempt, dict):
            continue
        if attempt.get('http_status') == 429:
            delays.append(30.)
            delays.extend([attempt.get('provider_cooldown_seconds'),
                           (attempt.get('retry_policy') or {}).get('retry_after_seconds')])
        for key in ('circuit_before', 'circuit_after'):
            circuit = attempt.get(key) or {}
            if circuit.get('state') in ('open', 'half_open'):
                delays.append(circuit.get('retry_after_seconds'))
    valid = [float(v) for v in delays if type(v) in (int, float) and math.isfinite(v) and v > 0]
    return max(valid, default=0.)


def write_json(path,value,exclusive=False):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    if exclusive and path.exists():
        raise ValueError('输出已存在；每次试验必须使用新编号保留所有尝试')
    data=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    if exclusive:
        with path.open('x') as f:f.write(data)
    else:
        temporary=path.with_suffix(path.suffix+'.tmp')
        temporary.write_text(data);temporary.replace(path)


def register_fixture_provenance(store,source_id,manifest):
    """Record author-supplied fixture provenance before any answer is created.

    Upload currently carries bytes/name only. This setup step records known
    source clusters, never expected answers. A generic authorship description
    is not a shared originating document and must not merge unrelated sources.
    This is explicitly harness setup, not a claim of an upload UI capability.
    """
    source=store.get('sources',source_id)
    if not source or source['sha256']!=manifest['sha256']:
        raise ValueError('导入原件与冻结清单不一致')
    metadata=dict(source.get('metadata') or {})
    origin=manifest.get('origin','')
    metadata.update(source_group=manifest['cluster'],
                    origin=origin if origin.startswith(('https://','http://')) else 'evaluation-cluster:'+manifest['cluster'],
                    provenance_description=origin,evaluation_fixture_kind=manifest.get('kind','unspecified'),
                    provenance_setup='frozen_input_manifest_before_answers')
    with store.transaction() as connection:
        connection.execute('UPDATE sources SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),source_id))
    return dict(source_id=source_id,input_source_id=manifest['id'],sha256=manifest['sha256'],source_group=metadata['source_group'],origin=metadata['origin'])


def run(args):
    suite=load_suite(args.suite)
    runtime=args.runtime.resolve(); report=args.output.resolve()
    if runtime.exists() or report.exists() or report.is_relative_to(runtime):
        raise ValueError('运行目录和报告必须是新路径，报告位于运行目录外')
    config_root=args.config_runtime.resolve() if args.config_runtime else None
    if args.mode=='live' and not config_root:
        raise ValueError('真实运行需要 --config-runtime；不会从源码查找或推测凭据')
    if config_root and (runtime.is_relative_to(config_root) or report.is_relative_to(config_root)):
        raise ValueError('不能在模型配置/主运行数据目录内写试验')
    runtime.mkdir(parents=True,mode=0o700)
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.model_gateway import ModelConfig, ModelError
    factory=None
    if args.mode=='fault':
        class FaultGateway:
            models={};usage=[];attempts=[]
            def __init__(self,config,cancelled):pass
            def __call__(self,messages,purpose):
                raise ModelError('故障注入：模型服务不可用；未发送外部请求')
        factory=FaultGateway
        config={'provider':'fault-injection','endpoint':'http://127.0.0.1:1/v1','api_key':'fault-not-a-secret',
                'models':{'table':'fault','document':'fault','verify':'fault'},'available_models':['fault']}
        (runtime/'model.json').write_text(json.dumps(config));(runtime/'model.json').chmod(0o600)
    app=create_app(runtime,runtime/'no-demo-samples',False,factory)
    if config_root:
        app.state.answers.config=ModelConfig(config_root)  # Read only; no credentials copied.
    config=app.state.answers.config.load()
    output=dict(schema_version='zhiheng.eval-run.v1',id=uuid.uuid4().hex,mode=args.mode,
                suite_sha256=digest(args.suite),code_sha256=code_digest(ROOT),started_at=now(),
                hardware={'platform':platform.platform(),'machine':platform.machine(),'python':platform.python_version(),'cpu_count':os.cpu_count()},
                dataset_seed=suite.get('seed'),requested_models=config['models'],
                sampling_parameters_recorded_in='cases[].record.model_attempts[].request_settings',cases=[],status='running')
    output['case_manifest']=[{k:c[k] for k in ('id','split','tags','chain_id','turn_index') if k in c} for c in suite['cases']]
    output['source_provenance_setup']=[]
    write_json(report,output,exclusive=True)
    with TestClient(app) as client:
        pid=client.post('/api/projects',json={'name':'隔离评测 '+output['id'][:8]}).json()['id']
        ids={};records={}
        for source in suite['sources']:
            source_file=args.suite.resolve().parent/source['file']
            response=client.post(f'/api/projects/{pid}/import',files=[('files',(source_file.name,source_file.read_bytes()))])
            response.raise_for_status();data=response.json()
            for job in data['jobs']:app.state.service.process_job(job['id'])
            imported=(data['sources']+data['duplicates'])[0]
            ids[source['id']]=imported['id']
            output['source_provenance_setup'].append(register_fixture_provenance(app.state.service.store,imported['id'],source))
            write_json(report,output)
        for case_index, case in enumerate(suite['cases']):
            started=time.monotonic()
            entry=dict(id=case['id'],split=case['split'],tags=case.get('tags',[]),started_at=now(),
                       source_sha256={s['id']:s['sha256'] for s in suite['sources'] if s['id'] in case['source_ids']})
            entry.update({k:case[k] for k in ('chain_id','turn_index') if k in case})
            parent=records.get(case.get('parent_case_id'))
            try:
                if case.get('parent_case_id') and (not parent or parent['status'] not in {'completed','needs_clarification','insufficient_evidence'}):
                    entry['not_run']='依赖轮次未得到可继续状态，保留为未完成'
                else:
                    payload=input_payload(case,ids,parent['id'] if parent else None,parent.get('context_revision') if parent else None)
                    response=client.post(f'/api/projects/{pid}/answers',json=payload)
                    if response.status_code!=202:
                        entry['not_run']='提交失败，HTTP '+str(response.status_code)
                    else:
                        aid=response.json()['id']
                        while True:
                            record=client.get(f'/api/projects/{pid}/answers/{aid}').json()
                            if record['status'] not in ('queued','running'):break
                            if time.monotonic()-started>args.timeout:
                                record=client.post(f'/api/projects/{pid}/answers/{aid}/cancel').json()
                                entry['timeout_cancelled']=True;break
                            time.sleep(.2)
                        if entry.get('timeout_cancelled') or record['status'] == 'cancelled':
                            entry['cancellation_audit_settled'] = wait_answer_cleanup(app.state.answers, aid)
                            if not entry['cancellation_audit_settled']:
                                entry['audit_incomplete'] = True
                                entry['audit_incomplete_reason'] = '后台请求尚未结束，已记录调用与费用仅为下界；未重放请求。'
                        # Export is the product's established recursive secret/path redaction.
                        response=client.get(f'/api/projects/{pid}/answers/{aid}/export?format=json')
                        response.raise_for_status()
                        exported=response.json();record=next(v for v in exported['answers'] if v['id']==aid)
                        entry['record']=record;entry['execution_events']=exported.get('execution_events',{}).get(aid,[])
                        records[case['id']]=record
            except Exception as exc:
                entry['error_type']=type(exc).__name__  # Never include provider bodies.
            entry['elapsed_ms']=round((time.monotonic()-started)*1000)
            output['cases'].append(entry);write_json(report,output)
            print(json.dumps({'id':case['id'],'status':entry.get('record',{}).get('status'),'not_run':entry.get('not_run')},ensure_ascii=False),flush=True)
            if entry.get('audit_incomplete'):
                output['stopped_reason'] = 'cancelled_request_audit_incomplete'
                break
            delay = following_case_cooldown(entry.get('record')) if args.mode == 'live' else 0
            if delay and case_index + 1 < len(suite['cases']):
                entry['following_case_cooldown'] = {'seconds': delay, 'status': 'waiting',
                    'automatic_post_replay': False, 'reason': 'Honor received provider/circuit cooldown before a different case.'}
                write_json(report, output)
                print(json.dumps({'waiting_for_provider_seconds': delay, 'next_case_not_submitted': True}), flush=True)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    time.sleep(min(.2, max(0, deadline - time.monotonic())))
                entry['following_case_cooldown']['status'] = 'finished'
                write_json(report, output)
    output.update(status='finished',finished_at=now(),final_code_sha256=code_digest(ROOT))
    output['code_changed_during_run']=output['code_sha256']!=output['final_code_sha256']
    write_json(report,output)


def score(args):
    run=json.loads(args.input.read_text()); key=json.loads(args.key.read_text())
    if key['suite_sha256']!=run['suite_sha256']:
        raise ValueError('参考答案与输入集合摘要不一致')
    reviews={};review_path=getattr(args,'reviews',None)
    if review_path:
        reviewed=json.loads(Path(review_path).read_text())
        if reviewed.get('run_sha256')!=digest(args.input):raise ValueError('独立审阅不属于当前运行记录')
        reviews=reviewed.get('cases')
        if not isinstance(reviews,dict) or set(reviews)-set(key['cases']):raise ValueError('独立审阅问题标识无效')
        for ident,value in reviews.items():
            if not isinstance(value,dict):raise ValueError('独立审阅须为对象')
            reviews[ident]={**value,'reviewer':value.get('reviewer') or reviewed.get('reviewer')}
    entries=[];actual={e['id']:e for e in run['cases']}
    if len(actual)!=len(run['cases']):raise ValueError('运行记录中问题ID重复')
    if set(actual)-set(key['cases']):raise ValueError('运行记录含参考集合之外的问题')
    # A crashed/interrupted run must retain the frozen denominator, including
    # cases never reached. It cannot score only its successful prefix.
    for ident,expected in key['cases'].items():
        expected=copy.deepcopy(expected)
        if ident in reviews:expected['review']=reviews[ident]
        entry=actual.get(ident,{'id':ident,'record':None})
        assessment=(assess(expected,entry['record']) if expected and entry.get('record') else
                    dict(passed=False if entry.get('record') is None else None,category='not_run' if entry.get('record') is None else 'missing_key',checks=[]))
        entries.append(dict(id=entry['id'],record=entry.get('record'),assessment=assessment,
                            **{k:expected[k] for k in ('chain_id','turn_index') if k in expected}))
    write_json(args.output,dict(schema_version='zhiheng.eval-score.v1',input_sha256=digest(args.input),
               key_sha256=digest(args.key),mode='offline_scoring',model_calls=0,
               reviews_sha256=digest(review_path) if review_path else None,
               evaluated_mode=run['mode'],code_changed_during_run=run.get('code_changed_during_run'),
               entries=entries,summary=summarize(entries)),exclusive=True)


def replay(args):
    data=json.loads(args.input.read_text())
    # This is a faithful offline copy, not a live successful execution claim.
    output=dict(data,mode='offline_replay',replay_of_sha256=digest(args.input),replayed_at=now(),model_calls=0)
    write_json(args.output,output,exclusive=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    r=commands.add_parser('run');r.add_argument('--suite',type=Path,required=True)
    r.add_argument('--runtime',type=Path,required=True);r.add_argument('--output',type=Path,required=True)
    r.add_argument('--mode',choices=['live','fault'],required=True);r.add_argument('--config-runtime',type=Path)
    r.add_argument('--timeout',type=float,default=1200);r.set_defaults(func=run)
    s=commands.add_parser('score');s.add_argument('--input',type=Path,required=True)
    s.add_argument('--key',type=Path,required=True);s.add_argument('--reviews',type=Path);s.add_argument('--output',type=Path,required=True);s.set_defaults(func=score)
    p=commands.add_parser('replay');p.add_argument('--input',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.set_defaults(func=replay)
    args=parser.parse_args();args.func(args)


if __name__=='__main__':main()
