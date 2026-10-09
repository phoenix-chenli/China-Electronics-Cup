"""Rebuild the pinned Windows runtime; recipients need no pip or compilation."""
from pathlib import Path
import base64, concurrent.futures, csv, hashlib, io, json, ssl
import subprocess, sys, tempfile, urllib.request, zipfile
from seal_runtime import seal, safe_member
ROOT = Path(__file__).resolve().parents[2]
VERSION = '3.13.6'
PYTHON_URL = f'https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip'
PYTHON_SHA = 'd6ab71980c0be5809f2a0edd991e28d999e7ac971dc3b6da676dc2f80eac41dd'
CACHE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(tempfile.gettempdir())/'evidenceflow-build-cache'
RUNTIME = ROOT/'06_运行环境/Windows_x64'
LICENSES = ROOT/'05_比赛与交接/文件清单与许可证'
LOCK = ROOT/'01_程序代码/依赖清单/后端依赖锁定.txt'

def sha(data): return hashlib.sha256(data).hexdigest()

def fetch(url):
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError: context = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    with opener.open(url, timeout=60) as response: return response.read()

def normalize(name): return name.lower().replace('_', '-').replace('.', '-')

def verify_record(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None: raise ValueError('Invalid wheel ZIP: ' + path.name)
        for name in names: safe_member(name)
        record = next(n for n in names if n.endswith('.dist-info/RECORD'))
        for name, encoded, size in csv.reader(io.StringIO(archive.read(record).decode('utf-8'))):
            safe_member(name)
            if not encoded:
                if name != record: raise ValueError('Unhashed wheel member: ' + name)
                continue
            algorithm, digest = encoded.split('=', 1)
            if algorithm != 'sha256': raise ValueError('Unsupported wheel RECORD hash')
            data = archive.read(name)
            actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            if digest != actual or (size and int(size) != len(data)): raise ValueError('Wheel RECORD differs: ' + name)

def wheel_record(path, source_build=None):
    name, version = path.name.split('-')[:2]
    metadata = json.loads(fetch(f'https://pypi.org/pypi/{name}/{version}/json'))
    actual = sha(path.read_bytes()); release = next((x for x in metadata['urls'] if x['filename'] == path.name), None)
    if not path.name.endswith(('-win_amd64.whl', '-any.whl')): raise ValueError('Wrong wheel platform: ' + path.name)
    verify_record(path)
    body = {'name': metadata['info']['name'], 'version': version, 'filename': path.name,
            'sha256': actual, 'license': metadata['info'].get('license_expression') or metadata['info'].get('license'),
            'project_urls': metadata['info'].get('project_urls', {}), 'record_verified': True}
    if release:
        if release['digests']['sha256'] != actual: raise ValueError('PyPI wheel SHA256 differs: ' + path.name)
        body.update(url=release['url'], distribution='official_pypi_wheel')
    elif normalize(name) in {'jieba','antlr4-python3-runtime'} and source_build:
        body.update(distribution='locally_built_wheel_from_verified_pypi_sdist', url=None, source_build=source_build)
    else: raise ValueError('Wheel is not an official release and has no verified source build: ' + path.name)
    return body

def build_source(name, version, wheels, cache):
    # These two locked pure-Python releases have no official wheel.
    metadata = json.loads(fetch(f'https://pypi.org/pypi/{name}/{version}/json'))
    release = next(x for x in metadata['urls'] if x['packagetype'] == 'sdist')
    source = cache/release['filename']
    if not source.exists(): source.write_bytes(fetch(release['url']))
    if sha(source.read_bytes()) != release['digests']['sha256']: raise ValueError('Official source SHA256 differs: '+name)
    args = [sys.executable, '-m', 'pip', 'wheel', '--no-deps', str(source), '--wheel-dir', str(wheels)]
    subprocess.run(args, check=True)
    return {'filename': release['filename'], 'url': release['url'], 'sha256': release['digests']['sha256'],
            'method': 'pip wheel --no-deps after official PyPI sdist SHA256 validation',
            'build_python': sys.version.split()[0], 'official_wheel': False}

def main():
    CACHE.mkdir(parents=True, exist_ok=True); wheels = CACHE/'wheels'; wheels.mkdir(exist_ok=True)
    locks = [line.strip() for line in LOCK.read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#')]
    binary_lock = CACHE/'binary-requirements.txt'
    source_names={'jieba','antlr4-python3-runtime'}
    binary_lock.write_text('\n'.join(x for x in locks if normalize(x.split('==')[0]) not in source_names)+'\n', encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--platform', 'win_amd64',
                    '--python-version', '313', '--implementation', 'cp', '--abi', 'cp313', '--abi', 'abi3', '--abi', 'none',
                    '-r', str(binary_lock), '-d', str(wheels)], check=True)
    source_builds={normalize(x.split('==')[0]):build_source(*x.split('=='),wheels,CACHE) for x in locks if normalize(x.split('==')[0]) in source_names}
    wanted = {normalize(x.split('==')[0]): x.split('==')[1] for x in locks}
    paths = [p for p in sorted(wheels.glob('*.whl')) if wanted.get(normalize(p.name.split('-')[0])) == p.name.split('-')[1]]
    if len(paths) != len(locks): raise ValueError('Missing or ambiguous locked wheels')
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: records = list(pool.map(lambda p: wheel_record(p, source_builds.get(normalize(p.name.split('-')[0]))), paths))
    archive = CACHE/'python.zip'
    if not archive.exists(): archive.write_bytes(fetch(PYTHON_URL))
    blob = archive.read_bytes()
    if sha(blob) != PYTHON_SHA: raise ValueError('Official Python archive SHA256 differs')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as embedded:
        for name in embedded.namelist(): safe_member(name)
        embedded.extractall(RUNTIME)
    sealed = seal(wheels=paths)
    report = {'python': {'version': VERSION, 'platform': 'win_amd64', 'url': PYTHON_URL, 'sha256': sha(blob),
                         'license': '06_运行环境/Windows_x64/LICENSE.txt'},
              'wheels': records, 'windows_runtime_tested': False, 'sealed_dependencies': sealed,
              'method': 'Official Python ZIP and binary wheels SHA256 verified; Jieba and ANTLR built from SHA256-verified official pure Python sources; every wheel RECORD verified.'}
    LICENSES.mkdir(parents=True, exist_ok=True)
    (LICENSES/'Windows依赖来源与摘要.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'python': VERSION, 'verified_distributions': len(records), **sealed}))

if __name__ == '__main__': main()
