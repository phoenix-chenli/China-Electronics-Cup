"""Real stores/worker pools with explicit local fault fixtures; no provider calls."""
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

import pytest
from backend import parsers
from backend.service import Service
from backend.answer_service import AnswerService
from backend.model_gateway import ModelGateway,ModelError
from backend.reliability import BoundedExecutor,cancel_import,retry_import
from backend.retrieval import RetrievalService


def wait_for(check,seconds=10):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=check()
        if value:return value
        time.sleep(.01)
    pytest.fail('bounded integration wait expired')


def import_text(service,pid,text):
    target=service.root/'incoming'/uuid.uuid4().hex;target.write_text(text)
    value=service.import_staged(pid,[{'path':target,'name':'原文.txt','format':'txt','bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}])
    return value['sources'][0],value['jobs'][0]


def result(text):
    return {'pages':[{'number':1,'text':text}],'tables':[],'metadata':{},'warnings':[],
            'page_count':1,'data_path':None,'summary':text}


def test_cancelled_queued_import_never_enters_parser_or_recovers(tmp_path,monkeypatch):
    calls=[];monkeypatch.setattr(parsers,'parse',lambda *a,**k:calls.append(1))
    service=Service(tmp_path/'runtime',tmp_path/'samples',workers=False);service.start()
    try:
        pid=service.store.create_project('P','')['id'];source,job=import_text(service,pid,'queued')
        assert cancel_import(service,source['id'],pid)
        service.process_job(job['id'])
        assert calls==[] and service.store.recover()==[]
        assert service.store.get('sources',source['id'])['status']=='cancelled'
    finally:service.close()


def test_late_old_parse_cannot_override_explicit_retry_result(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event();counter=[0];kwargs_seen=[]
    def parse(path,fmt,destination,progress,**kwargs):
        counter[0]+=1;number=counter[0];kwargs_seen.append(kwargs)
        if number==1:entered.set();assert release.wait(5)
        return result('old rejected value' if number==1 else 'new visible value')
    monkeypatch.setattr(parsers,'parse',parse)
    service=Service(tmp_path/'runtime',tmp_path/'samples');service.start()
    try:
        pid=service.store.create_project('P','')['id'];source,job=import_text(service,pid,'fixture')
        assert entered.wait(3);assert cancel_import(service,source['id'],pid)
        retried=retry_import(service,source['id'],pid)
        wait_for(lambda:service.store.get('sources',source['id'])['status']=='ready')
        release.set();wait_for(lambda:not service.futures)
        assert service.store.get('sources',source['id'])['pages'][0]['text']=='new visible value'
        assert service.store.get('jobs',job['id'])['status']=='cancelled'
        assert service.store.get('jobs',retried['job']['id'])['status']=='completed'
        assert kwargs_seen[0]['checkpoint_dir']==kwargs_seen[1]['checkpoint_dir']
        assert source['id'] in str(kwargs_seen[0]['checkpoint_dir'])
        assert kwargs_seen[0]['cancelled']() is True
    finally:release.set();service.close()


def test_import_pump_drains_over_capacity_without_duplicate_work(tmp_path,monkeypatch):
    hold=threading.Event();entered=threading.Event();counts=Counter();lock=threading.Lock()
    def parse(path,*args,**kwargs):
        with lock:counts[path.name]+=1
        entered.set();assert hold.wait(5)
        return result('usable')
    monkeypatch.setattr(parsers,'parse',parse)
    service=Service(tmp_path/'runtime',tmp_path/'samples');service.start()
    try:
        pid=service.store.create_project('P','')['id']
        jobs=[]
        for i in range(45):_,job=import_text(service,pid,f'unique{i}');jobs.append(job)
        assert entered.wait(3)
        assert service.pool.snapshot()['running_or_pending']<=18
        assert any(service.store.get('jobs',j['id'])['status']=='queued' for j in jobs)
        # Calling pump/schedule again must not add duplicate copies.
        for job in jobs:service.schedule(job['id'])
        hold.set()
        wait_for(lambda:all(service.store.get('jobs',j['id'])['status']=='completed' for j in jobs))
        assert len(counts)==45 and set(counts.values())=={1}
    finally:hold.set();service.close()


class GatewayFixture(ModelGateway):
    instances=[];requests=[];entered=threading.Event();release=threading.Event()
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.instances.append(self)
    async def _request(self,body):
        self.requests.append((self.project_id,self.config['endpoint'],body['model']))
        self.entered.set()
        while not self.release.is_set():await asyncio.sleep(.01)
        self.attempts[-1]['post_started']=True  # Explicit network-mechanism fixture only.
        raise ModelError('mechanism fixture, no actual network','transport_failed')


@pytest.fixture
def answer_env(tmp_path):
    service=Service(tmp_path/'runtime',tmp_path/'samples',workers=False);service.start()
    projects=[];sources=[]
    for name in ('P1','P2'):
        pid=service.store.create_project(name,'')['id'];source,job=import_text(service,pid,'真实原文费用12元。')
        service.process_job(job['id']);projects.append(pid);sources.append(source['id'])
    config={'endpoint':'https://fixture.invalid/v1','api_key':'','models':{r:'fixture-model' for r in ('table','document','verify')}}
    (service.root/'model.json').write_text(json.dumps(config))
    GatewayFixture.instances=[];GatewayFixture.requests=[];GatewayFixture.entered=threading.Event();GatewayFixture.release=threading.Event()
    answers=AnswerService(service,GatewayFixture,lambda directory:RetrievalService(directory,default_mode='lexical'));answers.start()
    try:yield service,answers,projects,sources
    finally:GatewayFixture.release.set();answers.close();service.close()


def submit(answers,pid,sid,request=None):
    return answers.submit(pid,'说明费用。',sid,'document',None,request or str(uuid.uuid4()))


def test_answer_queue_cancel_dedupe_and_saturation_never_extra_posts(answer_env):
    service,answers,projects,sources=answer_env
    answers.pool.shutdown();answers.pool=BoundedExecutor(1,1)
    first=submit(answers,projects[0],sources[0]);assert GatewayFixture.entered.wait(3)
    request=str(uuid.uuid4());queued=submit(answers,projects[0],sources[0],request)
    repeated=submit(answers,projects[0],sources[0],request);assert repeated['id']==queued['id']
    overflow=submit(answers,projects[1],sources[1]);assert overflow['status']=='failed'
    assert not overflow.get('model_attempts')
    assert answers.cancel(projects[0],queued['id'])['status']=='cancelled'
    GatewayFixture.release.set()
    wait_for(lambda:not answers.events)
    assert len(GatewayFixture.requests)==1
    assert answers.store.get(projects[0],queued['id'])['status']=='cancelled'
    assert answers.store.get(projects[1],overflow['id'])['status']=='failed'


def test_answer_gateway_receives_service_and_project_scoped_circuit(answer_env):
    service,answers,projects,sources=answer_env;GatewayFixture.release.set()
    first=submit(answers,projects[0],sources[0]);second=submit(answers,projects[1],sources[1])
    wait_for(lambda:not answers.events)
    assert {g.project_id for g in GatewayFixture.instances}==set(projects)
    assert all(g.circuit_registry is answers.gateway_circuits for g in GatewayFixture.instances)
    for pid in projects:
        breaker=answers.gateway_circuits.get(pid,'https://fixture.invalid/v1','fixture-model')
        assert breaker.failures==1
    # Restart converts interrupted records to failed and never invokes factory.
    count=len(GatewayFixture.requests);answers.close();answers.start()
    assert len(GatewayFixture.requests)==count
    assert answers.store.get(projects[0],first['id'])['status']=='failed'
