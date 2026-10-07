"""Seal verified vendor files without expanding a dependency tree in the source."""
from pathlib import Path, PurePosixPath
import hashlib, shutil, tempfile, zipfile
ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / '06_运行环境/Windows_x64'

def safe_member(name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith(('/', '\\')) or '\\' in name or '..' in parts or ':' in parts[0]:
        raise ValueError('Unsafe dependency ZIP member: ' + name)
    return parts

def wheel_members(path):
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())): raise ValueError('Duplicate wheel member')
        for item in archive.infolist():
            if item.is_dir(): continue
            parts = safe_member(item.filename)
            if parts[0].endswith('.data'):
                if len(parts) < 3 or parts[1] not in {'purelib', 'platlib'}: continue
                parts = parts[2:]
            yield '/'.join(parts), archive.read(item)

def split_bundle(bundle):
    # Source-control blobs stay below GitHub's per-file limit. RAR keeps the
    # canonical ZIP; the bootstrap validates the same digest after concatenation.
    names=[]
    with bundle.open('rb') as stream:
        index=1
        while block:=stream.read(60*1024*1024):
            part=bundle.with_name(bundle.name+'.'+str(index).zfill(3))
            part.write_bytes(block);names.append(part.name);index+=1
    for old in bundle.parent.glob(bundle.name+'.[0-9][0-9][0-9]'):
        if old.name not in names:
            old.resolve().relative_to(bundle.parent.resolve());old.unlink()
    return names

def seal(*, wheels=(), preserve_existing=False, extra_files=None):
    if not (RUNTIME / 'msvcp140.dll').is_file(): raise RuntimeError('先按开发说明补齐 Microsoft x64 C++ 运行库')
    site = RUNTIME / 'Lib/site-packages'; bundle = RUNTIME / '第三方依赖.zip'
    additions = {}
    for path in wheels:
        for name, data in wheel_members(Path(path)):
            if name in additions and additions[name] != data: raise ValueError('Conflicting wheel file: ' + name)
            additions[name] = data
    for name, data in (extra_files or {}).items():
        safe_member(name); additions[name] = data
    if site.is_dir():
        for path in sorted(site.rglob('*')):
            if path.is_symlink(): raise ValueError('Dependency build tree contains a link')
            if path.is_file():
                name = path.relative_to(site).as_posix(); safe_member(name); additions[name] = path.read_bytes()
    if not additions and not preserve_existing: raise RuntimeError('No dependency files were prepared')
    with tempfile.TemporaryDirectory(prefix='evidenceflow-seal-') as temp:
        staged = Path(temp) / 'dependencies.zip'; hashes = {}
        with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            if preserve_existing:
                expected = (RUNTIME / '第三方依赖.sha256').read_text(encoding='utf-8').split()[0]
                if hashlib.sha256(bundle.read_bytes()).hexdigest() != expected: raise ValueError('Existing dependency ZIP SHA256 differs')
                with zipfile.ZipFile(bundle) as old:
                    if len(old.namelist()) != len(set(old.namelist())) or old.testzip() is not None: raise ValueError('Existing dependency ZIP is invalid')
                    for entry in old.infolist():
                        if entry.is_dir(): continue
                        safe_member(entry.filename); data = old.read(entry)
                        if entry.filename in additions:
                            if additions[entry.filename] != data: raise ValueError('Existing pinned file would change: ' + entry.filename)
                            additions.pop(entry.filename)
                        out.writestr(entry, data); hashes[entry.filename] = hashlib.sha256(data).hexdigest()
            for name, data in sorted(additions.items()):
                out.writestr(name, data); hashes[name] = hashlib.sha256(data).hexdigest()
        with zipfile.ZipFile(staged) as archive:
            if archive.testzip() is not None or len(archive.namelist()) != len(hashes): raise ValueError('Sealed dependency ZIP validation failed')
            for name, digest in hashes.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != digest: raise ValueError('Sealed file differs: ' + name)
        shutil.copyfile(staged, bundle)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (RUNTIME / '第三方依赖.sha256').write_text(digest + '  第三方依赖.zip\n', encoding='utf-8')
    parts=split_bundle(bundle)
    expanded = (RUNTIME / 'Lib').resolve(); intended = RUNTIME.resolve()
    # Verify the resolved absolute deletion target before any recursive removal.
    if expanded.exists():
        if expanded.parent != intended or expanded.name != 'Lib' or (RUNTIME / 'Lib').is_symlink(): raise ValueError('Expanded runtime path is outside the intended runtime')
        shutil.rmtree(expanded)
    (RUNTIME / 'python313._pth').write_text('python313.zip\n.\nimport site\n', encoding='ascii')
    return {'files': len(hashes), 'sha256': digest,'source_parts':parts}

if __name__ == '__main__': print(seal())
