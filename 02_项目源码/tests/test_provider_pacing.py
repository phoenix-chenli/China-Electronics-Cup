"""Controlled HTTP mock + real local OS locks, threads and child processes.

No real network/model requests. Process death is intentional and local only.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import httpx
import pytest

from backend import provider_pacing as pace
from backend.model_gateway import ModelError, ModelGateway
from backend.reliability import GatewayCircuits


@pytest.fixture
def config(monkeypatch):
    monkeypatch.delenv(pace.DIRECTORY_ENV,raising=False)
    monkeypatch.delenv(pace.QUEUE_TIMEOUT_ENV,raising=False)
    pace._registry.clear()
    return {'endpoint':'https://provider.example.test/v1','api_key':'private-test-credential',
            'models':{r:r+'-model' for r in ('table','document','verify')},'timeout':5,'queue_timeout':2}


def mock(monkeypatch,handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(**kw,transport=httpx.MockTransport(handler)))


def ok():return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'}}]})


def test_threads_projects_models_and_paths_share_one_inflight_slot(config,monkeypatch):
    active=0;peak=0;lock=threading.Lock();posts=[]
    async def handler(request):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active);posts.append(time.monotonic())
        await asyncio.sleep(.1)
        with lock:active-=1
        return ok()
    mock(monkeypatch,handler)
    gateways=[ModelGateway({**config,'endpoint':config['endpoint']+('/alt' if i else '')},project_id=f'p{i}') for i in range(3)]
    with ThreadPoolExecutor(3) as pool:
        results=list(pool.map(lambda pair:pair[1]([],('table','document','verify')[pair[0]]),enumerate(gateways)))
    assert all(r=={'ok':True} for r in results) and peak==1 and len(posts)==3
    assert max(g.attempts[0]['queue_wait_ms'] for g in gateways)>=100
    assert all(g.attempts[0]['request_elapsed_ms']>=90 for g in gateways)


@pytest.mark.parametrize('change',[{'endpoint':'https://other.example.test/v1'},{'api_key':'other-private-credential'}])
def test_distinct_provider_or_credential_does_not_wait(config,monkeypatch,change):
    held=pace.ProviderLease(config,lambda:False,{})
    asyncio.run(held.acquire())
    mock(monkeypatch,lambda request:ok())
    try:
        gateway=ModelGateway({**config,**change,'queue_timeout':.04})
        assert gateway([],'table')=={'ok':True}
        assert gateway.attempts[0]['queue_wait_ms']<30
    finally:held.release()


def test_wait_cancel_has_no_post_and_releases_half_open_probe(config,monkeypatch):
    held=pace.ProviderLease(config,lambda:False,{})
    asyncio.run(held.acquire());event=threading.Event();posts=[]
    mock(monkeypatch,lambda request:(posts.append(1),ok())[1])
    registry=GatewayCircuits(threshold=1,cooldown=.01)
    breaker=registry.get('p',config['endpoint'],'table-model');breaker.before();breaker.after('timeout');time.sleep(.02)
    gateway=ModelGateway(config,cancelled=event.is_set,circuit_registry=registry,project_id='p')
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(gateway,[],'table');time.sleep(.06);event.set()
        with pytest.raises(InterruptedError):future.result(timeout=1)
    held.release()
    attempt=gateway.attempts[0]
    assert not posts and attempt['post_started'] is False and attempt['request_elapsed_ms']==0
    assert attempt['queue_wait_ms']>=40 and attempt['status']=='cancelled'
    assert breaker.failures==1 and not breaker.probing


def test_wait_timeout_does_not_count_as_transport_failure(config,monkeypatch):
    held=pace.ProviderLease(config,lambda:False,{})
    asyncio.run(held.acquire());posts=[]
    mock(monkeypatch,lambda request:(posts.append(1),ok())[1])
    registry=GatewayCircuits(threshold=1)
    gateway=ModelGateway({**config,'queue_timeout':.06},circuit_registry=registry,project_id='p')
    try:
        with pytest.raises(ModelError) as error:gateway([],'table')
    finally:held.release()
    assert error.value.code=='queue_timeout' and not posts
    assert gateway.attempts[0]['post_started'] is False and gateway.attempts[0]['request_elapsed_ms']==0
    assert registry.get('p',config['endpoint'],'table-model').failures==0


def test_429_cools_only_subsequent_request_without_replaying_failed_post(config,monkeypatch):
    starts=[]
    def handler(request):
        starts.append(time.monotonic())
        return httpx.Response(429,headers={'Retry-After':'.15'}) if len(starts)==1 else ok()
    mock(monkeypatch,handler)
    first=ModelGateway(config,project_id='p1')
    with pytest.raises(ModelError):first([],'table')
    assert len(starts)==1 and first.attempts[0]['provider_cooldown_seconds']==.15
    second=ModelGateway(config,project_id='p2');assert second([],'verify')=={'ok':True}
    assert len(starts)==2 and starts[1]-starts[0]>=.14
    assert second.attempts[0]['cooldown_wait_ms']>=100
    assert second.attempts[0]['request_elapsed_ms']<second.attempts[0]['queue_wait_ms']


def test_429_http_date_is_recorded_and_can_be_cancelled_without_next_post(config,monkeypatch):
    posts=[];target=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=5),usegmt=True)
    mock(monkeypatch,lambda r:(posts.append(1),httpx.Response(429,headers={'Retry-After':target}))[1])
    first=ModelGateway(config)
    with pytest.raises(ModelError):first([],'table')
    assert 3<first.attempts[0]['provider_cooldown_seconds']<=5
    second=ModelGateway({**config,'queue_timeout':.05})
    with pytest.raises(ModelError) as e:second([],'document')
    assert e.value.code=='queue_timeout' and len(posts)==1 and not second.attempts[0]['post_started']


@pytest.mark.parametrize('failure',['timeout','transport','cancel','bad_json','exception'])
def test_any_http_exit_releases_slot_without_unknown_post_retry(config,monkeypatch,failure):
    calls=[];cancelled=threading.Event()
    async def handler(request):
        calls.append(1)
        if len(calls)>1:return ok()
        if failure=='timeout':raise httpx.ReadTimeout('private-test-credential',request=request)
        if failure=='transport':raise httpx.ConnectError('private-test-credential',request=request)
        if failure=='bad_json':return httpx.Response(200,text='private-test-credential')
        if failure=='exception':raise RuntimeError('private-test-credential')
        cancelled.set();await asyncio.sleep(3);return ok()
    mock(monkeypatch,handler)
    first=ModelGateway(config,cancelled=cancelled.is_set)
    with pytest.raises((ModelError,InterruptedError,RuntimeError)):first([],'table')
    assert len(calls)==1
    second=ModelGateway({**config,'queue_timeout':.1});assert second([],'table')=={'ok':True}
    assert len(calls)==2 and 'private-test-credential' not in json.dumps(first.attempts)


def _process_request(directory,config,queue,start):
    os.environ[pace.DIRECTORY_ENV]=directory
    original=httpx.AsyncClient
    async def handler(request):
        queue.put(('post_started',os.getpid(),time.monotonic()))
        await asyncio.sleep(.15)
        queue.put(('post_finished',os.getpid(),time.monotonic()))
        return ok()
    httpx.AsyncClient=lambda **kw:original(**kw,transport=httpx.MockTransport(handler))
    queue.put(('ready',os.getpid(),0));start.wait(5)
    gateway=ModelGateway(config);gateway([],'table')
    queue.put(('attempt',os.getpid(),gateway.attempts[0]))


def _die_holding_lock(directory,config,queue):
    os.environ[pace.DIRECTORY_ENV]=directory
    lease=pace.ProviderLease(config,lambda:False,{})
    asyncio.run(lease.acquire());queue.put('locked');queue.close();queue.join_thread();os._exit(0)


def _process_rate_limit(directory,config,queue):
    os.environ[pace.DIRECTORY_ENV]=directory
    original=httpx.AsyncClient
    httpx.AsyncClient=lambda **kw:original(**kw,transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'5'})))
    gateway=ModelGateway(config)
    try:gateway([],'table')
    except ModelError:pass
    queue.put(gateway.attempts[0])


def test_shared_os_lock_serializes_real_processes_and_keeps_private_permissions(config,tmp_path):
    folder=tmp_path/'private';ctx=multiprocessing.get_context('spawn');queue=ctx.Queue();start=ctx.Event()
    processes=[ctx.Process(target=_process_request,args=(str(folder),config,queue,start)) for _ in range(2)]
    for process in processes:process.start()
    assert [queue.get(timeout=8)[0] for _ in processes]==['ready','ready'];start.set()
    events=[queue.get(timeout=8) for _ in range(6)]
    for process in processes:process.join(5);assert process.exitcode==0
    ranges={pid:{kind:at for kind,p,at in events if p==pid and kind.startswith('post_')} for pid in {e[1] for e in events}}
    spans=sorted((v['post_started'],v['post_finished']) for v in ranges.values())
    assert spans[1][0]>=spans[0][1]
    attempts=[v for k,_,v in events if k=='attempt'];assert all(v['pacing_scope']=='shared_processes' for v in attempts)
    assert max(v['queue_wait_ms'] for v in attempts)>=100
    if os.name != 'nt':
        assert folder.stat().st_mode&0o777==0o700
    for p in folder.iterdir():
        if os.name != 'nt':
            assert p.stat().st_mode&0o777==0o600
        assert 'provider.example.test' not in p.name and 'private-test-credential' not in p.read_text()


def test_process_death_releases_lock_without_removing_lock_file(config,tmp_path,monkeypatch):
    folder=tmp_path/'private';ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
    process=ctx.Process(target=_die_holding_lock,args=(str(folder),config,queue));process.start()
    assert queue.get(timeout=8)=='locked';process.join(5);assert process.exitcode==0
    monkeypatch.setenv(pace.DIRECTORY_ENV,str(folder));lease=pace.ProviderLease({**config,'queue_timeout':.2},lambda:False,{})
    asyncio.run(lease.acquire());lease.release()
    assert len(list(folder.glob('*.lock')))==1


def test_received_429_cooldown_survives_process_exit_and_blocks_only_same_credential(config,tmp_path,monkeypatch):
    folder=tmp_path/'private';ctx=multiprocessing.get_context('spawn');queue=ctx.Queue()
    process=ctx.Process(target=_process_rate_limit,args=(str(folder),config,queue));process.start()
    first=queue.get(timeout=8);process.join(5);assert process.exitcode==0
    assert first['http_status']==429 and first['provider_cooldown_seconds']==5
    monkeypatch.setenv(pace.DIRECTORY_ENV,str(folder));posts=[]
    mock(monkeypatch,lambda r:(posts.append(1),ok())[1])
    same=ModelGateway({**config,'queue_timeout':.06})
    with pytest.raises(ModelError) as e:same([],'verify')
    assert e.value.code=='queue_timeout' and not posts and not same.attempts[0]['post_started']
    other=ModelGateway({**config,'api_key':'different-private-token','queue_timeout':.06})
    assert other([],'verify')=={'ok':True} and len(posts)==1
    saved=''.join(p.name+p.read_text() for p in folder.iterdir())
    assert 'private-test-credential' not in saved and 'provider.example.test' not in saved and 'different-private-token' not in saved


def test_corrupt_state_recovers_once_and_clock_jump_does_not_extend_forever(config,tmp_path,monkeypatch):
    folder=tmp_path/'private';monkeypatch.setenv(pace.DIRECTORY_ENV,str(folder));monkeypatch.setattr(pace,'DEFAULT_COOLDOWN',.05)
    lease=pace.ProviderLease(config,lambda:False,{});asyncio.run(lease.acquire());lease.defer(.05);state=lease.state_path;lease.release()
    state.write_text('{broken');state.chmod(0o600)
    attempt={};next_lease=pace.ProviderLease(config,lambda:False,attempt);asyncio.run(next_lease.acquire());next_lease.release()
    assert attempt['pacing_state_recovered'] and attempt['queue_wait_ms']<500
    data=json.loads(state.read_text());data['written_wall']-=7200;state.write_text(json.dumps(data))
    attempt={};last=pace.ProviderLease(config,lambda:False,attempt);asyncio.run(last.acquire());last.release()
    assert attempt['pacing_wall_clock_changed'] and attempt['queue_wait_ms']<100


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode bits; Windows uses ACL checks')
def test_shared_state_rejects_public_directory_before_post(config,tmp_path,monkeypatch):
    folder=tmp_path/'public';folder.mkdir(mode=0o755);folder.chmod(0o755);monkeypatch.setenv(pace.DIRECTORY_ENV,str(folder))
    posts=[];mock(monkeypatch,lambda r:(posts.append(1),ok())[1]);gateway=ModelGateway(config)
    with pytest.raises(ModelError) as e:gateway([],'table')
    assert e.value.code=='pacing_failed' and not posts and not gateway.attempts[0]['post_started']


@pytest.mark.parametrize('value',[True,-1,0,'bad',float('nan'),float('inf')])
def test_invalid_queue_timeout_never_posts(config,monkeypatch,value):
    posts=[];mock(monkeypatch,lambda r:(posts.append(1),ok())[1])
    gateway=ModelGateway({**config,'queue_timeout':value})
    with pytest.raises(ModelError) as e:gateway([],'table')
    assert e.value.code=='config_failed' and not posts and not gateway.attempts[0]['post_started']
