"""Portable start/stop. Never stops processes merely by executable name."""
from pathlib import Path
import hashlib, json, os, shutil, signal, socket, subprocess, sys, time, urllib.request, uuid, webbrowser

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / '02_数据资料' / '运行数据'
LOGS = DATA / '日志'
STATE = LOGS / '服务实例.json'
ENTRY = Path(__file__).with_name('start_server.py')
URL = 'http://127.0.0.1:8765'
WORKSPACE = hashlib.sha256(os.path.normcase(str(ROOT)).encode('utf-8')).hexdigest()[:24]

def health():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + '/api/health', timeout=1) as r: return json.load(r)
    except (OSError, ValueError): return {}

def ours(status):
    return bool(status.get('ok') and status.get('service') == 'evidenceflow'
                and status.get('instance', {}).get('workspace') == WORKSPACE)

def windows_process(pid, terminate=False, expected_created=None):
    """Use the SAME handle for image/creation checks and termination, avoiding PID reuse."""
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; k.OpenProcess.restype = w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.OpenProcess(0x1000 | (1 if terminate else 0), False, pid)
    if not handle: return None
    try:
        size = w.DWORD(32768); name = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)): return None
        if os.path.normcase(name.value) != os.path.normcase(str(Path(sys.executable).resolve())): return None
        times = [w.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)): return None
        created = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
        if terminate:
            if expected_created != created: return None
            if not k.TerminateProcess(handle, 0): raise OSError('无法停止本包的服务进程')
        return created
    finally: k.CloseHandle(handle)

def writable():
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        check = LOGS / ('.write-' + uuid.uuid4().hex)
        check.write_text('ok', encoding='utf-8'); check.unlink()
    except OSError:
        raise RuntimeError('目录不可写。请完整解压到自己的桌面或文档目录，再双击启动。') from None

def start():
    writable()
    with (LOGS / '启动锁').open('a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise RuntimeError('另一启动操作正在进行，请稍后再试') from None
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        current = health()
        if ours(current):
            print('证流已在运行。')
            if os.environ.get('EVIDENCEFLOW_NO_BROWSER') != '1': webbrowser.open(URL)
            return
        with socket.socket() as sock:
            sock.settimeout(.3)
            if sock.connect_ex(('127.0.0.1', 8765)) == 0:
                raise RuntimeError('8765 端口已被其他程序或另一份证流占用。请先停止对应服务。')
        if not (ROOT / '01_程序代码/frontend/dist/index.html').is_file():
            raise RuntimeError('网页文件缺失，请完整解压整个压缩包')
        samples = ROOT / '02_数据资料/示例数据'
        if not (DATA / 'business.db').exists(): shutil.copy2(samples / 'business.db', DATA / 'business.db')
        (DATA / 'docs').mkdir(exist_ok=True)
        for path in (samples / 'docs').iterdir():
            dest = DATA / 'docs' / path.name
            if path.is_file() and not dest.exists(): shutil.copy2(path, dest)
        instance = uuid.uuid4().hex
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                   EVIDENCEFLOW_DATA_DIR=str(DATA), EVIDENCEFLOW_WORKSPACE_ID=WORKSPACE,
                   EVIDENCEFLOW_INSTANCE_ID=instance)
        
        options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        with (LOGS / '服务日志.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(ENTRY)], cwd=ROOT,
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        record = {'pid': process.pid, 'instance': instance, 'workspace': WORKSPACE,
                  'created': windows_process(process.pid) if os.name == 'nt' else None}
        temp = STATE.with_suffix('.tmp'); temp.write_text(json.dumps(record), encoding='utf-8'); temp.replace(STATE)
        for _ in range(180):
            current = health()
            if ours(current) and current['instance']['id'] == instance:
                print('证流已启动：' + URL)
                if os.environ.get('EVIDENCEFLOW_NO_BROWSER') != '1': webbrowser.open(URL)
                return
            if process.poll() is not None: break
            time.sleep(.25)
        if process.poll() is None: process.terminate()
        raise RuntimeError('启动未完成。请将 02_数据资料/运行数据/日志/服务日志.log 发给开发队友排查。')

def stop():
    if not STATE.exists():
        print('当前包没有已记录的服务进程。'); return
    record = json.loads(STATE.read_text(encoding='utf-8')); pid = int(record['pid'])
    if record.get('workspace') != WORKSPACE: raise RuntimeError('实例目录已改变，请先从原目录停止服务')
    current = health()
    if current and (not ours(current) or current['instance'].get('id') != record['instance']):
        raise RuntimeError('端口当前属于其他服务，已拒绝停止')
    if os.name == 'nt':
        if not windows_process(pid, terminate=True, expected_created=record.get('created')):
            print('该实例已停止或进程不匹配；没有结束其他进程。')
    else:
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
        if str(ENTRY) in command:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
    for _ in range(50):
        h = health()
        if not ours(h) or h['instance'].get('id') != record['instance']: break
        time.sleep(.1)
    else: raise RuntimeError('服务尚未退出，请稍后再试；实例记录已保留')
    STATE.unlink(missing_ok=True)
    print('证流已停止。')

if __name__ == '__main__':
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {'start', 'stop'}: raise RuntimeError('用法：launcher.py start 或 stop')
        (start if sys.argv[1] == 'start' else stop)()
    except Exception as exc:
        print(str(exc), file=sys.stderr); sys.exit(1)
