"""Expand immutable Windows dependencies into the current user's cache, not the project tree."""
from pathlib import Path
import hashlib, io, json, os, shutil, sys, uuid, zipfile

def load_bundle(runtime, expected):
    bundle=runtime/'第三方依赖.zip'
    if bundle.is_file():blob=bundle.read_bytes()
    else:
        parts=sorted(runtime.glob('第三方依赖.zip.[0-9][0-9][0-9]'))
        if not parts or len(parts)>32 or [p.name for p in parts]!=['第三方依赖.zip.'+str(i).zfill(3) for i in range(1,len(parts)+1)]:
            raise RuntimeError('内置依赖缺失或分卷不完整，请完整下载源码或解压交接包')
        if any(p.is_symlink() for p in parts):raise RuntimeError('无效的依赖分卷')
        blob=b''.join(p.read_bytes() for p in parts)
    if hashlib.sha256(blob).hexdigest()!=expected:raise RuntimeError('内置依赖压缩包校验失败，请重新完整解压交接包')
    return blob

def activate():
    if os.name != 'nt': return
    root=Path(__file__).resolve().parents[2]
    runtime=root/'06_运行环境/Windows_x64'
    bundle=runtime/'第三方依赖.zip'
    expected=(runtime/'第三方依赖.sha256').read_text(encoding='utf-8').split()[0]
    blob=load_bundle(runtime,expected)
    actual=expected
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'EvidenceFlow'/'runtime'
    # Wheel licence paths may exceed Win32's legacy 260-character limit when
    # the user cache itself has a long path. Extended absolute paths work even
    # on Windows installations where long-path policy has not been enabled.
    absolute=str(base.resolve())
    if not absolute.startswith('\\\\?\\'):
        base=Path('\\\\?\\UNC\\'+absolute[2:] if absolute.startswith('\\\\') else '\\\\?\\'+absolute)
    cache=base/actual[:20]
    marker=cache/'.complete'
    if not marker.is_file() or marker.read_text(encoding='ascii')!=actual:
        base.mkdir(parents=True,exist_ok=True)
        staging=base/('extract-'+uuid.uuid4().hex)
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for item in z.infolist():
                    if item.filename.startswith(('/', '\\')) or '..' in Path(item.filename).parts:
                        raise RuntimeError('无效的依赖压缩路径')
                z.extractall(staging)
            (staging/'.complete').write_text(actual,encoding='ascii')
            try: staging.rename(cache)
            except OSError:
                # Windows may report WinError 5 when another process publishes
                # this identical cache first. Accept only its completed digest.
                if not marker.is_file() or marker.read_text(encoding='ascii')!=actual: raise
        finally:
            if staging.exists():
                staging.resolve().relative_to(base.resolve())
                if not staging.name.startswith('extract-'): raise RuntimeError('无效的临时缓存目录')
                shutil.rmtree(staging)
    sys.path.insert(0,str(cache))
    # Keep handles alive: Python 3.8+ native extensions use scoped DLL search paths.
    global _dll_paths
    _dll_paths=[os.add_dll_directory(str(runtime)),os.add_dll_directory(str(cache))]
