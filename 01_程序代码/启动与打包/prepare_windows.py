"""Assemble Windows binaries without executing them. Run with native Python 3.13."""
from pathlib import Path
import concurrent.futures, hashlib, json, ssl, subprocess, sys, tempfile, urllib.request, zipfile

ROOT=Path(__file__).resolve().parents[2]
VERSION='3.13.6'
PYTHON_URL=f'https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip'
CACHE=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(tempfile.gettempdir())/'evidenceflow-build-cache'
RUNTIME=ROOT/'06_运行环境/Windows_x64'
LICENSES=ROOT/'05_比赛与交接/文件清单与许可证'
LOCK=ROOT/'01_程序代码/依赖清单/后端依赖锁定.txt'

def sha(data):return hashlib.sha256(data).hexdigest()
def fetch(url):
    try:
        import certifi
        context=ssl.create_default_context(cafile=certifi.where())
    except ImportError:context=ssl.create_default_context()
    with urllib.request.urlopen(url,context=context,timeout=60) as r:return r.read()
def main():
    CACHE.mkdir(parents=True,exist_ok=True)
    wheels=CACHE/'wheels';wheels.mkdir(exist_ok=True)
    # pip is a build-time tool only; it is never invoked on the recipient's machine.
    if not list(wheels.glob('*.whl')):
        subprocess.run([sys.executable,'-m','pip','download','--only-binary=:all:','--platform','win_amd64',
          '--python-version','313','--implementation','cp','--abi','cp313','--abi','abi3','--abi','none',
          '-r',str(LOCK),'-d',str(wheels)],check=True)
    archive=CACHE/'python.zip'
    if not archive.exists():archive.write_bytes(fetch(PYTHON_URL))
    blob=archive.read_bytes()
    if hashlib.md5(blob).hexdigest()!='31019bd49a54b8d3bfa7eb1093d4b8e5':
        raise ValueError('Python archive differs from the official release checksum')
    RUNTIME.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:z.extractall(RUNTIME)
    site=RUNTIME/'Lib/site-packages';site.mkdir(parents=True,exist_ok=True)
    records=[]
    def wheel_record(path):
        name,version=path.name.split('-')[:2]
        metadata=json.loads(fetch(f'https://pypi.org/pypi/{name}/{version}/json'))
        release=next(x for x in metadata['urls'] if x['filename']==path.name)
        actual=sha(path.read_bytes())
        if release['digests']['sha256']!=actual:raise ValueError('Wheel SHA256 mismatch: '+path.name)
        if not path.name.endswith(('-win_amd64.whl','-any.whl')):raise ValueError('Wrong wheel platform')
        return {'name':metadata['info']['name'],'version':version,'filename':path.name,'url':release['url'],
          'sha256':actual,'license':metadata['info'].get('license_expression') or metadata['info'].get('license'),
          'project_urls':metadata['info'].get('project_urls',{})}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        records=list(pool.map(wheel_record,sorted(wheels.glob('*.whl'))))
    for rec in records:
        with zipfile.ZipFile(wheels/rec['filename']) as z:
            for item in z.infolist():
                if item.is_dir():continue
                parts=Path(item.filename).parts
                if '..' in parts or Path(item.filename).is_absolute():raise ValueError('Unsafe wheel member')
                if parts[0].endswith('.data'):
                    if parts[1] not in {'purelib','platlib'}:continue
                    parts=parts[2:]
                dest=site.joinpath(*parts);dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(z.read(item))
    # Keep the embedded interpreter isolated. The service entry adds only its own code directory.
    (RUNTIME/'python313._pth').write_text('python313.zip\n.\nLib/site-packages\nimport site\n',encoding='ascii')
    LICENSES.mkdir(parents=True,exist_ok=True)
    report={'python':{'version':VERSION,'platform':'win_amd64','url':PYTHON_URL,'sha256':sha(blob),
                     'license':'06_运行环境/Windows_x64/LICENSE.txt'},
            'wheels':records,'windows_runtime_tested':False,
            'method':'Official Python ZIP MD5 and PyPI SHA256 verified; extraction only, no Windows execution.'}
    (LICENSES/'Windows依赖来源与摘要.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    exact='\n'.join(sorted(r['name']+'=='+r['version'] for r in records))+'\n'
    LOCK.write_text(exact,encoding='utf-8')
    print(json.dumps({'python':VERSION,'verified_wheels':len(records),'files':sum(p.is_file() for p in RUNTIME.rglob('*'))}))
if __name__=='__main__':main()
