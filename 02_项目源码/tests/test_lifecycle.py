"""Real isolated Mac processes; delayed parsers/gateways are explicit fixtures."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest

from backend.service import Service

ROOT = Path(__file__).resolve().parents[1]
EVENTS = []
pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac launcher lifecycle acceptance')

INJECTION = '''
import os, sys
if 'uvicorn' in getattr(sys, 'orig_argv', []):
    from pathlib import Path
    import json, time
    from backend import parsers, model_gateway
    runtime = Path(os.environ['ZHIHENG_DATA_DIR'])
    def append(name, payload):
        with (runtime / name).open('a') as stream:
            stream.write(json.dumps(payload) + '\\n')
    original_parse = parsers.parse
    def delayed_parse(path, ext, destination, progress, **kwargs):
        append('fixture-parses.jsonl', {'event': 'entered', 'format': ext})
        deadline = time.monotonic() + 25
        while (runtime / 'hold-parse').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        result = original_parse(path, ext, destination, progress, **kwargs)
        append('fixture-parsed.jsonl', {'event': 'parsed', 'data_path': result.get('data_path')})
        deadline = time.monotonic() + 25
        while (runtime / 'hold-after-parse').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        return result
    parsers.parse = delayed_parse
    class DelayedGateway:
        def __init__(self, config, cancelled):
            if (runtime / 'fail-gateway-constructor').exists():
                raise RuntimeError('fixture constructor failure')
            self.models = {}; self.usage = []; self.attempts = []
        def __call__(self, messages, role):
            append('fixture-model-calls.jsonl', {'role': role})
            deadline = time.monotonic() + 25
            # Deliberately ignore cancellation to exercise a late callback.
            while (runtime / 'hold-gateway').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            append('fixture-model-returns.jsonl', {'role': role})
            return {'status': 'needs_clarification', 'message': '机制夹具返回',
                    'clarification': {'question': '机制夹具澄清', 'options': []}}
    model_gateway.ModelGateway = DelayedGateway
'''


def wait_until(check, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('bounded lifecycle wait expired')


def alive(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


def free_port(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) != 0


@pytest.fixture(scope='session', autouse=True)
def lifecycle_report():
    yield
    destination = os.environ.get('ZHIHENG_LIFECYCLE_REPORT')
    if destination:
        report = {'environment': {'platform': platform.platform(), 'machine': platform.machine(),
                   'python': sys.version, 'executable': sys.executable,
                   'installed_dependencies_reused': True, 'fresh_mac_install_tested': False,
                   'finder_double_click_tested': False, 'real_model_calls': 0}, 'events': EVENTS}
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


class AppCopy:
    def __init__(self, path):
        self.path = path
        path.mkdir(parents=True)
        shutil.copytree(ROOT / 'backend', path / 'backend', ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('start.py', '启动知衡.command', '停止知衡.command'):
            shutil.copy2(ROOT / name, path / name)
        (path / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        (path / 'frontend').mkdir()
        (path / 'frontend' / 'dist').symlink_to(ROOT / 'frontend' / 'dist', target_is_directory=True)
        (path / 'samples').mkdir()
        self.runtime = path / '.runtime'
        self.runtime.mkdir()
        injection = path / 'fixture-injection'
        injection.mkdir()
        (injection / 'sitecustomize.py').write_text(INJECTION)
        self.env = os.environ.copy()
        self.env['PYTHONPATH'] = str(injection) + os.pathsep + str(path)
        self.client = httpx.Client(base_url='http://127.0.0.1:8879', trust_env=False, timeout=3)

    def command(self, *, stop=False, port=8879, ok=True):
        name = '停止知衡.command' if stop else '启动知衡.command'
        started = time.monotonic()
        result = subprocess.run([str(self.path / name), '--port', str(port), '--no-browser'],
                                cwd='/tmp', env=self.env, text=True, capture_output=True, timeout=45)
        EVENTS.append({'kind': 'command', 'workspace': str(self.path), 'command': name, 'port': port,
                       'elapsed_ms': round((time.monotonic()-started)*1000), 'returncode': result.returncode,
                       'stdout': result.stdout, 'stderr': result.stderr})
        if ok:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def health(self):
        try:
            result = self.client.get('/api/health')
            return result.json() if result.is_success else None
        except httpx.HTTPError:
            return None

    def kill_owned(self):
        identity = self.health()
        assert identity and identity['workspace'] == str(self.path)
        pid = identity['pid']
        assert os.getpgid(pid) == pid
        os.killpg(pid, signal.SIGKILL)
        wait_until(lambda: not alive(pid))
        EVENTS.append({'kind': 'fixture_SIGKILL', 'pid': pid, 'workspace': str(self.path)})
        return pid

    def import_csv(self, *, wait=True):
        project = self.client.post('/api/projects', json={'name': '恢复验收'}).json()
        content = '编号,数值\n甲,10\n乙,20\n'.encode()
        imported = self.client.post(f"/api/projects/{project['id']}/import", files={'files': ('中文 数据.csv', content, 'text/csv')})
        assert imported.status_code == 200, imported.text
        data = imported.json()
        source = data['sources'][0]
        if wait:
            wait_until(lambda: self.client.get('/api/sources/'+source['id']).json()['source']['status'] in {'ready','attention'})
        return project, source, data['jobs'][0], content

    def configure_fixture(self):
        # This is an empty-credential local mechanism fixture, never the real config.
        config = {'provider': 'lifecycle fixture', 'endpoint': 'http://127.0.0.1:8880', 'api_key': '',
                  'models': {role:'mechanism-fixture' for role in ('table','document','verify')}}
        path = self.runtime / 'model.json'
        path.write_text(json.dumps(config)); path.chmod(0o600)

    def answer(self, project, source):
        table = self.client.get('/api/sources/'+source['id']).json()['tables'][0]['name']
        body = {'question': '统计记录', 'source_id': source['id'], 'mode': 'table',
                'table_name': table, 'request_id': str(uuid.uuid4())}
        response = self.client.post(f"/api/projects/{project['id']}/answers", json=body)
        assert response.status_code == 202, response.text
        return response.json(), body

    def lines(self, name):
        path = self.runtime / name
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def appcopy(tmp_path, request):
    if not free_port(8879) or not free_port(8880):
        pytest.skip('isolated acceptance ports 8879/8880 are already occupied; no process stopped')
    base = Path(os.environ['ZHIHENG_LIFECYCLE_WORK']) if os.environ.get('ZHIHENG_LIFECYCLE_WORK') else tmp_path
    app = AppCopy(base / (request.node.name + '-' + uuid.uuid4().hex[:6]) / '中文 空格 启动验收')
    try:
        yield app
    finally:
        app.command(stop=True, ok=False)
        app.client.close()


def test_cold_warm_concurrent_and_command_permissions(appcopy):
    app = appcopy
    assert all(os.access(app.path / name, os.X_OK) for name in ('启动知衡.command','停止知衡.command'))
    app.command()
    pid = app.health()['pid']
    app.command()
    assert app.health()['pid'] == pid
    commands = [subprocess.Popen([str(app.path/'启动知衡.command'), '--port','8879','--no-browser'],
                 cwd='/tmp', env=app.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for command in commands:
        stdout, stderr = command.communicate(timeout=25)
        assert command.returncode == 0, stdout+stderr
    assert app.health()['pid'] == pid
    app.command(stop=True)
    assert not alive(pid) and free_port(8879)
    started=time.monotonic()
    cold_commands = [subprocess.Popen([str(app.path/'启动知衡.command'), '--port','8879','--no-browser'],
                     cwd='/tmp', env=app.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for command in cold_commands:
        stdout, stderr = command.communicate(timeout=25)
        assert command.returncode == 0, stdout+stderr
    new_pid = app.health()['pid']
    assert new_pid != pid
    EVENTS.append({'kind':'cold_warm_duplicate', 'first_pid':pid, 'restart_pid':new_pid,
                   'concurrent_launches_same_pid':True, 'concurrent_cold_launches':2,
                   'concurrent_cold_elapsed_ms':round((time.monotonic()-started)*1000),
                   'executable_commands':True, 'finder_double_click':False})


@pytest.mark.parametrize('shape', ['foreign','spoofed_pid','non_object'])
def test_foreign_port_and_untrusted_health_never_signal_process(appcopy, shape):
    app = appcopy
    sleeper = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],cwd='/tmp')
    payload = [] if shape == 'non_object' else {'service':'zhiheng' if shape=='spoofed_pid' else 'unrelated',
                                               'workspace':str(app.path), 'pid':sleeper.pid}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(payload).encode())
        def log_message(self,*args):pass
    server = HTTPServer(('127.0.0.1',8880),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        assert app.command(port=8880,ok=False).returncode == 1
        assert app.command(stop=True,port=8880,ok=False).returncode == 1
        assert sleeper.poll() is None
        EVENTS.append({'kind':'foreign_port','shape':shape,'fixture_pid':sleeper.pid,'untouched':True})
    finally:
        server.shutdown();server.server_close();sleeper.terminate();sleeper.wait()


def test_second_port_cannot_share_runtime(appcopy):
    app = appcopy
    app.command();pid=app.health()['pid']
    rejected=app.command(port=8880,ok=False)
    assert rejected.returncode == 1
    assert app.health()['pid']==pid and free_port(8880)
    EVENTS.append({'kind':'runtime_lock','primary_pid':pid,'second_port_rejected':True})


@pytest.mark.parametrize('shutdown', ['kill','stop'])
def test_interrupted_import_recovers_same_job_and_original(appcopy, shutdown):
    app=appcopy
    (app.runtime/'hold-parse').touch();app.command()
    project,source,job,content=app.import_csv(wait=False)
    wait_until(lambda:app.lines('fixture-parses.jsonl'))
    pid=app.health()['pid']
    if shutdown=='kill':app.kill_owned()
    else:
        result=app.command(stop=True)
        assert '已结束本次服务及其子进程' in result.stdout
    assert not alive(pid)
    (app.runtime/'hold-parse').unlink()
    app.command()
    detail=wait_until(lambda:(d if (d:=app.client.get('/api/sources/'+source['id']).json())['source']['status'] in {'ready','attention'} else None))
    jobs=app.client.get(f"/api/projects/{project['id']}/jobs").json()
    assert len(jobs)==1 and jobs[0]['id']==job['id'] and jobs[0]['status']=='completed'
    assert detail['source']['id']==source['id'] and detail['source']['row_count']==2
    downloaded=app.client.get('/api/sources/'+source['id']+'/original').content
    assert downloaded==content and hashlib.sha256(downloaded).hexdigest()==source['sha256']
    assert len(app.lines('fixture-parses.jsonl'))==2
    EVENTS.append({'kind':'import_recovery','shutdown':shutdown,'source_id':source['id'],'job_id':job['id'],
                   'original_sha256':source['sha256'],'original_unchanged':True,'same_job':True,'parse_attempts':2})


def test_interrupted_answer_restart_never_replays_gateway(appcopy):
    app=appcopy;app.configure_fixture();app.command()
    project,source,_,_=app.import_csv()
    (app.runtime/'hold-gateway').touch()
    answer,body=app.answer(project,source)
    wait_until(lambda:app.lines('fixture-model-calls.jsonl'))
    app.kill_owned();(app.runtime/'hold-gateway').unlink();app.command()
    url=f"/api/projects/{project['id']}/answers/{answer['id']}"
    interrupted=app.client.get(url).json()
    assert interrupted['status']=='failed' and '未自动重复调用' in interrupted['error']
    replay=app.client.post(f"/api/projects/{project['id']}/answers",json=body).json()
    assert replay['id']==answer['id'] and replay['status']=='failed'
    time.sleep(.2)
    assert len(app.lines('fixture-model-calls.jsonl'))==1
    EVENTS.append({'kind':'answer_restart','status':interrupted['status'],'fixture_calls_before':1,
                   'fixture_calls_after':1,'idempotent_request_preserved':True,'real_model':False})


def test_finished_derived_table_before_crash_can_be_reparsed(appcopy):
    app=appcopy;(app.runtime/'hold-after-parse').touch();app.command()
    project,source,job,content=app.import_csv(wait=False)
    wait_until(lambda:app.lines('fixture-parsed.jsonl'))
    derived=Path(json.loads(app.lines('fixture-parsed.jsonl')[0])['data_path'])
    assert derived.is_file() and derived.stat().st_size>0
    app.kill_owned();(app.runtime/'hold-after-parse').unlink();app.command()
    detail=wait_until(lambda:(d if (d:=app.client.get('/api/sources/'+source['id']).json())['source']['status'] in {'ready','attention','failed'} else None))
    EVENTS.append({'kind':'partial_derived_recovery','status':detail['source']['status'],
                   'data_existed_before_crash':True,'job_id':job['id']})
    assert detail['source']['status']=='ready',detail['source']['error']
    assert detail['source']['row_count']==2
    assert app.client.get('/api/sources/'+source['id']+'/original').content==content


def test_cancel_before_late_gateway_result_is_terminal(appcopy):
    app=appcopy;app.configure_fixture();app.command()
    project,source,_,_=app.import_csv();(app.runtime/'hold-gateway').touch()
    answer,_=app.answer(project,source);wait_until(lambda:app.lines('fixture-model-calls.jsonl'))
    url=f"/api/projects/{project['id']}/answers/{answer['id']}"
    cancelled=app.client.post(url+'/cancel').json()
    assert cancelled['status']=='cancelled'
    (app.runtime/'hold-gateway').unlink();wait_until(lambda:app.lines('fixture-model-returns.jsonl'));time.sleep(.2)
    assert app.client.get(url).json()==cancelled
    assert len(app.lines('fixture-model-calls.jsonl'))==1
    EVENTS.append({'kind':'late_callback','status':'cancelled','late_fixture_return':True,
                   'terminal_snapshot_unchanged':True,'no_verifier_call':True,'real_model':False})


def test_gateway_initialization_failure_does_not_leave_queued_job(appcopy):
    app=appcopy;app.configure_fixture();app.command()
    project,source,_,_=app.import_csv();(app.runtime/'fail-gateway-constructor').touch()
    answer,_=app.answer(project,source)
    url=f"/api/projects/{project['id']}/answers/{answer['id']}"
    failed=wait_until(lambda:(d if (d:=app.client.get(url).json())['status']=='failed' else None))
    assert not app.lines('fixture-model-calls.jsonl') and failed['result'] is None
    EVENTS.append({'kind':'gateway_constructor_failure','status':failed['status'],'fixture_calls':0})


def test_initialization_exception_releases_directory_lock(tmp_path, monkeypatch):
    a=Service(tmp_path/'runtime',ROOT/'samples',workers=False)
    b=Service(tmp_path/'runtime',ROOT/'samples',workers=False)
    def broken():raise RuntimeError('fixture initialization failure')
    monkeypatch.setattr(a.store,'initialize',broken)
    with pytest.raises(RuntimeError,match='fixture'):a.start()
    try:
        b.start()
        assert a.instance_lock is None and b.instance_lock is not None
    finally:a.close();b.close()
    EVENTS.append({'kind':'initialization_failure','directory_lock_released':True})


def test_duplicate_service_start_preserves_held_lock(tmp_path):
    a=Service(tmp_path/'runtime',ROOT/'samples',workers=False)
    b=Service(tmp_path/'runtime',ROOT/'samples',workers=False)
    try:
        a.start()
        with pytest.raises(RuntimeError,match='已启动'):a.start()
        with pytest.raises(RuntimeError,match='另一个'):b.start()
        a.close();b.start()
    finally:a.close();b.close()
    EVENTS.append({'kind':'duplicate_service_start','lock_preserved_until_close':True})
