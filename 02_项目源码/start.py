"""知衡本地启动器。Windows/macOS 均只管理身份核验属于本目录的服务。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import platform
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
import secrets
from backend.platform_io import FileLock

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
PYTHON = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

SQLITE_PREFLIGHT = '''
import sqlite3
def strict_sqlite_dqs():
    try:
        with sqlite3.connect(':memory:') as connection:
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            try:
                connection.execute('SELECT "__zhiheng_unknown_field__"').fetchone()
            except sqlite3.OperationalError:
                return True
    except (AttributeError, sqlite3.Error):
        pass
    return False
'''


def health(port: int) -> dict | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else None
    except Exception:
        return None


def is_ours(info: dict | None) -> bool:
    return bool(isinstance(info, dict) and info.get('service') == 'zhiheng' and info.get('workspace') == str(ROOT))


def process_identity(pid: int, port: int) -> dict | None:
    """Confirm the OS process, not just a PID supplied by an HTTP response."""
    if type(pid) is not int or pid <= 1:
        return None
    psutil = process_tools()
    try:
        process = psutil.Process(pid)
        words = process.cmdline()
        if Path(process.cwd()).resolve() != ROOT.resolve():
            return None
        legacy = 'backend.main:app' in words and 'uvicorn' in words
        managed = 'backend.local_server' in words
        if not (legacy or managed) or '-m' not in words:
            return None
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        if words[words.index('--port') + 1] != str(port) or words[words.index('--host') + 1] != '127.0.0.1':
            return None
        return {'pid': pid, 'port': port, 'workspace': str(ROOT),
                'started': process.create_time(), 'command': words}
    except (psutil.Error, OSError, ValueError, IndexError):
        return None


def process_alive(pid: int) -> bool:
    psutil = process_tools()
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def process_tools():
    try:
        import psutil
        return psutil
    except ImportError as exc:
        raise RuntimeError('缺少锁定的进程管理依赖，请先执行 start.py --prepare-only。') from exc


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.3):
            return True
    except OSError:
        return False


@contextmanager
def launcher_lock():
    try:
        with FileLock(RUNTIME / 'launcher.lock', timeout=15):
            yield
    except BlockingIOError as exc:
        raise RuntimeError('另一个启动或停止操作仍在进行，请稍后重试。') from exc


def saved_identity(port: int) -> dict | None:
    try:
        record = json.loads((RUNTIME / 'server-instance.json').read_text(encoding='utf-8'))
        identity = record.get('identity', record)
        if identity.get('port') == port and process_identity(identity.get('pid'), port) == identity:
            return identity
    except (OSError, ValueError, AttributeError):
        pass
    return None


def terminate(identity: dict) -> bool:
    """Wait for real exit; a closed listening socket is not an exited worker."""
    pid, port = identity['pid'], identity['port']
    if process_identity(pid, port) != identity:
        raise RuntimeError('服务进程身份已变化，未发送停止信号。')
    psutil = process_tools()
    process = psutil.Process(pid)
    if process.create_time() != identity['started']:
        raise RuntimeError('服务PID已被其他进程复用，未发送停止操作。')
    # Capture child identities before the parent can exit/reparent them.
    children = process.children(recursive=True)
    managed = 'backend.local_server' in identity['command']
    if managed:
        try:
            record = json.loads((RUNTIME / 'server-instance.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            record = {}
        if record.get('identity') != identity or not record.get('token'):
            raise RuntimeError('缺少本次实例的停止令牌，未发送停止操作。')
        request = RUNTIME / 'server-stop.json'
        temporary = request.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid':pid,'token':record['token']}), encoding='utf-8')
        temporary.replace(request)
    else:
        # Legacy POSIX instances can still shut down cleanly. psutil protects
        # against PID reuse; no port-based taskkill or broad process-name kill.
        process.terminate()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return False
        time.sleep(.1)
    if process_identity(pid, port) != identity:
        if not process_alive(pid):
            return False
        raise RuntimeError('退出期间进程身份发生变化，未继续发送信号。')
    # psutil Process objects retain create_time and guard kill against PID reuse.
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    process.kill()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(.1)
    raise RuntimeError('服务进程尚未退出，未启动第二个实例。')


def dependency_locks():
    names = ['requirements.lock.txt','requirements.retrieval.lock.txt']
    if os.name == 'nt':
        names.append('requirements.windows.lock.txt')
    return [ROOT / 'backend' / name for name in names
            if (ROOT / 'backend' / name).is_file()]


def check_environment():
    """Read-only and credential-free: never prints model.json contents."""
    report = {'platform':platform.system(),'python':platform.python_version(),'workspace':str(ROOT),
              'isolated_python_exists':PYTHON.exists(),'lifecycle_platform_supported':os.name in {'posix','nt'},
              'frontend_built':(ROOT/'frontend/dist/index.html').is_file(),
              'node_available':bool(shutil.which('node')),'npm_available':bool(shutil.which('npm')),
              'windows_runtime_verified':False}
    if PYTHON.exists():
        script = SQLITE_PREFLIGHT + """import json,sys,importlib
from pathlib import Path
from backend.reliability import locked_dependencies
from backend.ocr_adapter import capabilities
from backend.model_gateway import ModelConfig,ModelError
try:
    ModelConfig(Path(sys.argv[1])).load();configured=True
except ModelError:configured=False
native={}
for module in ('onnxruntime','pymupdf','tokenizers','numpy','psutil'):
    try:
        importlib.import_module(module);native[module]={'imported':True}
    except (ImportError,OSError) as exc:
        native[module]={'imported':False,'error_type':type(exc).__name__}
print(json.dumps({'locks':[dict(file=Path(p).name,**locked_dependencies(p)) for p in sys.argv[2:]],
                  'ocr':capabilities(),'model_configured':configured,
                  'native_modules':native,
                  'strict_sqlite_dqs':strict_sqlite_dqs()}))
"""
        response = subprocess.run([str(PYTHON),'-c',script,str(RUNTIME),*[str(p) for p in dependency_locks()]],
                                  cwd=ROOT,capture_output=True,text=True,timeout=15)
        if response.returncode == 0:
            report.update(json.loads(response.stdout))
        else:
            report['preflight_error']='独立环境无法完成依赖检查；请运行 --prepare-only'
    return report


def prepare(rebuild: bool) -> None:
    if sys.version_info < (3,12):
        raise RuntimeError('需要 Python 3.12 或以上及支持 strict DQS 的 SQLite；当前解释器版本过旧。')
    if not PYTHON.exists():
        print('首次启动：创建知衡独立 Python 环境。', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
    strict = subprocess.run([str(PYTHON),'-c',SQLITE_PREFLIGHT+'\nimport sys;sys.exit(not strict_sqlite_dqs())'],capture_output=True)
    if strict.returncode:
        raise RuntimeError('独立环境的 Python/SQLite 不支持 strict DQS；请使用 Python 3.12 或以上重建 .venv。')
    locks = dependency_locks()
    if len(locks) != (3 if os.name == 'nt' else 2):
        raise RuntimeError('缺少精确依赖锁，未自动安装浮动版本。')
    check = 'from backend.reliability import locked_dependencies;import sys;sys.exit(not all(locked_dependencies(p)["ok"] for p in sys.argv[1:]))'
    ready = subprocess.run([str(PYTHON),'-c',check,*[str(p) for p in locks]],cwd=ROOT,capture_output=True)
    if ready.returncode:
        print('按精确锁准备本地服务与检索依赖。', flush=True)
        subprocess.run([str(PYTHON),'-m','pip','install',*[argument for lock in locks for argument in ('-r',str(lock))]],check=True)
    native = subprocess.run([str(PYTHON),'-c','import onnxruntime,pymupdf,tokenizers,numpy,psutil'],
                            cwd=ROOT,capture_output=True)
    if native.returncode:
        raise RuntimeError('原生依赖无法加载。Windows请检查Python x64与Microsoft Visual C++ 2015–2022 x64运行库；详见快速启动说明。')
    subprocess.run([str(PYTHON),'-m','pip','check'],check=True,capture_output=True)
    front = ROOT / 'frontend'
    entry = front / 'dist' / 'index.html'
    # ZIP timestamps are not build evidence. Published dist runs without Node;
    # source developers explicitly request --rebuild after frontend changes.
    if rebuild or not entry.exists():
        npm = shutil.which('npm')
        if not npm:
            raise RuntimeError('需要 Node.js 和 npm 构建界面。请先安装 Node.js，再重新启动。')
        if not (front / 'node_modules').exists():
            print('首次启动：安装界面依赖。', flush=True)
            subprocess.run([npm, 'ci'], cwd=front, check=True)
        print('构建知衡界面…', flush=True)
        subprocess.run([npm, 'run', 'build'], cwd=front, check=True)


def stop(port: int) -> int:
    info = health(port)
    if info and not is_ours(info):
        print('端口由其他服务使用，未对它执行任何停止操作。')
        return 1
    identity = process_identity(info.get('pid'), port) if info else saved_identity(port)
    if not identity:
        if info or port_open(port):
            print('端口进程身份未确认，未执行停止操作。')
            return 1
        print('此目录没有仍在运行的知衡服务，无需停止。')
        return 0
    forced = terminate(identity)
    (RUNTIME / 'server-instance.json').unlink(missing_ok=True)
    print('知衡已停止。资料和项目仍保存在 .runtime 中。')
    if forced:
        print('解析未及时退出，已结束本次服务及其子进程；中断导入会在重启后恢复，问答不会自动重发。')
    return 0


def run(args) -> int:
    if args.stop:
        return stop(args.port)
    if getattr(args,'prepare_only',False):
        prepare(args.rebuild)
        print('依赖和界面已准备；未启动服务、未发送模型请求。')
        return 0
    prepare(args.rebuild)
    url = f'http://127.0.0.1:{args.port}'
    info = health(args.port)
    if info:
        if not is_ours(info) or not process_identity(info.get('pid'), args.port):
            print(f'{args.port} 已有其他服务，请指定 --port 使用另一个端口。')
            return 1
        print(f'知衡已在运行：{url}')
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if port_open(args.port):
        print(f'{args.port} 已由无法识别的服务占用，未启动或停止任何进程。')
        return 1
    if saved_identity(args.port):
        print('本目录服务仍在启动或退出，未重复启动。可先执行 --stop。')
        return 1
    RUNTIME.mkdir(exist_ok=True)
    log_path = RUNTIME / 'server.log'
    with log_path.open('ab') as log:
        env = os.environ.copy()
        env['ZHIHENG_DATA_DIR'] = str(RUNTIME)
        token = secrets.token_hex(32)
        env['ZHIHENG_SERVER_TOKEN'] = token
        options = {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session':True}
        process = subprocess.Popen(
            [str(PYTHON), '-m', 'backend.local_server', '--host', '127.0.0.1', '--port', str(args.port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **options,
        )
    for _ in range(100):
        if process.poll() is not None:
            print(f'服务未能启动，请查看日志：{log_path}')
            return 1
        identity = process_identity(process.pid, args.port)
        if identity:
            (RUNTIME / 'server-instance.json').write_text(json.dumps({'identity':identity,'token':token}, ensure_ascii=False), encoding='utf-8')
        info = health(args.port)
        if is_ours(info) and info.get('pid') == process.pid:
            identity = process_identity(process.pid, args.port)
            if not identity:
                continue
            print(f'知衡已启动：{url}\n运行目录：{RUNTIME}\n服务进程：{process.pid}')
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        time.sleep(.2)
    identity = process_identity(process.pid, args.port)
    if identity:
        terminate(identity)
    print(f'启动未能确认，已结束本次可识别进程。请查看：{log_path}')
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='知衡 · 多模态可解释问数问答智能体')
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--check', action='store_true', help='只读检查依赖、OCR与配置，不启动或安装')
    parser.add_argument('--prepare-only', action='store_true', help='按锁安装依赖并准备界面，不启动服务')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('端口必须在 1024 到 65535 之间')
    if args.check:
        print(json.dumps(check_environment(),ensure_ascii=False,indent=2))
        return 0
    # The console's Python may be a different install. All process management
    # executes in the project environment where exact dependencies are installed.
    if PYTHON.exists() and Path(sys.prefix).resolve() != (ROOT / '.venv').resolve():
        return subprocess.call([str(PYTHON), str(ROOT / 'start.py'), *sys.argv[1:]], cwd=ROOT)
    if not PYTHON.exists() and not args.stop:
        with launcher_lock():
            prepare(args.rebuild)
        return subprocess.call([str(PYTHON), str(ROOT / 'start.py'), *sys.argv[1:]], cwd=ROOT)
    with launcher_lock():
        return run(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'启动未完成：{exc}')
        raise SystemExit(1)
