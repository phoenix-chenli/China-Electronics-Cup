import hashlib
import json
import threading
from datetime import datetime,timezone
from pathlib import Path
import pytest
from backend.reliability import BoundedExecutor,QueueFull,CircuitBreaker,retry_policy,source_diagnostics,cancel_import,import_cancelled,retry_import,locked_dependencies
from backend.model_gateway import ModelError
from backend.service import Service
from backend.demo_workspace import load_demo


def test_queue_backpressure_cancel_and_release_are_bounded():
    hold=threading.Event();pool=BoundedExecutor(1,1)
    try:
        one=pool.submit(hold.wait);two=pool.submit(lambda:42)
        with pytest.raises(QueueFull):pool.submit(lambda:43)
        assert pool.snapshot()['running_or_pending']==2
        assert two.cancel()
        three=pool.submit(lambda:44);hold.set()
        assert one.result(timeout=2) and three.result(timeout=2)==44
    finally:hold.set();pool.shutdown()
    assert pool.snapshot()['running_or_pending']==0
    with pytest.raises(RuntimeError):pool.submit(lambda:0)


def test_circuit_opens_without_replaying_then_only_one_new_probe():
    clock=[0];c=CircuitBreaker(2,30,lambda:clock[0])
    c.before();c.after('timeout');c.before();c.after('transport_failed')
    with pytest.raises(ModelError):c.before()
    assert not c.snapshot()['automatic_post_replay']
    clock[0]=31;c.before()
    with pytest.raises(ModelError):c.before()
    c.after();c.before();assert c.snapshot()['state']=='closed'


@pytest.mark.parametrize('status',[401,402,403])
def test_account_errors_stop_new_calls_on_first_failure(status):
    clock=[0];c=CircuitBreaker(clock=lambda:clock[0])
    c.before();c.after(f'http_{status}')
    with pytest.raises(ModelError):c.before()
    assert c.snapshot()['state']=='open'
    assert retry_policy(status,post_started=True)['category']=='authentication_or_billing'
    clock[0]=301;c.before();c.after('completed')
    assert c.snapshot()['state']=='closed'


@pytest.mark.parametrize('status,code,category',[(429,None,'rate_limited'),(503,None,'temporarily_unavailable'),(None,'schema_failed','protocol'),(None,'read_timeout','transport')])
def test_retry_classification_never_resends_unknown_post(status,code,category):
    r=retry_policy(status,'12',post_started=True,error_code=code)
    assert r['category']==category and r['retry_after_seconds']==12
    assert not r['automatic_retry'] and r['strategy']=='explicit_new_request_only'


def test_retry_after_http_date_and_bad_values():
    r=retry_policy(429,'Wed, 21 Oct 2015 07:28:00 GMT',wall_clock=datetime(2015,10,21,7,27,0,tzinfo=timezone.utc))
    assert r['retry_after_seconds']==60
    assert retry_policy(503,'nan')['retry_after_seconds'] is None
    assert retry_policy(503,'-5')['retry_after_seconds'] is None


def service(tmp_path):
    result=Service(tmp_path/'runtime',tmp_path/'samples',workers=False);result.start();return result


def staged(s,project):
    path=s.root/'incoming'/'in';path.write_text('标题\n正文12.50')
    return s.import_staged(project['id'],[{'path':path,'name':'原文.txt','format':'txt','bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}])


def test_cancelled_import_is_not_recovered_on_restart_and_retry_is_explicit(tmp_path):
    s=service(tmp_path)
    try:
        p=s.store.create_project('甲','');data=staged(s,p);source=data['sources'][0];job=data['jobs'][0]
        with pytest.raises(KeyError):cancel_import(s,source['id'],'wrong-project')
        assert cancel_import(s,source['id'],p['id'])
        assert import_cancelled(s,job['id'])
        assert s.store.recover()==[]
        r=retry_import(s,source['id'],p['id']);assert r['job']['id']!=job['id']
        assert s.store.get('jobs',job['id'])['status']=='cancelled'
    finally:s.close()


def test_diagnostics_do_not_disclose_internal_paths(tmp_path):
    s=service(tmp_path)
    try:
        p=s.store.create_project('甲','');d=staged(s,p);row=s.store.get('sources',d['sources'][0]['id'])
        r=source_diagnostics(row)
        assert 'original_path' not in r and 'data_path' not in r
        assert str(tmp_path) not in json.dumps(r)
    finally:s.close()


def test_demo_project_is_isolated_idempotent_and_checks_actual_sample_sha(tmp_path):
    s=service(tmp_path)
    try:
        private=s.store.create_project('个人','')
        s.samples.mkdir();sample=s.samples/'例.txt';sample.write_text('公开示例12.50')
        manifest=[{'file':'例.txt','origin':'https://example.org/source','sha256':hashlib.sha256(sample.read_bytes()).hexdigest()}]
        (s.samples/'manifest.json').write_text(json.dumps(manifest))
        one=load_demo(s);two=load_demo(s)
        assert one['project']['id']==two['project']['id']!=private['id']
        assert len(one['sources'])==len(two['duplicates'])==1
        assert s.store.sources(private['id'])==[] and not two['automatic_answers_submitted']
        sample.write_text('changed')
        with pytest.raises(ValueError,match='摘要'):load_demo(s)
    finally:s.close()


def test_dependency_lock_requires_exact_versions(tmp_path):
    path=tmp_path/'bad.lock';path.write_text('fastapi>=0.1')
    with pytest.raises(ValueError):locked_dependencies(path)
    path.write_text('not-installed-zhiheng==0.0.0')
    assert not locked_dependencies(path)['ok']


def test_local_subprocess_cancellation_kills_own_child_promptly(tmp_path):
    import os,sys,time,subprocess
    from backend.reliability import run_cancellable
    cancelled=threading.Event();timer=threading.Timer(.25,cancelled.set);timer.start()
    began=time.monotonic()
    with pytest.raises(InterruptedError):
        run_cancellable([sys.executable,'-c','import time;time.sleep(10)'],timeout=8,cancelled=cancelled.is_set)
    timer.join();assert time.monotonic()-began<2
    with pytest.raises(subprocess.TimeoutExpired):
        run_cancellable([sys.executable,'-c','import time;time.sleep(10)'],timeout=.15)


def test_cancel_before_start_never_starts_process(monkeypatch):
    from backend.reliability import run_cancellable
    monkeypatch.setattr('backend.reliability.subprocess.Popen',lambda *a,**k:pytest.fail('unexpected process'))
    with pytest.raises(InterruptedError):run_cancellable(['never-run'],timeout=1,cancelled=lambda:True)
