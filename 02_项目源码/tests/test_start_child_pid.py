"""Verify Windows-style launcher/service PID separation without spawning OS processes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('child_pid_launcher',Path(__file__).resolve().parents[1]/'start.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


def env(tmp_path,monkeypatch,reported):
    runtime=tmp_path/'runtime'
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    monkeypatch.setattr(launcher,'RUNTIME',runtime)
    monkeypatch.setattr(launcher,'prepare',lambda *_:None)
    monkeypatch.setattr(launcher,'port_open',lambda *_:False)
    monkeypatch.setattr(launcher,'saved_identity',lambda *_:None)
    monkeypatch.setattr(launcher.time,'sleep',lambda *_:None)
    first=[True]
    def health(port):
        if first[0]:first[0]=False;return None
        return reported
    monkeypatch.setattr(launcher,'health',health)
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(pid=101,poll=lambda:None))
    return runtime,SimpleNamespace(stop=False,prepare_only=False,rebuild=False,port=8899,no_browser=True)


def test_health_service_pid_is_saved_and_used_for_stop(tmp_path,monkeypatch):
    info={'service':'zhiheng','workspace':str(tmp_path),'pid':202}
    runtime,args=env(tmp_path,monkeypatch,info)
    identity={'pid':202,'port':8899,'workspace':str(tmp_path),'started':123,'command':['python','-m','backend.local_server']}
    checked=[]
    def verify(pid,port):
        checked.append(pid);return identity if pid==202 else None
    monkeypatch.setattr(launcher,'process_identity',verify)
    assert launcher.run(args)==0
    record=json.loads((runtime/'server-instance.json').read_text())
    assert record['identity']['pid']==202 and record['token']
    assert checked==[202]  # Popen's venv-launcher PID is not a service identity.
    stopped=[]
    monkeypatch.setattr(launcher,'terminate',lambda value:stopped.append(value) or False)
    assert launcher.stop(args.port)==0
    assert stopped==[identity] and not (runtime/'server-instance.json').exists()


@pytest.mark.parametrize('owned_workspace',[False,True])
def test_unverified_health_never_stops_that_reported_pid(tmp_path,monkeypatch,owned_workspace):
    info={'service':'zhiheng','workspace':str(tmp_path) if owned_workspace else '/another-workspace','pid':999}
    runtime,args=env(tmp_path,monkeypatch,info)
    monkeypatch.setattr(launcher,'process_identity',lambda *_:None)
    ended=[]
    class OwnedProcess:
        def __init__(self,pid):self.pid=pid
        def children(self,recursive=True):return [OwnedProcess(202)]
        def terminate(self):ended.append(self.pid)
    waited=[]
    monkeypatch.setattr(launcher,'process_tools',lambda:SimpleNamespace(Process=OwnedProcess,Error=RuntimeError,wait_procs=lambda targets,timeout:waited.extend(p.pid for p in targets)))
    assert launcher.run(args)==1
    assert ended==[202,101] and waited==[202,101]
    assert 999 not in ended and not (runtime/'server-instance.json').exists()
