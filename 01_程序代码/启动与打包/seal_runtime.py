"""Seal vendor files to keep the delivered directory shallow; does not run Windows code."""
from pathlib import Path
import hashlib,json,shutil,zipfile
ROOT=Path(__file__).resolve().parents[2]
def main():
    runtime=ROOT/'06_运行环境/Windows_x64';site=runtime/'Lib/site-packages'
    if not (runtime/'msvcp140.dll').is_file():raise RuntimeError('先按开发说明补齐 Microsoft x64 C++ 运行库')
    bundle=runtime/'第三方依赖.zip'
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(site.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(site).as_posix())
    with zipfile.ZipFile(bundle) as z:
        assert z.testzip() is None
        for p in site.rglob('*'):
            if p.is_file():assert z.read(p.relative_to(site).as_posix())==p.read_bytes()
    (runtime/'第三方依赖.sha256').write_text(hashlib.sha256(bundle.read_bytes()).hexdigest()+'  第三方依赖.zip\n',encoding='utf-8')
    # The canonical copy is now verified in the ZIP; remove the expanded build tree.
    shutil.rmtree(runtime/'Lib')
    (runtime/'python313._pth').write_text('python313.zip\n.\nimport site\n',encoding='ascii')
    print('依赖封装完成；Windows 未运行。')
if __name__=='__main__':main()
