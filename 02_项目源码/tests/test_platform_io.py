import errno
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from backend import platform_io
from backend.service import Service

spec = importlib.util.spec_from_file_location('platform_launcher', Path(__file__).resolve().parents[1] / 'start.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_lock_contention_and_exception_release(tmp_path):
    path = tmp_path / '中文 space' / 'instance.lock'
    with pytest.raises(ValueError):
        with platform_io.FileLock(path):
            with pytest.raises(BlockingIOError):
                platform_io.FileLock(path).acquire()
            raise ValueError('controlled failure')
    with platform_io.FileLock(path):
        assert path.is_file()


def test_lock_rejects_link(tmp_path):
    target = tmp_path / 'target'; target.touch()
    path = tmp_path / 'lock'
    try:
        path.symlink_to(target)
    except OSError:
        if os.name == 'nt':
            pytest.skip('Windows symlink creation requires Developer Mode or privilege')
        raise
    with pytest.raises(OSError):
        platform_io.FileLock(path).acquire()


def test_windows_byte_range_adapter_rewinds_and_propagates_real_errors(monkeypatch):
    calls = []
    fake_os = SimpleNamespace(name='nt', SEEK_SET=0, lseek=lambda *args:calls.append(('seek',args)))
    fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda *args:calls.append(('lock',args)))
    monkeypatch.setattr(platform_io, 'os', fake_os)
    monkeypatch.setitem(sys.modules, 'msvcrt', fake)
    assert platform_io.try_lock_fd(42)
    platform_io.unlock_fd(42)
    assert calls == [('seek',(42,0,0)),('lock',(42,2,1)),('seek',(42,0,0)),('lock',(42,0,1))]
    def blocked(*_): raise OSError(errno.EACCES, 'held')
    fake.locking = blocked
    assert not platform_io.try_lock_fd(42)
    def broken(*_): raise OSError(errno.EBADF, 'bad descriptor')
    fake.locking = broken
    with pytest.raises(OSError): platform_io.try_lock_fd(42)


def test_service_instances_cannot_share_runtime(tmp_path):
    first = Service(tmp_path/'runtime',tmp_path/'samples',workers=False)
    second = Service(tmp_path/'runtime',tmp_path/'samples',workers=False)
    first.start()
    try:
        with pytest.raises(RuntimeError,match='另一个知衡进程'):
            second.start()
    finally:
        first.close()
    second.start();second.close()


def test_process_identity_uses_cwd_command_port_and_birthtime(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    command = ['python','-m','backend.local_server','--host','127.0.0.1','--port','8876']
    process = SimpleNamespace(cmdline=lambda:command,cwd=lambda:str(tmp_path),status=lambda:'running',create_time=lambda:100.25)
    module = SimpleNamespace(Process=lambda _:process,STATUS_ZOMBIE='zombie',Error=RuntimeError)
    monkeypatch.setattr(launcher,'process_tools',lambda:module)
    identity = launcher.process_identity(123,8876)
    assert identity['started']==100.25
    assert launcher.process_identity(123,8877) is None
    process.cwd=lambda:str(tmp_path/'other')
    assert launcher.process_identity(123,8876) is None


def test_stale_identity_never_sends_stop(monkeypatch):
    monkeypatch.setattr(launcher,'process_identity',lambda *args:{'changed':True})
    monkeypatch.setattr(launcher,'process_tools',lambda:pytest.fail('must not access termination API'))
    with pytest.raises(RuntimeError,match='身份已变化'):
        launcher.terminate({'pid':123,'port':8876})


def test_pid_reuse_between_identity_check_and_process_handle_is_rejected(monkeypatch):
    identity = {'pid':123,'port':8876,'started':1}
    monkeypatch.setattr(launcher,'process_identity',lambda *args:identity)
    monkeypatch.setattr(launcher,'process_tools',lambda:SimpleNamespace(
        Process=lambda _:SimpleNamespace(create_time=lambda:2)))
    with pytest.raises(RuntimeError,match='PID已被其他进程复用'):
        launcher.terminate(identity)


def test_packaged_dist_never_needs_node_due_to_timestamps(tmp_path,monkeypatch):
    (tmp_path/'backend').mkdir();(tmp_path/'frontend/dist').mkdir(parents=True)
    (tmp_path/'frontend/dist/index.html').write_text('dist')
    (tmp_path/'frontend/package.json').write_text('{}')
    for name in ('requirements.lock.txt','requirements.retrieval.lock.txt'):
        (tmp_path/'backend'/name).write_text('example==1.0')
    python=tmp_path/'python';python.touch()
    monkeypatch.setattr(launcher,'ROOT',tmp_path);monkeypatch.setattr(launcher,'PYTHON',python)
    monkeypatch.setattr(launcher.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher.shutil,'which',lambda *_:pytest.fail('Node must not be needed for published dist'))
    launcher.prepare(False)
