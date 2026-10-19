import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('zhiheng_launcher',Path(__file__).resolve().parents[1]/'start.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


def test_preflight_missing_environment_reads_only(tmp_path,monkeypatch):
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    monkeypatch.setattr(launcher,'PYTHON',tmp_path/'not-installed-python')
    result=launcher.check_environment()
    assert not result['isolated_python_exists']
    assert not list(tmp_path.iterdir())
    assert not result['windows_runtime_verified']


def test_locked_dependency_mismatch_installs_exact_both_locks(tmp_path,monkeypatch):
    (tmp_path/'backend').mkdir();(tmp_path/'frontend/dist').mkdir(parents=True)
    (tmp_path/'frontend/dist/index.html').write_text('packaged interface')
    for name in ('requirements.lock.txt','requirements.retrieval.lock.txt'):(tmp_path/'backend'/name).write_text('example==1.0')
    python=tmp_path/'python';python.touch()
    monkeypatch.setattr(launcher,'ROOT',tmp_path);monkeypatch.setattr(launcher,'PYTHON',python)
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1 if any('locked_dependencies' in str(part) for part in command) else 0)
    monkeypatch.setattr(launcher.subprocess,'run',run)
    launcher.prepare(False)
    installs=[c for c in commands if 'install' in c]
    assert len(installs)==1 and installs[0].count('-r')==2
    assert 'pip' in commands[-1] and 'check' in commands[-1]


def test_no_unpinned_fallback_when_lock_missing(tmp_path,monkeypatch):
    python=tmp_path/'python';python.touch()
    monkeypatch.setattr(launcher,'ROOT',tmp_path);monkeypatch.setattr(launcher,'PYTHON',python)
    monkeypatch.setattr(launcher.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError,match='精确依赖锁'):launcher.prepare(False)


def test_unsupported_sqlite_stops_before_install_build_or_start(tmp_path,monkeypatch):
    python=tmp_path/'python';python.touch()
    monkeypatch.setattr(launcher,'ROOT',tmp_path);monkeypatch.setattr(launcher,'PYTHON',python)
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(launcher.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='strict DQS'):launcher.prepare(False)
    assert len(commands)==1 and 'SQLITE_DBCONFIG_DQS_DML' in commands[0][-1]


def test_old_python_stops_before_environment_creation(tmp_path,monkeypatch):
    monkeypatch.setattr(launcher,'ROOT',tmp_path);monkeypatch.setattr(launcher,'PYTHON',tmp_path/'missing-python')
    monkeypatch.setattr(launcher.sys,'version_info',(3,11,9))
    monkeypatch.setattr(launcher.subprocess,'run',lambda *args,**kwargs:pytest.fail('must fail before launching a child'))
    with pytest.raises(RuntimeError,match='Python 3.12'):launcher.prepare(False)
    assert not list(tmp_path.iterdir())
