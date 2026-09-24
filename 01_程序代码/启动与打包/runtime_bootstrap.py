"""Expand immutable Windows dependencies into the current user's cache, not the project tree."""
from pathlib import Path
import hashlib, json, os, shutil, sys, uuid, zipfile

def activate():
    if os.name != 'nt': return
    root=Path(__file__).resolve().parents[2]
    runtime=root/'06_运行环境/Windows_x64'
    bundle=runtime/'第三方依赖.zip'
    expected=(runtime/'第三方依赖.sha256').read_text(encoding='utf-8').split()[0]
    actual=hashlib.sha256(bundle.read_bytes()).hexdigest()
    if expected!=actual: raise RuntimeError('内置依赖压缩包校验失败，请重新完整解压交接包')
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'EvidenceFlow'/'runtime'
    cache=base/actual[:20]
    marker=cache/'.complete'
    if not marker.is_file() or marker.read_text(encoding='ascii')!=actual:
        base.mkdir(parents=True,exist_ok=True)
        staging=base/('extract-'+uuid.uuid4().hex)
        try:
            with zipfile.ZipFile(bundle) as z:
                for item in z.infolist():
                    if item.filename.startswith(('/', '\\')) or '..' in Path(item.filename).parts:
                        raise RuntimeError('无效的依赖压缩路径')
                z.extractall(staging)
            (staging/'.complete').write_text(actual,encoding='ascii')
            try: staging.rename(cache)
            except FileExistsError:
                if not marker.is_file(): raise RuntimeError('依赖缓存不完整，请删除 %LOCALAPPDATA%/EvidenceFlow/runtime 后重试')
        finally:
            if staging.exists(): shutil.rmtree(staging)
    sys.path.insert(0,str(cache))
    # Keep handles alive: Python 3.8+ native extensions use scoped DLL search paths.
    global _dll_paths
    _dll_paths=[os.add_dll_directory(str(runtime)),os.add_dll_directory(str(cache))]
